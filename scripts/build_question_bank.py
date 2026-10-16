"""Translate every kiosk question into the offered Indian languages, once, into
data/question_bank.json -- where a native speaker can read it before any
patient hears it. See triage/question_bank.py for why this is a file and not a
live translation.

    set SARVAM_API_KEY, then:
    python scripts/build_question_bank.py            # all offered languages
    python scripts/build_question_bank.py hi-IN ta-IN

Safe to re-run: a translation whose English hasn't changed is reused, not
re-bought. Every run resets "reviewed" to false -- a changed file is an
unreviewed file.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage import question_bank, sarvam                      # noqa: E402
from triage.protocols.ctas import PROTOCOL                    # noqa: E402

PAUSE = 1.2   # seconds between calls -- Sarvam's Starter plan allows ~60 translate calls/min


def main() -> None:
    if not sarvam.configured():
        raise SystemExit("SARVAM_API_KEY is not set -- see the README's Indian-language section.")

    languages = sys.argv[1:] or [c for c in sarvam.LANGUAGES if c != sarvam.ENGLISH]
    for code in languages:
        if not sarvam.is_supported(code) or code == sarvam.ENGLISH:
            raise SystemExit(f"{code!r} isn't an offered non-English language: "
                             f"{[c for c in sarvam.LANGUAGES if c != sarvam.ENGLISH]}")

    existing = None
    if question_bank.BANK_PATH.exists():
        existing = json.loads(question_bank.BANK_PATH.read_text(encoding="utf-8"))

    calls = 0

    def translate(text: str, lang: str) -> str:
        nonlocal calls
        if calls:
            time.sleep(PAUSE)
        calls += 1
        out = sarvam.translate(text, sarvam.ENGLISH, lang)
        print(f"  [{lang}] {text}\n         -> {out}")
        return out

    bank = question_bank.build(PROTOCOL, languages, translate, existing=existing)
    question_bank.BANK_PATH.parent.mkdir(exist_ok=True)
    question_bank.BANK_PATH.write_text(
        json.dumps(bank, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {question_bank.BANK_PATH} -- {len(bank['questions'])} questions, "
          f"{calls} new translations. reviewed=false: have a native speaker read it.")


if __name__ == "__main__":
    main()
