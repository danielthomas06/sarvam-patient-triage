"""triage/sarvam.py and triage/question_bank.py.

Nothing here touches the network: `urlopen` is replaced, so these pin what we
SEND and what we do with what comes back -- including the two properties that
matter most for a third-party cloud on the speech path: the API key goes
only where it should, and a failure is loud, never an empty string passed off
as a transcript.
"""

import base64
import io
import json
import urllib.error

import pytest

from triage import question_bank, sarvam
from triage.protocols.ctas import PROTOCOL

KEY = "test-key-SHOULD-NEVER-APPEAR-ANYWHERE"


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int, body: dict | str) -> urllib.error.HTTPError:
    raw = body if isinstance(body, str) else json.dumps(body)
    return urllib.error.HTTPError("https://api.sarvam.ai/x", code, "err", {}, io.BytesIO(raw.encode()))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    sarvam._translate_cache.clear()
    sarvam._tts_cache.clear()
    monkeypatch.setattr(sarvam.time, "sleep", lambda s: None)


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", KEY)


def _fake_net(monkeypatch, responder):
    """Replace urlopen; returns the list of requests it was handed."""
    calls = []

    def fake(req, timeout=None):
        calls.append(req)
        out = responder(req)
        if isinstance(out, Exception):
            raise out
        return _Resp(out if isinstance(out, bytes) else json.dumps(out).encode())

    monkeypatch.setattr(sarvam.urllib.request, "urlopen", fake)
    return calls


# ---------------------------------------------------------------- configuration

def test_nothing_works_without_a_key():
    assert not sarvam.configured()
    for call in (lambda: sarvam.translate("नमस्ते", "hi-IN"),
                 lambda: sarvam.transcribe(b"RIFF....", "hi-IN"),
                 lambda: sarvam.synthesize("नमस्ते", "hi-IN")):
        with pytest.raises(sarvam.Unavailable, match="SARVAM_API_KEY"):
            call()


def test_only_the_wired_languages_are_offered(keyed):
    assert set(sarvam.LANGUAGES) == {"en-IN", "hi-IN", "ta-IN", "ml-IN"}
    assert not sarvam.is_supported("fr-FR")
    for call in (lambda: sarvam.synthesize("bonjour", "fr-FR"),
                 lambda: sarvam.transcribe(b"RIFF", "fr-FR"),
                 lambda: sarvam.translate("bonjour", "fr-FR")):
        with pytest.raises(sarvam.Unavailable, match="isn't offered"):
            call()


@pytest.mark.parametrize("text,expected", [
    ("मुझे सीने में दर्द है", True),        # Devanagari
    ("எனக்கு காய்ச்சல்", True),            # Tamil
    ("എനിക്ക് പനിയാണ്", True),             # Malayalam
    ("I have chest pain", False),
    ("mujhe bukhar hai", False),            # romanised Hindi: not guessed at
    ("", False),
])
def test_translation_is_only_attempted_on_indic_script(text, expected):
    assert sarvam.needs_translation(text) is expected


# ---------------------------------------------------------------- the key

def test_the_key_goes_in_the_header_and_nowhere_else(keyed, monkeypatch):
    calls = _fake_net(monkeypatch, lambda r: {"translated_text": "I have a fever"})
    sarvam.translate("मुझे बुखार है", "hi-IN")
    req = calls[0]
    headers = {k.lower(): v for k, v in req.header_items()}
    assert headers["api-subscription-key"] == KEY
    assert KEY not in req.full_url
    assert KEY.encode() not in req.data


@pytest.mark.parametrize("status,body", [
    (403, {"error": {"message": "bad key " + KEY}}),
    (500, {"error": {"message": "upstream said " + KEY + " is wrong"}}),
    (422, "plain text mentioning " + KEY),
])
def test_an_error_never_contains_the_key(keyed, monkeypatch, status, body):
    _fake_net(monkeypatch, lambda r: _http_error(status, body))
    with pytest.raises(sarvam.Unavailable) as exc:
        sarvam.translate("मुझे बुखार है", "hi-IN")
    assert KEY not in str(exc.value)


def test_a_failure_is_an_error_not_an_empty_string(keyed, monkeypatch):
    _fake_net(monkeypatch, lambda r: {"translated_text": "   "})
    with pytest.raises(sarvam.Unavailable, match="empty translation"):
        sarvam.translate("मुझे बुखार है", "hi-IN")


def test_one_retry_on_a_rate_limit_then_it_works(keyed, monkeypatch):
    attempts = []

    def responder(req):
        attempts.append(1)
        return _http_error(429, {"error": {"message": "slow down"}}) if len(attempts) == 1 \
            else {"translated_text": "fever"}

    _fake_net(monkeypatch, responder)
    assert sarvam.translate("बुखार", "hi-IN") == "fever"
    assert len(attempts) == 2


def test_a_persistent_rate_limit_says_so(keyed, monkeypatch):
    _fake_net(monkeypatch, lambda r: _http_error(429, {"error": {"message": "x"}}))
    with pytest.raises(sarvam.Unavailable, match="rate limit"):
        sarvam.translate("बुखार", "hi-IN")


# ---------------------------------------------------------------- speech to text

def test_transcribe_asks_for_the_patients_own_words_not_a_translation(keyed, monkeypatch):
    calls = _fake_net(monkeypatch, lambda r: {"transcript": "  எனக்கு காய்ச்சல்  ", "language_code": "ta-IN"})
    out = sarvam.transcribe(b"RIFFfakewav", "ta-IN")
    assert out == "எனக்கு காய்ச்சல்"                      # stripped, native script kept
    req = calls[0]
    assert req.full_url.endswith("/speech-to-text")
    assert req.get_header("Content-type").startswith("multipart/form-data; boundary=")
    body = req.data
    assert b'name="mode"\r\n\r\ntranscribe' in body       # never mode=translate
    assert b'name="language_code"\r\n\r\nta-IN' in body
    assert b'name="file"; filename="speech.wav"' in body
    assert b"RIFFfakewav" in body


def test_empty_audio_is_refused_before_any_request(keyed, monkeypatch):
    calls = _fake_net(monkeypatch, lambda r: {})
    with pytest.raises(sarvam.Unavailable, match="no audio"):
        sarvam.transcribe(b"", "hi-IN")
    assert calls == []


def test_multipart_encoding():
    body, ctype = sarvam._multipart({"a": "1", "b": "two"}, {"f": ("x.wav", b"\x00\x01", "audio/wav")})
    boundary = ctype.split("boundary=")[1]
    assert body.count(f"--{boundary}".encode()) == 4        # 3 parts + the closing marker
    assert body.endswith(f"--{boundary}--\r\n".encode())
    assert b'name="b"\r\n\r\ntwo' in body and b"\x00\x01" in body


# ---------------------------------------------------------------- translation

def test_long_text_is_split_on_sentence_ends_under_the_limit():
    sentence = "मुझे दो दिन से बुखार है। "
    text = sentence * 200
    chunks = sarvam._chunks(text, limit=300)
    assert len(chunks) > 1
    assert all(len(c) <= 300 for c in chunks)
    assert all(c.endswith("।") for c in chunks)              # never cut mid-sentence
    assert " ".join(chunks).replace(" ", "") == text.replace(" ", "").strip()


def test_translate_is_cached(keyed, monkeypatch):
    calls = _fake_net(monkeypatch, lambda r: {"translated_text": "Do you have a fever?"})
    for _ in range(3):
        assert sarvam.translate("क्या आपको बुखार है?", "hi-IN") == "Do you have a fever?"
    assert len(calls) == 1


def test_same_language_and_blank_text_cost_nothing(keyed, monkeypatch):
    calls = _fake_net(monkeypatch, lambda r: {"translated_text": "x"})
    assert sarvam.translate("already english", "en-IN", "en-IN") == "already english"
    assert sarvam.translate("   ", "hi-IN") == ""
    assert calls == []


# ---------------------------------------------------------------- text to speech

def _wav_b64() -> str:
    return base64.b64encode(b"RIFF....WAVEfake").decode()


def test_synthesize_decodes_and_caches(keyed, monkeypatch):
    calls = _fake_net(monkeypatch, lambda r: {"audios": [_wav_b64()]})
    first = sarvam.synthesize("क्या आपको बुखार है?", "hi-IN")
    again = sarvam.synthesize("क्या आपको बुखार है?", "hi-IN")
    assert first == again == b"RIFF....WAVEfake"
    assert len(calls) == 1                                    # a repeat question is free
    sent = json.loads(calls[0].data)
    assert sent["model"] == "bulbul:v3" and sent["language_code"] == "hi-IN"
    assert sent["output_audio_codec"] == "wav"


def test_malayalam_uses_a_voice_model_that_has_malayalam():
    """v4-flash has no Malayalam voice; the default must not be v4-flash."""
    assert "v4-flash" not in sarvam.TTS_MODEL


def test_synthesize_retries_under_the_older_field_name(keyed, monkeypatch):
    def responder(req):
        payload = json.loads(req.data)
        if "target_language_code" not in payload:
            return _http_error(422, {"error": {"message": "target_language_code is required"}})
        return {"audios": [_wav_b64()]}

    calls = _fake_net(monkeypatch, responder)
    assert sarvam.synthesize("നിങ്ങൾക്ക് പനിയുണ്ടോ?", "ml-IN").startswith(b"RIFF")
    assert len(calls) == 2


def test_overlong_text_is_refused_not_truncated(keyed):
    with pytest.raises(sarvam.Unavailable, match="too long"):
        sarvam.synthesize("अ" * (sarvam.MAX_TTS_CHARS + 1), "hi-IN")


def test_no_audio_back_is_an_error(keyed, monkeypatch):
    _fake_net(monkeypatch, lambda r: {"audios": []})
    with pytest.raises(sarvam.Unavailable, match="no audio"):
        sarvam.synthesize("नमस्ते", "hi-IN")


# ---------------------------------------------------------------- question bank

@pytest.fixture
def bank_file(tmp_path, monkeypatch):
    path = tmp_path / "question_bank.json"
    monkeypatch.setattr(question_bank, "BANK_PATH", path)
    monkeypatch.setattr(question_bank, "_cache", None)
    return path


def test_only_questions_a_kiosk_can_ask_are_banked():
    ids = set(question_bank.askable(PROTOCOL))
    assert "severe_pain_central" in ids
    assert "self_harm_risk" not in ids      # SENSITIVE: staff, in private, never a kiosk
    assert "spo2_under_90" not in ids       # MEASURE: a device, not a question


def test_build_translates_each_question_into_each_language():
    bank = question_bank.build(PROTOCOL, ["hi-IN", "ta-IN"], lambda t, l: f"{l}:{t}")
    entry = bank["questions"]["new_neuro_deficit"]
    english = PROTOCOL.discriminators["new_neuro_deficit"].question
    assert entry == {"en": english, "hi-IN": f"hi-IN:{english}", "ta-IN": f"ta-IN:{english}"}
    assert bank["_meta"]["reviewed"] is False


def test_rebuild_reuses_what_hasnt_changed_and_retranslates_what_has():
    first = question_bank.build(PROTOCOL, ["hi-IN"], lambda t, l: "v1:" + t)
    first["questions"]["new_neuro_deficit"]["en"] = "an older wording"     # the protocol moved on
    paid = []
    second = question_bank.build(PROTOCOL, ["hi-IN"], lambda t, l: paid.append(t) or "v2:" + t,
                                  existing=first)
    assert paid == [PROTOCOL.discriminators["new_neuro_deficit"].question]  # only that one
    assert second["questions"]["severe_pain_central"]["hi-IN"].startswith("v1:")


def test_lookup_serves_a_banked_translation(bank_file):
    english = PROTOCOL.discriminators["new_neuro_deficit"].question
    bank_file.write_text(json.dumps({"questions": {"new_neuro_deficit": {"en": english, "hi-IN": "हिन्दी प्रश्न"}}}),
                         encoding="utf-8")
    assert question_bank.lookup("new_neuro_deficit", "hi-IN", english) == "हिन्दी प्रश्न"
    assert question_bank.lookup("new_neuro_deficit", "ta-IN", english) is None   # no Tamil banked


def test_a_stale_translation_is_not_served_for_a_changed_question(bank_file):
    bank_file.write_text(json.dumps({"questions": {"new_neuro_deficit": {"en": "the old question", "hi-IN": "पुराना"}}}),
                         encoding="utf-8")
    assert question_bank.lookup("new_neuro_deficit", "hi-IN", "the question as it reads now") is None


def test_a_missing_bank_is_not_an_error(bank_file):
    assert question_bank.lookup("anything", "hi-IN", "x") is None
    assert question_bank.reviewed() is False
