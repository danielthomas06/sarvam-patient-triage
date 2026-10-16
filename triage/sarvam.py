"""Sarvam AI -- Indian-language speech, translation and voice, over plain HTTPS.

Used for patients who don't speak English: their spoken answer is transcribed
in its own language, translated to English for the extraction models, and the
system's questions are translated back and read aloud. Standard library only,
matching ollama.py and local_stt.py -- no SDK to install.

THE BOUNDARY STILL HOLDS. Nothing here touches a BeliefState or a Category.
This module turns audio into text and text into text; what the text *means* is
still decided by the same guarded extraction and the same deterministic engine
as for an English patient. What it does add is a new way for a finding to be
wrong -- a mistranslation can flip "no chest pain" -- so serve.py always keeps
the patient's own words alongside the English (see Encounter.transcript_native)
and the nurse screen shows both. This module's job is only to be honest about
when it couldn't do its part: every failure is an `Unavailable`, never a
quietly empty string.

PRIVACY. Unlike the Vosk and Ollama tiers, this one sends audio and text to a
third party's cloud (api.sarvam.ai). The key is read from the environment
(SARVAM_API_KEY) and is never logged, never put in an exception message, and
never sent anywhere except the `api-subscription-key` header of a request to
Sarvam. Retention is configured per workspace in Sarvam's dashboard, not here.

Endpoints, models and field names follow Sarvam's published docs as read in
October 2026; they have not been exercised against a live key from this
codebase until the first real call, so treat a 4xx here as "check the docs"
before "check the code".
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("SARVAM_BASE_URL", "https://api.sarvam.ai").rstrip("/")
STT_MODEL = os.environ.get("SARVAM_STT_MODEL", "saaras:v4")
TRANSLATE_MODEL = os.environ.get("SARVAM_TRANSLATE_MODEL", "sarvam-translate:v1")
# bulbul:v3 rather than v4-flash on purpose: v4-flash does not yet offer
# Malayalam or Odia voices, v3 covers all eleven TTS languages.
TTS_MODEL = os.environ.get("SARVAM_TTS_MODEL", "bulbul:v3")
TTS_SPEAKER = os.environ.get("SARVAM_TTS_SPEAKER", "priya")
TIMEOUT = float(os.environ.get("SARVAM_TIMEOUT", "30"))

ENGLISH = "en-IN"

# The languages this deployment offers a patient. Sarvam supports many more
# for speech-to-text (23) and translation (22); this is the set that has been
# wired end to end -- add one by adding a line, then re-run
# scripts/build_question_bank.py and have a native speaker review the result.
LANGUAGES: dict[str, dict[str, str]] = {
    "en-IN": {"native": "English", "english": "English"},
    "hi-IN": {"native": "हिन्दी", "english": "Hindi"},
    "ta-IN": {"native": "தமிழ்", "english": "Tamil"},
    "ml-IN": {"native": "മലയാളം", "english": "Malayalam"},
}

# REST speech-to-text refuses audio longer than this (HTTP 422); the browser
# recorder auto-stops just under it (web/voice.js).
MAX_STT_SECONDS = 30
# Translation request limits are 1,000 chars (mayura) / 2,000 (sarvam-translate).
TRANSLATE_CHUNK = 1800
MAX_TTS_CHARS = 500


class Unavailable(RuntimeError):
    """Not configured, unreachable, rate limited, or refused. Always carries a
    message that is safe to show a nurse -- and never contains the API key."""


def api_key() -> str | None:
    return (os.environ.get("SARVAM_API_KEY") or "").strip() or None


def configured() -> bool:
    return api_key() is not None


def available() -> tuple[bool, str]:
    """(True, 'ready') when a key is set. Deliberately NOT a network probe --
    every probe is a billable, rate-limited call, and /api/config is polled."""
    if configured():
        return True, "key set (not probed -- first real call will confirm)"
    return False, "SARVAM_API_KEY is not set"


def is_supported(code: str) -> bool:
    return code in LANGUAGES


def needs_translation(text: str) -> bool:
    """Does this text contain Indic-script letters (Devanagari through
    Malayalam, U+0900-U+0DFF)? Plain ASCII -- English, or romanised Hindi a
    patient typed on an English keyboard -- is passed through untouched rather
    than sent to a translator that would have to guess its language."""
    return any("ऀ" <= ch <= "෿" for ch in text)


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

def _clean(message: str) -> str:
    """Belt and braces: whatever came back from the network, the key is not in
    anything we raise or log."""
    key = api_key()
    if key:
        message = message.replace(key, "***")
    return message[:300]


def _send(req: urllib.request.Request, *, retries: int = 1) -> bytes:
    """One request, with one retry on 429/503 (Sarvam's documented transient
    codes). Everything else becomes `Unavailable` with a readable reason."""
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            detail = body
            try:
                detail = (json.loads(body).get("error") or {}).get("message") or body
            except (ValueError, AttributeError):
                pass
            if exc.code in (429, 503) and attempt < retries:
                time.sleep(1.5)
                continue
            if exc.code == 403:
                raise Unavailable("Sarvam rejected the API key (HTTP 403)") from None
            if exc.code == 429:
                raise Unavailable("Sarvam rate limit reached -- try again in a moment") from None
            raise Unavailable(_clean(f"Sarvam HTTP {exc.code}: {detail}")) from None
        except urllib.error.URLError as exc:
            raise Unavailable(_clean(f"Sarvam unreachable: {exc.reason}")) from None
        except TimeoutError:
            raise Unavailable(f"Sarvam timed out after {TIMEOUT:.0f}s") from None
    raise Unavailable("Sarvam request failed")   # pragma: no cover


def _headers(extra: dict | None = None) -> dict:
    key = api_key()
    if not key:
        raise Unavailable("SARVAM_API_KEY is not set")
    return {"api-subscription-key": key, **(extra or {})}


def _post_json(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(payload).encode(), method="POST",
        headers=_headers({"Content-Type": "application/json"}),
    )
    raw = _send(req)
    try:
        return json.loads(raw)
    except ValueError:
        raise Unavailable("Sarvam returned something that wasn't JSON") from None


def _multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]):
    """(body, content_type) for a multipart/form-data upload. Hand-rolled
    because the standard library has no encoder and an SDK just for this
    would be the project's first runtime dependency on the speech path."""
    boundary = uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="{name}"\r\n\r\n{value}\r\n').encode()
    for name, (filename, data, ctype) in files.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n").encode()
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


# --------------------------------------------------------------------------
# speech to text
# --------------------------------------------------------------------------

def transcribe(wav_bytes: bytes, language_code: str) -> str:
    """Audio in the patient's language -> text in that language's own script.

    `mode="transcribe"` on purpose: the patient's actual words are the record
    of truth. Translation is a separate, visible step (see `translate`), so
    the nurse can see both and a translation error can't hide a transcription
    one -- or vice versa. Raw WAV must be mono 16-bit PCM, which is exactly
    what web/voice.js's recorder produces.
    """
    if not wav_bytes:
        raise Unavailable("no audio received")
    if not is_supported(language_code):
        raise Unavailable(f"language {language_code!r} isn't offered")
    body, ctype = _multipart(
        {"model": STT_MODEL, "mode": "transcribe", "language_code": language_code},
        {"file": ("speech.wav", wav_bytes, "audio/wav")},
    )
    req = urllib.request.Request(
        f"{BASE}/speech-to-text", data=body, method="POST",
        headers=_headers({"Content-Type": ctype}),
    )
    try:
        payload = json.loads(_send(req))
    except ValueError:
        raise Unavailable("Sarvam returned something that wasn't JSON") from None
    return (payload.get("transcript") or "").strip()


# --------------------------------------------------------------------------
# translation (cached -- the same reviewed question is asked of many patients)
# --------------------------------------------------------------------------

_cache_lock = threading.Lock()
_translate_cache: dict[tuple[str, str, str], str] = {}
_tts_cache: dict[tuple[str, str, str], bytes] = {}
_CACHE_MAX = 512


def _remember(cache: dict, key, value) -> None:
    with _cache_lock:
        if len(cache) >= _CACHE_MAX:
            cache.pop(next(iter(cache)))      # oldest first; good enough here
        cache[key] = value


def _chunks(text: str, limit: int = TRANSLATE_CHUNK) -> list[str]:
    """Split on sentence ends (Latin and Devanagari danda) so no chunk exceeds
    the translator's request limit, without cutting a sentence in half."""
    if len(text) <= limit:
        return [text]
    parts = re.split(r"(?<=[.!?।॥])\s+", text)
    out: list[str] = []
    cur = ""
    for p in parts:
        while len(p) > limit:                 # one enormous "sentence"
            out.append(p[:limit])
            p = p[limit:]
        if cur and len(cur) + 1 + len(p) > limit:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        out.append(cur)
    return out


def translate(text: str, source: str, target: str = ENGLISH) -> str:
    text = (text or "").strip()
    if not text or source == target:
        return text
    for code in (source, target):
        if not is_supported(code):
            raise Unavailable(f"language {code!r} isn't offered")
    key = (text, source, target)
    with _cache_lock:
        hit = _translate_cache.get(key)
    if hit is not None:
        return hit

    pieces = []
    for chunk in _chunks(text):
        payload = _post_json("/translate", {
            "input": chunk, "source_language_code": source,
            "target_language_code": target, "model": TRANSLATE_MODEL,
        })
        translated = (payload.get("translated_text") or "").strip()
        if not translated:
            raise Unavailable("Sarvam returned an empty translation")
        pieces.append(translated)
    result = " ".join(pieces)
    _remember(_translate_cache, key, result)
    return result


# --------------------------------------------------------------------------
# text to speech
# --------------------------------------------------------------------------

def synthesize(text: str, language_code: str) -> bytes:
    """Text in the patient's language -> WAV audio. Cached by (text, language,
    voice): the reviewed question bank is a few dozen sentences asked over and
    over, so after the first patient this costs nothing and, under a surge,
    spends none of the rate limit."""
    text = (text or "").strip()
    if not text:
        raise Unavailable("nothing to speak")
    if len(text) > MAX_TTS_CHARS:
        raise Unavailable(f"text too long to speak ({len(text)} > {MAX_TTS_CHARS} characters)")
    if not is_supported(language_code):
        raise Unavailable(f"language {language_code!r} isn't offered")
    key = (text, language_code, f"{TTS_MODEL}/{TTS_SPEAKER}")
    with _cache_lock:
        hit = _tts_cache.get(key)
    if hit is not None:
        return hit

    payload = {"text": text, "model": TTS_MODEL, "speaker": TTS_SPEAKER,
               "output_audio_codec": "wav", "language_code": language_code}
    try:
        data = _post_json("/text-to-speech", payload)
    except Unavailable as exc:
        # The docs name the field `language_code`; older docs and SDKs called
        # it `target_language_code`. One retry under the old name, so a doc
        # drift is a quiet fix rather than a dead feature.
        if "target_language_code" not in str(exc):
            raise
        payload["target_language_code"] = payload.pop("language_code")
        data = _post_json("/text-to-speech", payload)

    audios = data.get("audios") or []
    if not audios:
        raise Unavailable("Sarvam returned no audio")
    try:
        wav = base64.b64decode("".join(audios))
    except ValueError:
        raise Unavailable("Sarvam returned audio that couldn't be decoded") from None
    _remember(_tts_cache, key, wav)
    return wav
