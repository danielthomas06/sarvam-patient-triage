"""A pre-translated bank of the questions a kiosk asks, for non-English sessions.

WHY A FILE, NOT A LIVE TRANSLATION PER QUESTION. For English patients the
wording of a question can be model-generated, because `_vet_question` checks
it against the English words that would leak the answer ("Is the pain
spreading to your jaw?" plants "yes"). That vetting cannot be run on a Tamil
sentence. So for a non-English session the English question is always the
*reviewed* wording from the protocol (`Discriminator.question`), and what the
patient hears is a translation of exactly that, produced ahead of time into
`data/question_bank.json` where a native speaker can read it before it is
ever spoken to anyone. A runtime translation is only the fallback when a
question isn't in the bank yet, and the bank says plainly whether anyone has
reviewed it.

An entry is only used while its stored English still equals the protocol's
current English: edit a question and its old translations stop being served
instead of silently describing a different question.
"""

from __future__ import annotations

import json
import pathlib
from typing import Callable

from .core import Protocol, Source

BANK_PATH = pathlib.Path(__file__).resolve().parent.parent / "data" / "question_bank.json"

_cache: tuple[float, dict] | None = None


def _load() -> dict:
    global _cache
    try:
        mtime = BANK_PATH.stat().st_mtime
    except OSError:
        return {}
    if _cache is None or _cache[0] != mtime:
        try:
            _cache = (mtime, json.loads(BANK_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}
    return _cache[1]


def lookup(check_id: str, language: str, english: str) -> str | None:
    """The banked translation of `check_id`'s question, or None if there isn't
    one, or if the English it was translated from has since changed."""
    entry = (_load().get("questions") or {}).get(check_id)
    if not entry or entry.get("en") != english:
        return None
    return entry.get(language) or None


def reviewed() -> bool:
    """Has a human signed off on the bank? Surfaced rather than assumed."""
    return bool((_load().get("_meta") or {}).get("reviewed"))


def askable(protocol: Protocol) -> dict[str, str]:
    """check id -> English question, for every check a kiosk can put to a
    patient. SENSITIVE checks are staff-only and never spoken to a patient;
    MEASURE / OBSERVE / RECORD checks are not patient questions at all."""
    return {did: d.question for did, d in sorted(protocol.discriminators.items())
            if d.source is Source.ASK}


def build(protocol: Protocol, languages: list[str],
          translate: Callable[[str, str], str], *, existing: dict | None = None) -> dict:
    """Translate every askable question into each language. `translate(text,
    language) -> str`. Reuses anything in `existing` whose English is
    unchanged, so re-running after adding one language or editing one
    question only pays for what moved."""
    old = (existing or {}).get("questions") or {}
    questions: dict[str, dict] = {}
    for did, english in askable(protocol).items():
        entry = {"en": english}
        prev = old.get(did) or {}
        for lang in languages:
            if prev.get("en") == english and prev.get(lang):
                entry[lang] = prev[lang]
            else:
                entry[lang] = translate(english, lang)
        questions[did] = entry
    return {
        "_meta": {
            "reviewed": False,
            "note": ("Machine-translated. NOT yet reviewed by a native speaker or a "
                     "clinician -- read every line before a patient hears it. Set "
                     "reviewed to true only after that has happened."),
            "languages": languages,
        },
        "questions": questions,
    }
