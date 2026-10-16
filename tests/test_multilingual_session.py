"""A patient who doesn't speak English, end to end through an Encounter.

The translator is faked, so these pin OUR behaviour around it: what gets
translated, what is kept, what the models and the engine are shown, and what
happens when the translator isn't there. Whether Sarvam translates well is a
separate question -- one a native speaker has to answer, not a test.
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import serve
from triage import question_bank, sarvam
from triage.protocols.ctas import PROTOCOL

HINDI_CHEST = "मुझे सीने में दर्द है"
TAMIL_NO = "இல்லை"


@pytest.fixture(autouse=True)
def _offline_models(monkeypatch):
    monkeypatch.setattr(serve, "USE_MODEL", False)       # keyword tier: no model calls
    monkeypatch.setattr(question_bank, "BANK_PATH", question_bank.BANK_PATH.with_name("_absent.json"))
    monkeypatch.setattr(question_bank, "_cache", None)
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)


def _translator(table: dict[str, str]):
    def fake(text, source, target="en-IN"):
        if text in table:
            return table[text]
        raise AssertionError(f"unexpected translation request: {text!r}")
    return fake


def _patient(monkeypatch, language="hi-IN", table=None):
    e = serve.new_patient()
    e.set_language(language)
    monkeypatch.setattr(serve._sarvam, "translate", _translator(table or {}))
    return e


# ---------------------------------------------------------------- the narrative

def test_an_english_session_is_untouched(monkeypatch):
    e = serve.new_patient()
    monkeypatch.setattr(serve._sarvam, "translate",
                        lambda *a, **k: pytest.fail("English must never reach the translator"))
    e.add_narrative("I have chest pain")
    assert e.transcript == ["I have chest pain"]
    assert e.transcript_native == [""]
    assert e.state()["language"] == "en-IN"


def test_a_hindi_narrative_is_translated_for_extraction_and_the_original_is_kept(monkeypatch):
    e = _patient(monkeypatch, "hi-IN", {HINDI_CHEST: "I have chest pain"})
    e.add_narrative(HINDI_CHEST)

    assert e.transcript == ["I have chest pain"]           # what the models read
    assert e.transcript_native == [HINDI_CHEST]            # what the patient said
    assert e.weights["chest_pain"] >= 0.5                  # extraction worked on the English

    st = e.state()
    assert st["transcript"] == ["I have chest pain"]
    assert st["transcript_native"] == [HINDI_CHEST]
    assert st["language"] == "hi-IN"


def test_the_translation_is_in_the_audit_trail(monkeypatch):
    e = _patient(monkeypatch, "hi-IN", {HINDI_CHEST: "I have chest pain"})
    e.add_narrative(HINDI_CHEST)
    events = [ev for ev in e.ledger.events if ev.payload.get("check") == "translation"]
    assert len(events) == 1
    assert HINDI_CHEST in events[0].payload["quote"] and "I have chest pain" in events[0].payload["quote"]
    assert e.ledger.verify()[0]                              # the chain still verifies


def test_ascii_text_in_a_hindi_session_is_not_sent_to_the_translator(monkeypatch):
    e = _patient(monkeypatch, "hi-IN", {})                   # any translation request would fail the test
    e.add_narrative("chest pain since morning")
    assert e.transcript == ["chest pain since morning"]
    assert e.transcript_native == [""]


def test_if_the_translator_is_down_the_patients_own_words_still_get_through(monkeypatch):
    e = serve.new_patient()
    e.set_language("ta-IN")

    def down(*a, **k):
        raise sarvam.Unavailable("Sarvam unreachable: timed out")

    monkeypatch.setattr(serve._sarvam, "translate", down)
    e.add_narrative("எனக்கு நெஞ்சு வலி")

    assert e.transcript == ["எனக்கு நெஞ்சு வலி"]            # not dropped
    assert any("translation unavailable" in f for f in e.fallbacks)
    assert e.untranslated is True


def test_an_unread_patient_is_flagged_in_the_nurse_queue_not_left_as_a_quiet_non_urgent(monkeypatch):
    """The keyword tier can't read Tamil, so with the translator down a patient
    who just described chest pain scores Non-urgent purely because nothing was
    understood. The queue must say so out loud."""
    e = serve.new_patient()                      # origin "kiosk" by default
    e.set_language("ta-IN")
    monkeypatch.setattr(serve._sarvam, "translate",
                        lambda *a, **k: (_ for _ in ()).throw(sarvam.Unavailable("down")))
    e.add_narrative("எனக்கு நெஞ்சு வலி")
    row = next(r for r in serve.queue_rows() if r["ref"] == e.ref)
    assert row["untranslated"] is True and row["language"] == "ta-IN"
    assert e.state()["untranslated"] is True


def test_a_translated_patient_is_not_flagged(monkeypatch):
    e = _patient(monkeypatch, "hi-IN", {HINDI_CHEST: "I have chest pain"})
    e.add_narrative(HINDI_CHEST)
    assert e.untranslated is False
    assert next(r for r in serve.queue_rows() if r["ref"] == e.ref)["untranslated"] is False


def test_an_unsupported_language_is_refused():
    e = serve.new_patient()
    with pytest.raises(ValueError):
        e.set_language("fr-FR")
    assert e.language == "en-IN"


# ---------------------------------------------------------------- the replies

def test_a_tamil_no_is_translated_before_it_is_parsed(monkeypatch):
    e = _patient(monkeypatch, "ta-IN", {TAMIL_NO: "no"})
    e.add_narrative("chest pain")
    outcome = e.answer("persistent_vomiting", TAMIL_NO)
    assert outcome == "false"                                # recorded as a real denial
    assert e.belief.is_observed("persistent_vomiting")
    quote = [ev for ev in e.ledger.events
             if ev.payload.get("check") == "persistent_vomiting"][-1].payload["quote"]
    assert quote == f"no [said: {TAMIL_NO}]"                 # the record shows both


def test_an_untranslatable_reply_stays_unknown_rather_than_becoming_a_no(monkeypatch):
    """The dangerous direction: a reply nobody could read must NOT become a
    negative. keyword_parse needs an actual English denial word to say 'no'."""
    e = serve.new_patient()
    e.set_language("ta-IN")
    monkeypatch.setattr(serve._sarvam, "translate",
                        lambda *a, **k: (_ for _ in ()).throw(sarvam.Unavailable("down")))
    outcome = e.answer("persistent_vomiting", TAMIL_NO)
    assert outcome == "unclear"
    assert not e.belief.is_observed("persistent_vomiting")


# ---------------------------------------------------------------- the questions

def _ready(monkeypatch, language, table):
    e = _patient(monkeypatch, language, table)
    e.set_identity({"age": "40", "sex": "M"})
    e.add_narrative("chest pain") if False else e.add_narrative("I have chest pain")
    return e


def test_a_non_english_session_asks_the_protocols_reviewed_english_not_generated_wording(monkeypatch):
    seen = []
    e = serve.new_patient()
    e.set_language("hi-IN")
    e.set_identity({"age": "40", "sex": "M"})
    monkeypatch.setattr(serve._sarvam, "translate",
                        lambda text, src, tgt="en-IN": seen.append(text) or f"HI<{text}>")
    e.add_narrative("I have chest pain")
    step = e.next_step()
    d = PROTOCOL.discriminators[step["check"]]
    assert step["question"] == d.question                    # reviewed wording, always
    assert step["question_native"] == f"HI<{d.question}>"    # and that is what gets translated
    assert step["language"] == "hi-IN"


def test_the_reviewed_bank_wins_over_a_live_translation(monkeypatch):
    e = serve.new_patient()
    e.set_language("ml-IN")
    e.set_identity({"age": "40", "sex": "M"})
    monkeypatch.setattr(serve._sarvam, "translate",
                        lambda *a, **k: pytest.fail("a banked question must not be re-translated"))
    monkeypatch.setattr(serve._qbank, "lookup", lambda cid, lang, en: f"BANKED[{lang}]")
    e.add_narrative("I have chest pain")
    step = e.next_step()
    assert step["question_native"] == "BANKED[ml-IN]"


def test_if_no_translation_is_available_the_question_still_comes_back_in_english(monkeypatch):
    e = serve.new_patient()
    e.set_language("hi-IN")
    e.set_identity({"age": "40", "sex": "M"})
    e.add_narrative("I have chest pain")
    monkeypatch.setattr(serve._sarvam, "translate",
                        lambda *a, **k: (_ for _ in ()).throw(sarvam.Unavailable("down")))
    step = e.next_step()
    assert step["question"] and step["question_native"] is None   # the kiosk can fall back


def test_an_english_session_gets_no_native_question():
    e = serve.new_patient()
    e.set_identity({"age": "40", "sex": "M"})
    e.add_narrative("I have chest pain")
    step = e.next_step()
    assert step["question_native"] is None and step["language"] == "en-IN"


# ---------------------------------------------------------------- audio routing

def test_english_audio_stays_on_the_local_model_even_if_a_key_is_set(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "k")
    monkeypatch.setattr(serve._sarvam, "transcribe",
                        lambda *a, **k: pytest.fail("English audio must not be sent to a cloud"))
    monkeypatch.setattr(serve._stt, "available", lambda: (True, "ready"))
    monkeypatch.setattr(serve._stt, "transcribe_wav", lambda b: "I have a headache")
    status, payload = serve.transcribe_audio(b"RIFF", "en-IN")
    assert (status, payload["text"], payload["engine"]) == (200, "I have a headache", "vosk")


def test_hindi_audio_goes_to_sarvam_and_comes_back_in_hindi(monkeypatch):
    monkeypatch.setattr(serve._sarvam, "transcribe", lambda audio, lang: "मुझे बुखार है")
    status, payload = serve.transcribe_audio(b"RIFF", "hi-IN")
    assert (status, payload["text"], payload["engine"]) == (200, "मुझे बुखार है", "sarvam")


def test_non_english_audio_with_sarvam_down_is_an_honest_503_not_a_guess(monkeypatch):
    def down(*a, **k):
        raise sarvam.Unavailable("SARVAM_API_KEY is not set")

    monkeypatch.setattr(serve._sarvam, "transcribe", down)
    monkeypatch.setattr(serve._stt, "transcribe_wav",
                        lambda b: pytest.fail("the English-only local model must never be tried on Tamil"))
    status, payload = serve.transcribe_audio(b"RIFF", "ta-IN")
    assert status == 503 and payload["text"] == "" and "SARVAM_API_KEY" in payload["error"]


@pytest.mark.parametrize("lang", ["en-IN", "hi-IN"])
def test_no_audio_is_a_400_on_every_path(monkeypatch, lang):
    monkeypatch.setattr(serve._stt, "available", lambda: (True, "ready"))
    assert serve.transcribe_audio(b"", lang)[0] == 400


def test_an_unoffered_language_is_a_400():
    assert serve.transcribe_audio(b"RIFF", "fr-FR")[0] == 400


# ---------------------------------------------------------------- the real routes

@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _call(base, path, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type"), exc.read()


def test_the_picker_only_offers_english_until_there_is_a_key(server, monkeypatch):
    status, _, body = _call(server, "/api/languages")
    offered = [l["code"] for l in json.loads(body)["languages"]]
    assert status == 200 and offered == ["en-IN"]

    monkeypatch.setenv("SARVAM_API_KEY", "k")
    offered = [l["code"] for l in json.loads(_call(server, "/api/languages")[2])["languages"]]
    assert offered == ["en-IN", "hi-IN", "ta-IN", "ml-IN"]


def test_setting_a_language_over_http(server):
    ref = json.loads(_call(server, "/api/patient/new", {"origin": "kiosk"})[2])["ref"]
    status, _, body = _call(server, "/api/language", {"ref": ref, "language": "ta-IN"})
    assert status == 200 and json.loads(body)["language"] == "ta-IN"
    assert _call(server, "/api/language", {"ref": ref, "language": "xx-XX"})[0] == 400


def test_tts_needs_a_live_session_so_it_cant_be_an_open_proxy(server, monkeypatch):
    monkeypatch.setattr(serve._sarvam, "synthesize",
                        lambda *a, **k: pytest.fail("no session, so nothing may be synthesised"))
    status, _, body = _call(server, "/api/tts", {"ref": "nope", "text": "hi", "language": "hi-IN"})
    assert status == 404 and json.loads(body)["no_such_patient"]


def test_tts_returns_audio_for_a_live_session(server, monkeypatch):
    ref = json.loads(_call(server, "/api/patient/new", {"origin": "kiosk"})[2])["ref"]
    monkeypatch.setattr(serve._sarvam, "synthesize", lambda text, lang: b"RIFF-FAKE-WAV")
    status, ctype, body = _call(server, "/api/tts", {"ref": ref, "text": "क्या आपको बुखार है?", "language": "hi-IN"})
    assert (status, ctype, body) == (200, "audio/wav", b"RIFF-FAKE-WAV")


def test_tts_failure_is_a_json_503_the_kiosk_can_fall_back_from(server, monkeypatch):
    ref = json.loads(_call(server, "/api/patient/new", {"origin": "kiosk"})[2])["ref"]

    def down(*a, **k):
        raise sarvam.Unavailable("Sarvam rate limit reached -- try again in a moment")

    monkeypatch.setattr(serve._sarvam, "synthesize", down)
    status, _, body = _call(server, "/api/tts", {"ref": ref, "text": "x", "language": "hi-IN"})
    assert status == 503 and "rate limit" in json.loads(body)["error"]


def test_transcribe_route_passes_the_language_through(server, monkeypatch):
    got = {}
    monkeypatch.setattr(serve._sarvam, "transcribe",
                        lambda audio, lang: got.update(lang=lang, n=len(audio)) or "बुखार")
    status, _, body = _call(server, "/api/transcribe?lang=hi-IN", raw=b"RIFFxxxx")
    assert status == 200 and json.loads(body)["text"] == "बुखार"
    assert got == {"lang": "hi-IN", "n": 8}


def test_identity_extraction_translates_a_non_english_introduction_first(server, monkeypatch):
    monkeypatch.setattr(serve._sarvam, "translate",
                        lambda text, src, tgt="en-IN": "My name is Priya Raman, I am 34, female.")
    status, _, body = _call(server, "/api/extract_identity",
                            {"text": "मेरा नाम प्रिया रमन है, मैं 34 साल की महिला हूँ", "language": "hi-IN"})
    out = json.loads(body)
    assert status == 200 and out["name"] == "Priya Raman" and out["sex"] == "F" and out["age"] == 34
    assert out["heard"].startswith("मेरा नाम")              # the kiosk shows the patient what THEY said
