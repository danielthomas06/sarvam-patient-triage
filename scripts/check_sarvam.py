"""Live smoke test for the Sarvam integration -- no microphone, no app needed.

    set SARVAM_API_KEY, then:   python scripts/check_sarvam.py

For each language it round-trips one sentence through all three endpoints:

    text --(text-to-speech)--> audio --(speech-to-text)--> text --(translate)--> English

so a clean run proves the key, the three endpoints and our field names in one
go, and a failure prints exactly which step broke and why. It costs a few
rupees of credit at most. It does not touch the app or any patient data.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")     # Windows consoles default to cp1252

from triage import sarvam                                      # noqa: E402

SENTENCES = {
    "hi-IN": "मुझे दो दिन से बुखार है और सीने में दर्द है।",
    "ta-IN": "எனக்கு இரண்டு நாட்களாக காய்ச்சல் இருக்கிறது, நெஞ்சு வலிக்கிறது.",
    "ml-IN": "എനിക്ക് രണ്ട് ദിവസമായി പനിയുണ്ട്, നെഞ്ചുവേദനയുമുണ്ട്.",
}


def step(label: str, fn):
    try:
        out = fn()
    except sarvam.Unavailable as exc:
        print(f"   FAIL  {label}: {exc}")
        return None
    print(f"   ok    {label}")
    return out


def main() -> int:
    if not sarvam.configured():
        print("SARVAM_API_KEY is not set in this terminal.")
        print('PowerShell:  $env:SARVAM_API_KEY = "your-key"')
        return 2

    failures = 0
    for lang, sentence in SENTENCES.items():
        print(f"\n{sarvam.LANGUAGES[lang]['english']} ({lang})")
        print(f"   said      : {sentence}")

        wav = step("text-to-speech", lambda: sarvam.synthesize(sentence, lang))
        heard = step("speech-to-text", lambda: sarvam.transcribe(wav, lang)) if wav else None
        if heard is not None:
            print(f"   heard     : {heard}")
        english = step("translate to English", lambda: sarvam.translate(heard or sentence, lang))
        if english is not None:
            print(f"   English   : {english}")
        failures += not (wav and heard and english)

    print("\n" + ("ALL THREE ENDPOINTS WORK for every language." if not failures
                  else f"{failures} language(s) had a failing step -- paste the FAIL lines back."))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
