"""Name, age, and sex out of one spoken self-introduction.

Kiosk check-in used to need a separate voice button per field (name, age,
sex...) -- five small, isolated dictations. This lets a patient say it once,
naturally: "My name is John Smith, I'm 45, male." Same three-tier ladder as
extract.py's seed() -- hosted model, then local, then a plain regex fallback
-- but a much lighter guard than seed()'s belief-state pipeline: name/age/sex
aren't clinical findings with an under-triage asymmetry to protect against,
they're reception details a person sees on screen immediately afterward and
can correct with a keystroke. The one rule kept unchanged from seed(): a
field not actually stated is left unset, never guessed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

MODEL = os.environ.get("TRIAGE_MODEL", "claude-opus-5")

try:
    from pydantic import BaseModel, Field

    class IdentityFields(BaseModel):
        name: str | None = Field(default=None, description="Full name, exactly as stated. Omit entirely if not said.")
        age_years: float | None = Field(
            default=None,
            description="Age in whole or fractional years, only if stated in years or clearly not an infant.")
        age_months: float | None = Field(
            default=None,
            description="Age in months, ONLY if stated in months or the person is clearly an infant under 2. "
                        "Never set both age_years and age_months.")
        sex: str | None = Field(default=None, description='Exactly "M", "F", or "Other". Omit if not stated.')

    _HAVE_PYDANTIC = True
except ImportError:  # pragma: no cover - offline install
    _HAVE_PYDANTIC = False


@dataclass(frozen=True, slots=True)
class Identity:
    name: str | None = None
    age_years: float | None = None
    age_months: float | None = None
    sex: str | None = None
    tier: str = "hosted"
    heard: str = ""   # the transcript this came from, echoed back for the UI


class Unavailable(Exception):
    """No model reachable. Caller falls through to the next rung."""


_SYSTEM = (
    "You read one short spoken self-introduction from a patient checking in "
    "at an emergency department, and pull out only their name, age, and sex.\n\n"
    "Rules:\n"
    "- Extract only what is explicitly stated. Never infer, guess, or complete a name.\n"
    "- If age is given in months, or the person is clearly an infant under two "
    "years old, set age_months and leave age_years unset. Otherwise set age_years.\n"
    '- sex must be exactly "M", "F", or "Other" if stated -- otherwise leave it unset.\n'
    "- Leave any field unset rather than guess at it.\n"
)


def _client():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise Unavailable("anthropic SDK not installed") from exc
    try:
        return anthropic.Anthropic()
    except Exception as exc:  # pragma: no cover - credential resolution
        raise Unavailable(str(exc)) from exc


# --------------------------------------------------------------------------
# tier 3 -- no model at all
# --------------------------------------------------------------------------

_NAME_RE = re.compile(
    r"\b(?:my name is|name'?s|i'?m|i am|this is)\s+"
    r"([A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*){0,2})",
    re.IGNORECASE,
)
_STOP_WORDS = {"and", "i'm", "im", "i", "a", "an"}

_AGE_MONTHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*months?\s*old\b", re.IGNORECASE)
_AGE_YEARS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:years?\s*old|yo\b|years?)\b", re.IGNORECASE)
_BARE_AGE_RE = re.compile(r"\b(?:i'?m|i am)\s+(\d{1,3})\b", re.IGNORECASE)

_SEX_WORDS = (
    (re.compile(r"\bfemale\b|\bwoman\b|\bgirl\b", re.IGNORECASE), "F"),
    (re.compile(r"\bmale\b|\bman\b|\bboy\b", re.IGNORECASE), "M"),
    (re.compile(r"\bother\b", re.IGNORECASE), "Other"),
)


def _regex_fallback(text: str) -> Identity:
    """Deliberately crude, like keyword_seed() elsewhere in this project: it
    fills only what a regex can defend and leaves the rest unset for the
    patient to type. Never guesses a name or a sex it didn't see stated."""
    name = None
    m = _NAME_RE.search(text)
    if m:
        words = [w for w in m.group(1).split() if w.lower() not in _STOP_WORDS]
        if words:
            name = " ".join(words).strip(" ,.").title()

    age_years = age_months = None
    m = _AGE_MONTHS_RE.search(text)
    if m:
        age_months = float(m.group(1))
    else:
        m = _AGE_YEARS_RE.search(text) or _BARE_AGE_RE.search(text)
        if m:
            age_years = float(m.group(1))

    sex = None
    for pattern, value in _SEX_WORDS:
        if pattern.search(text):
            sex = value
            break

    return Identity(name=name, age_years=age_years, age_months=age_months,
                     sex=sex, tier="keyword", heard=text)


# --------------------------------------------------------------------------
# tier 2 -- a model on this machine
# --------------------------------------------------------------------------

def _extract_local(text: str) -> Identity:
    from . import ollama

    ok, why = ollama.available()
    if not ok:
        raise Unavailable(why)

    schema = {
        "type": "object",
        "properties": {
            "name": {"type": ["string", "null"]},
            "age_years": {"type": ["number", "null"]},
            "age_months": {"type": ["number", "null"]},
            "sex": {"type": ["string", "null"], "enum": ["M", "F", "Other", None]},
        },
        "required": ["name", "age_years", "age_months", "sex"],
    }
    try:
        raw = ollama.chat(_SYSTEM, f"Self-introduction:\n{text}", schema)
    except ollama.Unreachable as exc:
        raise Unavailable(str(exc)) from exc

    def num(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    sex = raw.get("sex")
    return Identity(
        name=(raw.get("name") or "").strip() or None,
        age_years=num(raw.get("age_years")),
        age_months=num(raw.get("age_months")),
        sex=sex if sex in ("M", "F", "Other") else None,
        tier="local", heard=text,
    )


# --------------------------------------------------------------------------
# tier 1 -- hosted
# --------------------------------------------------------------------------

def extract_identity(text: str, *, use_model: bool = True) -> Identity:
    """One spoken self-introduction in, name/age/sex out. Walks hosted ->
    local -> regex exactly like extract.seed(), and for the same reason: try
    the best available model, degrade honestly, never guess past what was
    actually said."""
    text = (text or "").strip()
    if not text:
        return Identity(tier="none", heard=text)
    if not use_model or not _HAVE_PYDANTIC:
        return _regex_fallback(text)

    try:
        client = _client()
    except Unavailable:
        return _fall_back(text)

    try:
        response = client.messages.parse(
            model=MODEL, max_tokens=512,
            system=_SYSTEM,
            messages=[{"role": "user", "content": f"Self-introduction:\n{text}"}],
            output_format=IdentityFields,
        )
        f: IdentityFields = response.parsed_output
        return Identity(
            name=(f.name or "").strip() or None,
            age_years=f.age_years, age_months=f.age_months,
            sex=f.sex if f.sex in ("M", "F", "Other") else None,
            tier="hosted", heard=text,
        )
    except Exception:
        return _fall_back(text)


def _fall_back(text: str) -> Identity:
    try:
        return _extract_local(text)
    except Unavailable:
        return _regex_fallback(text)
