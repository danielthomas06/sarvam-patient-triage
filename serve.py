"""Local web app. The engine, live, over HTTP.

    python serve.py            then open http://localhost:8000

Unlike the published artifact, which replays generated data, this runs the real
`decide()` on whatever you type. Type something nobody scripted and watch the
engine handle it -- or fail to, which is more useful.

Standard library only. No framework, no build step, nothing to install: the
project's "runs bare" property is worth more than the convenience of FastAPI.

TWO ROLES ON ONE SCREEN, because that is the actual workflow. The patient types
free text and the nurse records observations, and they are not the same input:
what a patient says is narrative to be extracted with evidence, while what a
nurse sees is an observation recorded directly. The screen keeps them apart so
the provenance of every check stays visible.
"""

from __future__ import annotations

import itertools
import json
import mimetypes
import os
import pathlib
import re
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from triage import (
    Answer, BeliefState, Category, Evidence, Source,
    decide, choose_next, keyword_seed, keyword_parse, parse, plausible_set, rank, render,
    seed, should_stop,
)
from triage.audit import EventKind, Ledger, ReasonCode
from triage.protocols.ctas import (
    PROTOCOL, PAIN_LOCALITY, OBSTETRIC_GYNAE_ONLY, excludes_obstetric_gynae,
)
from triage.monitor import BED_COUNT, REASSESS_MINUTES, Board
from triage.cohort import Cohort, Patient, paediatric_vital_category
from triage.cohort import resolve as cohort_resolve
from triage.observations import Measured, derive, news2_from
from triage.pain import FLAT_LADDER, resolve_pain
from triage.flacc import (
    FLACC_CATEGORIES, FLACC_DESCRIPTIONS, MINIMUM_SELF_REPORT_PAIN_AGE,
    SELF_REPORT_PAIN_CHECKS, flacc_score,
)
from triage.actions import first_line_actions
from triage import ollama as _ollama
from triage import local_stt as _stt
from triage import identity_extract as _identity
from triage import vision as _vision
from triage import sarvam as _sarvam
from triage import question_bank as _qbank

WEB = pathlib.Path(__file__).parent / "web"

# Modification time of this file when the process started. The page compares it
# against its own and warns if they differ, because a stale server answering
# "no such route" for a route that exists in the source is indistinguishable
# from a bug until you think to check.
BUILD = int(pathlib.Path(__file__).stat().st_mtime)
PORT = int(os.environ.get("TRIAGE_PORT", "8000"))

# Extraction tier. The model ladder is the default because the keyword fallback
# is a safety net, not a product: it reads phrasing, not meaning. The page can
# switch to it when a local model's latency makes a live demo unwatchable.
USE_MODEL = True


# A tiny stand-in for a hospital records system. The Round 2 brief assumes
# roughly half of arrivals have a prior record and half do not, and the half
# without are the harder ones -- so the demo has to be able to show both.
KNOWN_PATIENTS = {
    "NHS-448-201-7734": {
        "name": "Margaret Doyle", "age": 68, "sex": "F",
        "history": ["hypertension", "type 2 diabetes"],
        "medications": ["ramipril", "metformin"],
        "flags": [],
    },
    "NHS-991-044-2210": {
        "name": "Alan Whitfield", "age": 81, "sex": "M",
        "history": ["atrial fibrillation", "heart failure"],
        "medications": ["bisoprolol", "apixaban"],
        # Both of these change how the rest of the encounter must be read.
        "flags": ["anticoagulated", "rate-limiting medication"],
    },
    "NHS-330-812-5567": {
        "name": "Priya Raman", "age": 34, "sex": "F",
        "history": ["asthma"], "medications": ["salbutamol"], "flags": [],
    },
}


class Encounter:
    """One patient, from arrival to a decision."""

    def __init__(self, ref: str, age: float | None = None, *,
                 name: str = "", sex: str = "", patient_id: str = "", origin: str = "kiosk"):
        self.ref = ref
        self.origin = origin
        """'kiosk' (self-checked-in, nobody's reviewed it) or 'nurse' (a
        clinician is the one typing, right now, on their own screen -- it
        does not belong in a "not yet reviewed" queue, it's already being
        reviewed by definition). Set once at creation from who actually
        called /api/patient/new; queue_rows() reads it to keep a nurse's own
        blank in-progress walk-in from showing up in their own intake list."""
        self.lock = threading.Lock()
        """Guards this ONE patient's own state, and only this one. A model
        call can sit mid-request for 10-15 seconds; before per-encounter
        locking, that held a single global lock, so a second patient on a
        second kiosk couldn't so much as record a keystroke until the first
        one's model call returned. See GLOBAL_LOCK's own comment for the
        other half of this -- the brief, separate lock for the session
        directory and the shared board, never held across a model call."""
        self.age = age
        self.name = name
        self.sex = sex
        self.patient_id = patient_id
        self.record: dict | None = None
        self.belief = BeliefState(PROTOCOL)
        self.weights: dict[str, float] = {}
        self.transcript: list[str] = []
        """Always English -- it is what the extraction models read and what
        their verbatim-quote guard checks against. For a non-English patient
        it is the TRANSLATION; their own words live in transcript_native."""
        self.transcript_native: list[str] = []
        """Parallel to `transcript`: the patient's original words where a
        translation happened, "" where there was nothing to translate. Kept so
        a mistranslation is visible to the nurse, not just silently believed."""
        self.language = _sarvam.ENGLISH
        self.untranslated = False
        """True once any of this patient's words reached extraction UNtranslated
        (translator down). Sticky on purpose: that earlier text stays unread
        even if a later sentence translates fine. The keyword tier cannot read
        Hindi at all, so such a patient can look Non-urgent purely because the
        system understood nothing -- the nurse queue flags it rather than let a
        quiet BLUE stand in for "nobody has read this"."""
        self.ledger = Ledger(ref)
        self.tier = "none"
        self.rejected: list[str] = []
        self.fallbacks: list[str] = []
        self.asked: dict[str, str] = {}       # check id -> question we asked
        self.pending: str | None = None       # check id awaiting a reply
        self.override: dict | None = None
        self.vitals = Measured()
        self.looks_unwell = False
        """Nurse gestalt, not derivable from any number. The CTAS appendix is
        explicit that normal vitals in a child who looks ill may mean a
        pre-arrest state -- so this has to be something a nurse asserts
        directly, never inferred."""
        self.paediatric_reasons: list[str] = []
        """Set by decision() -- why the paediatric vital floor landed where it
        did, including the appendix's own warning when vitals are normal but
        the child looks unwell. Empty for a non-paediatric cohort."""
        self.started = time.time()
        self.photos: list[dict] = []
        """Each: {id, mime, bytes, caption, candidate_checks, status, tier}.
        A photo's caption and candidate_checks are a MODEL'S PROPOSAL, exactly
        like a question's "why" is -- see add_photo()/confirm_photo(). Nothing
        here reaches the belief state until confirm_photo() is called, which
        requires a nurse's click, the same rule that holds everywhere else a
        model gets near a finding. status is 'pending' | 'confirmed' | 'rejected'."""

    # ---- writes ----------------------------------------------------------

    def _sync_board(self) -> None:
        """If this patient is already on the waiting board, push whatever
        just changed into Board.observe() -- re-scores immediately and clears
        an overdue re-check, rather than waiting for the periodic tick to
        notice on its own. This is what makes the nurse dashboard's
        "Reassess" action mean something: opening an admitted patient's
        record and recording a fresh observation on it now actually reaches
        the board, which previously had no path back to it at all.
        """
        with GLOBAL_LOCK:
            if self.ref in board.patients:
                now = (time.time() - BOARD_START) / 60.0
                board.observe(self.ref, self.belief, now)

    def add_photo(self, image: bytes, mime: str) -> dict:
        """A photo just arrived. Analyse it (if a vision model is reachable)
        and store the proposal -- caption and candidate check -- alongside
        the image itself, but record nothing yet."""
        result = _vision.analyze_photo(image, mime, use_model=USE_MODEL)
        entry = {
            "id": f"ph{len(self.photos) + 1}", "mime": mime, "bytes": image,
            "caption": result.caption, "candidate_checks": list(result.candidate_checks),
            "status": "pending", "tier": result.tier,
            "available": result.available, "detail": result.detail,
        }
        self.photos.append(entry)
        self.ledger.append(EventKind.OBSERVED, "system", check="photo",
                           value=entry["id"], quote=result.caption or result.detail)
        return entry

    def _find_photo(self, photo_id: str) -> dict | None:
        return next((p for p in self.photos if p["id"] == photo_id), None)

    def confirm_photo(self, photo_id: str, check_id: str | None) -> dict | None:
        """A nurse looked at the photo and the proposal, and agrees. Only now
        does anything reach the belief state -- via the same observe() a
        typed staff finding uses, so a photo-sourced check is indistinguishable
        downstream from one a nurse typed directly."""
        entry = self._find_photo(photo_id)
        if entry is None:
            return None
        entry["status"] = "confirmed"
        if check_id and check_id in PROTOCOL.discriminators:
            self.observe(check_id, Answer.TRUE, f"confirmed from photo: {entry['caption']}")
        return entry

    def reject_photo(self, photo_id: str) -> dict | None:
        entry = self._find_photo(photo_id)
        if entry is None:
            return None
        entry["status"] = "rejected"
        return entry

    def set_language(self, code: str) -> None:
        """Which language this patient speaks and is spoken to in. Changes how
        their words reach the extraction models (translated) and how the
        system's questions reach them (reviewed wording, translated, spoken);
        never what the engine does with the resulting findings."""
        if not _sarvam.is_supported(code):
            raise ValueError(f"unsupported language {code!r}")
        self.language = code
        self.ledger.append(EventKind.OBSERVED, "reception", check="language",
                           value=code, quote=_sarvam.LANGUAGES[code]["english"])

    def _to_english(self, text: str) -> tuple[str, str, str | None]:
        """(english, original_if_translated, fallback_note).

        Plain ASCII, or any text in an English session, passes through
        untouched. Otherwise it is translated -- and the original is kept and
        returned so the caller can store it next to the translation. If the
        translator is unreachable the patient's own words go to extraction
        as-is, with a note: a model can still read some Hindi, and the quote
        guard still holds against whatever text it was actually given, which
        beats silently dropping what the patient said.
        """
        if self.language == _sarvam.ENGLISH or not _sarvam.needs_translation(text):
            return text, "", None
        try:
            english = _sarvam.translate(text, self.language, _sarvam.ENGLISH)
        except _sarvam.Unavailable as exc:
            self.untranslated = True
            return text, "", (f"translation unavailable ({exc}) -- extraction used the "
                              f"patient's original words")
        self.ledger.append(EventKind.OBSERVED, "system", check="translation",
                           value=self.language, quote=f"{text} => {english}")
        return english, text, None

    def _question_native(self, check_id: str, english: str) -> str | None:
        """The question in the patient's language: the reviewed bank first, a
        live (cached) translation only if the bank has nothing, None if neither
        works -- in which case the kiosk can only show the English."""
        banked = _qbank.lookup(check_id, self.language, english)
        if banked:
            return banked
        try:
            return _sarvam.translate(english, _sarvam.ENGLISH, self.language)
        except _sarvam.Unavailable:
            return None

    def add_narrative(self, text: str) -> None:
        text, native, note = self._to_english(text)
        self.transcript.append(text)
        self.transcript_native.append(native)
        joined = "\n".join(self.transcript)
        result = seed(joined, PROTOCOL) if USE_MODEL else keyword_seed(joined, PROTOCOL)

        # Merge rather than replace: the nurse may have recorded observations
        # between utterances, and re-extracting must not discard them.
        for did, answer in result.belief.answers.items():
            if not self.belief.is_observed(did):
                self.belief = self.belief.record(
                    did, answer, result.belief.evidence.get(did))
                self.ledger.append(EventKind.OBSERVED, "system",
                                   check=did, value=answer.value,
                                   quote=str(result.belief.evidence.get(did, "")))
        for b, w in result.branch_weights.items():
            self.weights[b] = max(self.weights.get(b, 0.0), w)
        self.tier = result.tier
        self.rejected = list(result.rejected)
        self.fallbacks = list(getattr(result, 'fallbacks', []))
        if note:
            self.fallbacks.append(note)
        self._sync_board()

    def set_identity(self, values: dict) -> dict:
        """Who this is. Age is the field that matters most.

        Age selects the scoring cohort, and an unknown age is a fail-safe rather
        than a default -- `cohort.resolve` withholds the adult thresholds instead
        of quietly applying them to a child.
        """
        self.name = (values.get("name") or "").strip()
        self.sex = (values.get("sex") or "").strip()
        self.patient_id = (values.get("patient_id") or "").strip()

        # Whole years cannot tell a 3-month-old from a newborn -- both would
        # type "0" -- and the paediatric vital tables are banded in months
        # from birth. A separate months field, used only when it matters,
        # rather than forcing every reception screen to think in fractions of
        # a year for the common adult case.
        months_raw = values.get("age_months")
        try:
            months = float(months_raw) if months_raw not in (None, "") else None
        except (TypeError, ValueError):
            months = None
        if months is not None:
            self.age = months / 12.0
        else:
            try:
                self.age = float(values["age"]) if values.get("age") not in (None, "") else None
            except (TypeError, ValueError):
                self.age = None

        # Record lookup. A miss is not an error -- roughly half of arrivals have
        # nothing on file, and those are the harder patients, not the easier ones.
        self.record = KNOWN_PATIENTS.get(self.patient_id)
        if self.record:
            self.name = self.name or self.record["name"]
            self.sex = self.sex or self.record["sex"]
            self.age = self.age if self.age is not None else self.record["age"]

            # Record-sourced checks are free and always taken first: they cost no
            # patient time, and anticoagulation changes how a head injury is read
            # whatever the patient says.
            if "anticoagulated" in self.record.get("flags", []):
                if "anticoagulated" in PROTOCOL.discriminators:
                    self.belief = self.belief.record(
                        "anticoagulated", Answer.TRUE,
                        Evidence(", ".join(self.record["medications"]), "record"))
                    self.ledger.append(EventKind.OBSERVED, "record",
                                       check="anticoagulated", value="true",
                                       quote=", ".join(self.record["medications"]))
        self.ledger.append(EventKind.OBSERVED, "reception",
                           check="identity", value="recorded",
                           quote=f"{self.name or 'unidentified'}, "
                                 f"age {self.age if self.age is not None else 'unknown'}")
        return self.cohort_view()

    def _patient(self) -> Patient:
        """One Patient object, built the same way everywhere it's needed --
        cohort classification and the paediatric vitals floor must never see
        two different versions of who this is."""
        return Patient(
            age_years=self.age,
            rate_limiting_meds=bool(self.record and
                "rate-limiting medication" in self.record.get("flags", [])),
            frail=bool(self.age and self.age >= 75),
            looks_unwell=self.looks_unwell,
        )

    def cohort_view(self) -> dict:
        """What the age stratification makes of this patient."""
        a = cohort_resolve(self._patient())
        return {
            "cohort": str(a.cohort),
            "adult_score_applies": a.may_score_vitals,
            "warnings": list(a.warnings),
        }

    def observe(self, check_id: str, answer: Answer, note: str) -> None:
        """A nurse observation. No model involved, and it outranks extraction."""
        self.belief = self.belief.record(check_id, answer, Evidence(note, "staff"))
        self.ledger.append(EventKind.OBSERVED, "staff",
                           check=check_id, value=answer.value, quote=note)
        self._sync_board()

    def record_vitals(self, values: dict) -> None:
        """Numbers off a monitor, turned into checks.

        A measured value settles its checks in BOTH directions: SpO2 97% is real
        evidence the low-saturation checks are false, not merely unasked. That is
        the one place a negative is admissible without a patient denying
        anything, because a measurement is not a memory.
        """
        def num(key):
            v = values.get(key)
            if v in (None, ""):
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        alert = values.get("alert")
        if "looks_unwell" in values:
            self.looks_unwell = bool(values.get("looks_unwell"))
        self.vitals = Measured(
            respiratory_rate=num("respiratory_rate"), spo2=num("spo2"),
            on_oxygen=bool(values.get("on_oxygen")), systolic_bp=num("systolic_bp"),
            pulse=num("pulse"), temperature=num("temperature"),
            alert=None if alert is None else bool(alert),
            pain_score=int(num("pain_score")) if num("pain_score") is not None else None,
            age_years=self.age,
        )

        cohort = cohort_resolve(self._patient())
        for check_id, answer, why in derive(
            self.vitals, PROTOCOL, pain_locality=self._pain_locality(),
            adult_thresholds=cohort.may_score_vitals,
        ):
            self.belief = self.belief.record(check_id, answer, Evidence(why, "vitals"))
            self.ledger.append(EventKind.OBSERVED, "vitals",
                               check=check_id, value=answer.value, quote=why)
        self._sync_board()

    def record_flacc(self, values: dict) -> None:
        """A staff-observed pain score for a patient too young to self-report
        -- see triage/flacc.py for why this exists and where it's from.

        Always resolved as 'central': the manual is explicit that paediatric
        pain isn't split by body region, so this doesn't consult
        _pain_locality() the way the self-report ladder does.
        """
        try:
            scores = {cat: int(values[cat]) for cat in FLACC_CATEGORIES}
        except (KeyError, TypeError, ValueError):
            return   # incomplete or malformed -- record nothing rather than guess
        score = flacc_score(**scores)
        why = f"FLACC {score}/10 (" + ", ".join(f"{k}={v}" for k, v in scores.items()) + ")"
        for check_id, positive in resolve_pain(PROTOCOL, score, "central"):
            outcome = Answer.TRUE if positive else Answer.FALSE
            self.belief = self.belief.record(check_id, outcome, Evidence(why, "staff"))
            self.ledger.append(EventKind.OBSERVED, "staff",
                               check=check_id, value=outcome.value, quote=why)
        self._sync_board()

    # The pain ladder is one question with several checks behind it -- three
    # for the illustrative pack's flat severity scale, six for CTAS's, which
    # splits every band by body-region locality (see triage/pain.py for why
    # that split is a safety property, not a display choice). Either way, one
    # number settles the whole thing rather than asking "how bad is the pain"
    # once per band.
    PAIN_CHECK_IDS = frozenset(FLAT_LADDER) | {
        f"{band}_pain_{loc}" for band in ("severe", "moderate", "mild")
        for loc in ("central", "peripheral")
    }

    @staticmethod
    def _pain_score(reply: str) -> int | None:
        # Lookarounds rather than word boundaries: "8/10" and "a 5." both parse,
        # and the 1 in "10" cannot be read as a separate score.
        m = re.search("(?<![0-9])(10|[0-9])(?![0-9])", reply)
        return int(m.group(1)) if m else None

    def _pain_locality(self) -> str:
        """Which body-region bucket the leading branch's pain checks fall
        under. Ignored entirely for a pack with a flat (non-localised) pain
        ladder; defaults to 'central' under genuine ambiguity, which is the
        safer direction -- see PAIN_LOCALITY in protocols/ctas.py.

        Paediatric patients always get 'central', full stop, regardless of
        leading branch -- the manual is explicit: "the Paediatrics guidelines
        do not distinguish between central and peripheral pain" (Sec 2.4.2).
        """
        if cohort_resolve(self._patient()).is_paediatric:
            return "central"
        leading = max(self.weights.items(), key=lambda kv: kv[1], default=None)
        if leading is None:
            return "central"
        return PAIN_LOCALITY.get(leading[0], "central")

    def answer(self, check_id: str, reply: str) -> str:
        # parse()/keyword_parse() and the denial guard only know English words
        # for "no" and "not" -- a Tamil "இல்லை" would read as an unclear
        # answer, or worse. Translate first; the ledger keeps both, so the
        # record shows what was said as well as what was understood.
        reply, native, note = self._to_english(reply)
        said = f"{reply} [said: {native}]" if native else reply
        if note:
            self.fallbacks.append(note)

        if check_id in self.PAIN_CHECK_IDS:
            score = self._pain_score(reply)
            if score is not None:
                self.pending = None
                for cid, positive in resolve_pain(PROTOCOL, score, self._pain_locality()):
                    outcome = Answer.TRUE if positive else Answer.FALSE
                    # Not added to `asked`: they were settled, not asked, and
                    # charging three questions to the budget for one utterance
                    # cut the interview short. next_step skips them anyway --
                    # it tests `is_observed`, which these now are.
                    self.belief = self.belief.record(
                        cid, outcome, Evidence(reply, "patient"))
                    self.ledger.append(EventKind.OBSERVED, "patient",
                                       check=cid, value=outcome.value,
                                       quote=f"{said} (scored {score}/10)")
                return "true" if score >= 1 else "false"
            # No number in the reply -- fall through and let the parser try.

        # Respect the tier here too. This used to call `parse` unconditionally,
        # so on the keyword tier every reply paid for a model round trip that
        # could not succeed -- ~10s of latency to reach the same answer a regex
        # gives instantly.
        if USE_MODEL:
            question = self.asked.get(check_id, PROTOCOL.discriminators[check_id].question)
            outcome, evidence = parse(check_id, question, reply, PROTOCOL)
        else:
            outcome, evidence = keyword_parse(reply)
        self.pending = None
        if outcome is Answer.UNKNOWN:
            return "unclear"
        self.belief = self.belief.record(check_id, outcome, evidence)
        self.ledger.append(EventKind.OBSERVED, "patient",
                           check=check_id, value=outcome.value, quote=said)
        return outcome.value

    def record_override(self, clinician: str, category: str, reason: str, note: str) -> None:
        d = self.decision()
        chosen = Category[category]
        self.ledger.record_override(
            clinician=clinician, recommended=d.category, chosen=chosen,
            reason=ReasonCode(reason), note=note, decision=d,
            evidence=self.belief.trace(),
        )
        self.override = {"by": clinician, "category": chosen.name}

    def accept(self, clinician: str = "rn.k.mensah") -> None:
        """Accept the recommendation and admit the patient to the board.

        Idempotent -- a repeated call (a double-click, a retried request)
        finds `self.ref` already on the board and does not hand out a second
        bed for the same patient.
        """
        d = self.decision()
        clinician = (clinician or "rn.k.mensah").strip()
        self.ledger.append(EventKind.SHOWN, "system", to=clinician)
        self.ledger.append(EventKind.ACCEPTED, clinician, category=d.category.name)

        # board/BOARD_RECORDS are shared across every patient, not just this
        # one -- GLOBAL_LOCK here, on top of the enc.lock the caller already
        # holds. Safe under the ordering rule stated on GLOBAL_LOCK itself:
        # acquiring it while already holding one specific enc.lock never
        # deadlocks, only the reverse would.
        with GLOBAL_LOCK:
            if self.ref not in board.patients:
                now = (time.time() - BOARD_START) / 60.0
                board.admit(self.ref, d.category, self.belief, self.plausible, now=now)
                BOARD_RECORDS[self.ref] = self.state()
                if self.photos:
                    BOARD_PHOTOS[self.ref] = list(self.photos)

    # ---- reads -----------------------------------------------------------

    @property
    def plausible(self) -> frozenset[str]:
        if not self.weights:
            return frozenset(PROTOCOL.branches)
        return plausible_set(self.weights)

    def decision(self):
        """The category, with the age-appropriate vital-sign score (if any)
        acting as a floor -- never a ceiling; a belief-driven finding can
        still outrank it.

        Adults get NEWS2, self-gated by `news2_from` to a complete set of
        five readings. Paediatric cohorts get `paediatric_vital_category`
        instead: scored against age-banded normal ranges from birth, per the
        CTAS appendix, because NEWS2 is not validated under 16. Neither path
        runs for the other cohort -- that boundary is `may_score_vitals`,
        computed once so cohort_view() and this cannot disagree about it.
        """
        cohort = cohort_resolve(self._patient())
        vitals_score = None
        floor = None
        self.paediatric_reasons = []
        if cohort.cohort is Cohort.ADULT and cohort.may_score_vitals:
            vitals_score = news2_from(self.vitals)
        elif cohort.is_paediatric:
            floor, self.paediatric_reasons = paediatric_vital_category(
                self._patient(),
                resp_rate=self.vitals.respiratory_rate, pulse=self.vitals.pulse,
            )
        return decide(self.belief, self.plausible, vitals=vitals_score, floor=floor)

    SHORTLIST_SIZE = 6
    """How many VOI-ranked candidates choose_next() gets to reason over. Not
    the whole vocabulary -- a pre-filtered, already relevance- and
    cost-adjusted shortlist, so the model is refining a vetted order with
    context the pure math can't see, not roaming free."""

    def _asked_log(self) -> dict[str, dict]:
        """What's already been asked and answered, for choose_next() to read
        so it doesn't repeat itself. Only checks actually settled -- an
        asked-but-unclear reply leaves nothing here to contradict."""
        return {
            cid: {"question": q, "answer": self.belief.answers[cid].value}
            for cid, q in self.asked.items()
            if cid in self.belief.answers and self.belief.is_observed(cid)
        }

    def next_step(self) -> dict | None:
        """What to acquire next, and who acquires it.

        Walks the whole ranking rather than stopping at the top. A saturation
        probe usually outranks a question -- it is cheap and settles several
        checks -- but it is not something to ask a patient, and returning it
        alone left the panel showing a label with no question and the loop
        stuck there. So: collect everything for staff or a device as pending,
        and return the best thing the PATIENT can actually answer.

        Among the questions a patient CAN answer, `rank()` supplies a
        shortlist rather than a single winner; choose_next() picks among
        them with the conversation in view. It cannot choose anything
        outside that shortlist, and it never touches urgency -- same
        boundary as everywhere else a model writes into this system.
        """
        stop = should_stop(self.belief, self.plausible, len(self.asked))
        if stop:
            return {"check": None, "text": None, "actor": None, "question": None,
                    "why": None, "value": None, "pending": [], "stopped": stop}

        pending: list[dict] = []
        shortlist: list[tuple[str, float]] = []
        for cand in rank(self.belief, self.plausible,
                          branch_weights=self.weights, limit=14):
            d = PROTOCOL.discriminators[cand.discriminator_id]
            if d.id in self.asked or self.belief.is_observed(d.id):
                continue

            # Genuine exclusion, not a deprioritisation -- see
            # OBSTETRIC_GYNAE_ONLY's own docstring in protocols/ctas.py for
            # why this is the one check in the pack that works this way, and
            # why it fires on sex OR an age too young for pregnancy to be a
            # real consideration -- blank/unknown age still asks.
            if d.id in OBSTETRIC_GYNAE_ONLY and excludes_obstetric_gynae(self.sex, self.age):
                continue

            # A pre-verbal or too-young patient cannot self-report a number
            # or describe where pain spreads -- CTAS's own answer for this
            # is FLACC, a staff-observed scale, not a patient question. See
            # triage/flacc.py for the citation and the age threshold's own
            # honesty note. Rerouted to pending, not skipped -- the finding
            # still matters, it just needs a different assessor.
            if (d.id in SELF_REPORT_PAIN_CHECKS and self.age is not None
                    and self.age < MINIMUM_SELF_REPORT_PAIN_AGE):
                if len(pending) < 4:
                    pending.append({
                        "check": d.id, "text": d.text,
                        "actor": "staff observation (FLACC)",
                        "value": round(cand.value, 3),
                    })
                continue

            if d.source is not Source.ASK:
                if len(pending) < 4:
                    pending.append({
                        "check": d.id, "text": d.text,
                        "actor": ("device or assistant" if d.source is Source.MEASURE
                                  else "record lookup" if d.source is Source.RECORD
                                  else "staff observation"),
                        "value": round(cand.value, 3),
                    })
                continue

            if d.source is Source.SENSITIVE:
                continue          # never asked by a kiosk; staff, in private

            shortlist.append((d.id, cand.value))
            if len(shortlist) >= self.SHORTLIST_SIZE:
                break

        if not shortlist:
            # Nothing left the patient can answer -- but staff may still have work.
            return {"check": None, "text": None, "actor": None, "question": None,
                    "why": None, "value": None, "pending": pending, "stopped": None}

        check_id, top_value = shortlist[0]
        question, why = None, ""

        if USE_MODEL:
            chosen = choose_next(
                [cid for cid, _ in shortlist], self.transcript, self._asked_log(),
                PROTOCOL, age=self.age,
            )
            if chosen:
                check_id, question, why = chosen

        value = next((v for cid, v in shortlist if cid == check_id), top_value)
        d = PROTOCOL.discriminators[check_id]
        if question is None:
            # choose_next() unavailable or its pick didn't survive the guard
            # -- fall back to rank()'s own top pick with reviewed wording,
            # exactly today's behaviour.
            question = render(check_id, PROTOCOL, age=self.age) if USE_MODEL else d.question

        # Non-English session: the model may still have helped choose WHICH
        # check to ask (that is a ranking, and the ranking is guarded), but
        # the words are always the protocol's reviewed English -- the leading-
        # question vetting that protects a generated English question cannot
        # be run on a Tamil sentence, so a generated one is never translated
        # and spoken to a patient. See triage/question_bank.py.
        question_native = None
        if self.language != _sarvam.ENGLISH:
            question = d.question
            question_native = self._question_native(check_id, question)

        self.asked[check_id] = question
        self.pending = check_id
        return {
            "check": check_id, "text": d.text, "actor": "patient",
            "question": question, "why": why, "value": round(value, 3),
            "pending": pending,
            "language": self.language, "question_native": question_native,
        }

    def state(self) -> dict:
        d = self.decision()
        c = d.confidence
        checks = []
        for did in sorted(self.belief.answers):
            answer = self.belief.answers[did]
            if answer is Answer.UNKNOWN:
                continue
            ev = self.belief.evidence.get(did)
            checks.append({
                "id": did,
                "text": PROTOCOL.discriminators[did].text,
                "positive": answer is Answer.TRUE,
                "origin": ev.origin if ev else "unknown",
                "quote": ev.quote if ev else "",
            })
        return {
            "ref": self.ref,
            "build": BUILD,
            "name": self.name,
            "sex": self.sex,
            "age": self.age,
            "patient_id": self.patient_id,
            "record": self.record,
            "cohort": self.cohort_view(),
            "tier": self.tier,
            "use_model": USE_MODEL,
            "transcript": self.transcript,
            "transcript_native": self.transcript_native,
            "language": self.language,
            "untranslated": self.untranslated,
            "checks": checks,
            "rejected": self.rejected[:6],
            "fallbacks": self.fallbacks[:3],
            "branches": sorted(
                ({"id": b, "name": PROTOCOL.branches[b].name, "weight": round(w, 2)}
                 for b, w in self.weights.items()),
                key=lambda x: -x["weight"])[:6],
            # The leading branch's first-line actions, if the table covers it. A
            # small weight threshold keeps a barely-mentioned branch from
            # suggesting orders for a complaint the patient hasn't really got.
            "actions": (lambda leading: (
                {"branch": PROTOCOL.branches[leading[0]].name,
                 "items": first_line_actions(leading[0], d.category)}
                if leading and leading[1] >= 0.15
                   and first_line_actions(leading[0], d.category)
                else None
            ))(max(self.weights.items(), key=lambda kv: kv[1], default=None)),
            "category": d.category.name,
            "label": d.category.label,
            "target": d.category.target_minutes,
            "fired": PROTOCOL.discriminators[d.fired].text if d.fired else None,
            # A general discriminator sits on every branch, so listing them all
            # says nothing -- "Severe pain, on: Abdominal pain, Back pain,
            # Shortness of breath..." for a headache reads as a bug even though
            # it is accurate. Name the branches only when the check is specific
            # to a few, which is exactly when it is worth knowing.
            "fired_on": (
                [PROTOCOL.branches[b].name for b in d.fired_on]
                if 0 < len(d.fired_on) <= max(2, len(PROTOCOL.branches) // 3)
                else []
            ),
            "fired_general": len(d.fired_on) > max(2, len(PROTOCOL.branches) // 3),
            "confidence": {
                "band": c.band,
                "worst": c.worst_case.label,
                "could_escalate": c.could_escalate,
                "unresolved": c.unresolved,
                "text": str(c),
            },
            "vitals": self.vitals.recorded(),
            "looks_unwell": self.looks_unwell,
            "news2": (lambda sc: {"total": sc.total, "band": sc.band} if sc else None)(
                news2_from(self.vitals)),
            "paediatric_reasons": self.paediatric_reasons,
            "from_vitals": d.from_vitals,
            "override": self.override,
            "pending": self.pending,
            "ledger": [
                {"seq": e.seq, "kind": str(e.kind), "actor": e.actor,
                 "payload": {k: v for k, v in e.payload.items() if k != "evidence"},
                 "digest": e.digest[:8], "prev": e.prev_hash[:8]}
                for e in self.ledger.events
            ],
            "chain_ok": self.ledger.verify()[0],
            # Whether this encounter has actually been admitted to the board --
            # looked up live rather than cached, because a re-score or a tick
            # can move the entry after Accept was pressed.
            "admitted": self.ref in board.patients,
            "bed": board.patients[self.ref].bed if self.ref in board.patients else None,
            # Metadata only -- no image bytes in a JSON response that gets
            # polled every 20 seconds. The actual image is served separately
            # by GET /api/photo, referenced by (ref, id).
            "photos": [
                {"id": p["id"], "caption": p["caption"],
                 "candidate_checks": p["candidate_checks"], "status": p["status"],
                 "tier": p["tier"], "available": p["available"], "detail": p["detail"]}
                for p in self.photos
            ],
        }


# ---------------------------------------------------------------------------
# a small waiting room alongside, so the board is not empty
# ---------------------------------------------------------------------------

board = Board(PROTOCOL)
BOARD_START = time.time()


def seed_board() -> None:
    def b(*checks):
        st = BeliefState(PROTOCOL)
        for c in checks:
            st = st.record(c, Answer.TRUE, Evidence("recorded at triage", "staff"))
        return st

    # Real CTAS-pack ids -- these three crashed on startup after the switch
    # from the illustrative pack, since "cardiac_pain"/"minor_haemorrhage"/
    # "mild_pain" don't exist in this vocabulary. Caught by actually starting
    # the server, not by reading the diff.
    pl = plausible_set({"chest_pain": 0.6, "abdominal_pain": 0.4})
    board.admit("bed-02 chest pain", Category.ORANGE, b("pain_radiating"), pl, now=-52)
    board.admit("bed-05 laceration", Category.GREEN, b(), pl, now=-46)
    board.admit("bed-09 ankle", Category.GREEN, b("mild_pain_peripheral"), pl, now=-190)


def board_state() -> dict:
    now = (time.time() - BOARD_START) / 60.0
    board.tick(now)
    patients = []
    for w in board.ranked(now):
        interval = REASSESS_MINUTES[w.current]
        rec = BOARD_RECORDS.get(w.ref)
        patients.append({
            "ref": w.ref,
            "bed": w.bed,
            "name": rec["name"] if rec else None,
            "category": w.current.name,
            "label": w.current.label,
            "raised_from": w.initial.label if w.current != w.initial else None,
            "waited": round(w.waited(now)),
            "due_in": round(w.due_in(now)),
            "interval": interval,
            "starved": w.flagged_starved,
            "has_record": rec is not None,
        })
    return {"capacity": BED_COUNT, "free_beds": board.free_beds(), "patients": patients}


def queue_rows() -> list[dict]:
    """Nurse-facing: every kiosk session in progress, not the post-accept
    waiting board (that's board_state()) -- this is "who has a kiosk
    mid-conversation right now, and does any of them need looking at before
    they finish". Critical sessions sort first.

    Each row is read under that patient's OWN lock, briefly -- never
    GLOBAL_LOCK for more than the initial snapshot, so building this list
    cannot stall an in-progress kiosk conversation.
    """
    with GLOBAL_LOCK:
        # Once accepted, a patient belongs to the waiting board (board_state()),
        # not the "not yet reviewed" list -- without this exclusion, an
        # admitted kiosk patient's Encounter (which is never removed from
        # ENCOUNTERS, only reset when the SAME kiosk terminal starts its next
        # patient) kept showing up here indefinitely after being seen.
        sessions = [e for e in ENCOUNTERS.values()
                    if e.origin == "kiosk" and e.ref not in board.patients]
    now = time.time()
    rows = []
    for e in sessions:
        with e.lock:
            d = e.decision()
            stopped = should_stop(e.belief, e.plausible, len(e.asked))
            rows.append({
                "ref": e.ref, "name": e.name or None,
                "age": e.age, "sex": e.sex or None,
                "waited_minutes": round((now - e.started) / 60, 1),
                "tier": e.tier,
                "checks_recorded": len(e.belief.answers),
                "category": d.category.name, "label": d.category.label,
                "critical": d.category <= Category.ORANGE,
                "stopped": stopped,
                "language": e.language, "untranslated": e.untranslated,
            })
    rows.sort(key=lambda r: (not r["critical"], r["waited_minutes"]))
    return rows


# Full clinical detail for the board's click-through, keyed the same as
# Board.patients. Separate from Waiting on purpose: Waiting is what the
# monitor module needs to re-score and re-check, not a place to grow patient
# demographics and a transcript. A snapshot taken at Accept -- it does not
# track further changes to the encounter, which by then has usually been
# reset for the next patient anyway.
BOARD_RECORDS: dict[str, dict] = {}

# The one thing BOARD_RECORDS deliberately does NOT hold: raw image bytes.
# state() only ever puts photo METADATA into BOARD_RECORDS (via the ordinary
# snapshot below) -- this is the separate byte store, snapshotted the same
# way and at the same moment, so a photo stays viewable after Accept even
# though the Encounter that captured it usually gets reset for the next
# kiosk patient right after.
BOARD_PHOTOS: dict[str, list[dict]] = {}


ENCOUNTERS: dict[str, Encounter] = {}

GLOBAL_LOCK = threading.Lock()
"""Guards the session directory (ENCOUNTERS) and the shared board /
BOARD_RECORDS -- nothing patient-specific. Held BRIEFLY: a dict lookup, a
dict insert, a bed assignment. Never held across a model call -- that's
what each Encounter's own `.lock` is for (see Encounter.__init__).

Lock ordering, stated once so it stays obvious: a handler may acquire
GLOBAL_LOCK while already holding a specific enc.lock (looking up a patient,
then later touching the shared board on their behalf). Nothing may acquire
an enc.lock while holding GLOBAL_LOCK -- reversing that would deadlock the
first patient who touches the board against the session lookup for a
second. `new_patient()` and `_lookup()` below only ever take GLOBAL_LOCK
alone, precisely to keep that one-directional.
"""

_next_ref = itertools.count(1)


def new_patient(origin: str = "kiosk") -> Encounter:
    """Start a fresh session. Every call gets its own Encounter with its own
    lock -- this is the whole reason multiple patients on different kiosks
    no longer share state or block on each other's model calls.

    `origin` distinguishes a kiosk self-check-in from a nurse's own manual
    walk-in entry -- see Encounter.origin for why that matters to the
    intake queue."""
    with GLOBAL_LOCK:
        ref = f"ED-{int(time.time()) % 100000:05d}-{next(_next_ref)}"
        enc = Encounter(ref, origin=origin)
        ENCOUNTERS[ref] = enc
        return enc


def _lookup(ref: str) -> Encounter | None:
    with GLOBAL_LOCK:
        return ENCOUNTERS.get(ref)


class NoSuchPatient(Exception):
    """The ref a kiosk sent doesn't resolve to a live session -- most likely
    the server restarted (ENCOUNTERS is in-memory only, nothing here
    survives a process restart) or the session was already finished. Either
    way the honest response is "start over", not a crash."""
    def __init__(self, ref: str):
        self.ref = ref


@contextmanager
def session(ref: str):
    """Look up one patient and hold ONLY their lock for the duration --
    never GLOBAL_LOCK, so a slow model call on this patient cannot block
    anyone else's kiosk. Raises NoSuchPatient for an unknown/expired ref
    rather than silently falling back to someone else's encounter, which is
    the mistake the old single-slot current() made structurally impossible
    to avoid."""
    enc = _lookup(ref)
    if enc is None:
        raise NoSuchPatient(ref)
    with enc.lock:
        yield enc


# ---------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------

def transcribe_audio(audio: bytes, lang: str) -> tuple[int, dict]:
    """(http status, json body) for POST /api/transcribe.

    English keeps its existing, fully local path (Vosk): nothing about an
    English patient's audio leaves the machine just because a Sarvam key
    happens to be set. Any other language goes to Sarvam, because there is no
    local model for it -- and if Sarvam can't do it, that's an honest 503
    the kiosk turns into "please type", not a guess.
    """
    if lang != _sarvam.ENGLISH:
        if not audio:
            return 400, {"text": "", "error": "no audio received"}
        if not _sarvam.is_supported(lang):
            return 400, {"text": "", "error": f"language {lang!r} isn't offered"}
        try:
            return 200, {"text": _sarvam.transcribe(audio, lang), "language": lang,
                         "engine": "sarvam"}
        except _sarvam.Unavailable as exc:
            return 503, {"text": "", "error": str(exc)}

    ok, detail = _stt.available()
    if not ok:
        return 503, {"text": "", "error": f"local transcription unavailable -- {detail}"}
    if not audio:
        return 400, {"text": "", "error": "no audio received"}
    text = _stt.transcribe_wav(audio)
    if text is None:
        return 400, {"text": "", "error": "audio wasn't readable as mono 16-bit PCM WAV"}
    return 200, {"text": text, "language": lang, "engine": "vosk"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):        # quiet; the console is for the demo
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload, default=str).encode(), "application/json")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _no_such_patient(self, exc: "NoSuchPatient") -> None:
        # ENCOUNTERS is in-memory only -- a server restart, or a ref that was
        # never issued, both land here. The honest response is "start a new
        # session", not a 500 or a silent fallback to someone else's.
        return self._json(
            {"error": f"no session for ref {exc.ref!r} -- it may have finished, "
                      "or the server restarted. Start a new patient.",
             "no_such_patient": True}, 404)

    # ---- routes ----------------------------------------------------------

    def _ref(self) -> str:
        return (parse_qs(urlsplit(self.path).query).get("ref") or [""])[0]

    def _qs(self, name: str) -> str:
        return (parse_qs(urlsplit(self.path).query).get(name) or [""])[0]

    def do_GET(self):
        # The page URL now carries ?patient=REF (so a reload resumes the
        # same kiosk session) -- self.path is the whole "/?patient=..."
        # string, which the old exact match against "/" never matched, so
        # the query string silently 404'd the entire page. Caught by
        # actually reloading with a ref in the URL, not by reading the diff.
        route_path = urlsplit(self.path).path
        if route_path in ("/", "/index.html"):
            return self._serve_file("index.html")

        if self.path.startswith("/api/state"):
            try:
                with session(self._ref()) as enc:
                    st = enc.state()
            except NoSuchPatient as exc:
                return self._no_such_patient(exc)
            with GLOBAL_LOCK:
                return self._json({"encounter": st, "board": board_state()})

        if self.path.startswith("/api/next"):
            try:
                with session(self._ref()) as enc:
                    return self._json({"next": enc.next_step(), "encounter": enc.state()})
            except NoSuchPatient as exc:
                return self._no_such_patient(exc)

        if self.path.startswith("/api/queue"):
            return self._json(queue_rows())

        if self.path.startswith("/api/languages"):
            # Only offer what can actually work: with no Sarvam key there is no
            # way to hear, translate or speak a non-English language, and a
            # picker full of dead options is worse than no picker.
            offered = [c for c in _sarvam.LANGUAGES
                       if c == _sarvam.ENGLISH or _sarvam.configured()]
            return self._json({
                "sarvam": _sarvam.configured(),
                "languages": [{"code": c, **_sarvam.LANGUAGES[c]} for c in offered],
            })

        if self.path.startswith("/api/targets"):
            return self._json({c.name: c.target_minutes for c in Category})

        if self.path.startswith("/api/config"):
            # Which backend is ACTUALLY in use, resolved at import. Config read
            # from the environment is invisible otherwise, and "it feels slow"
            # is impossible to diagnose without seeing whether the remote host
            # was ever reached. Times a real round trip rather than guessing.
            reachable, detail = _ollama.available()
            probe_ms = None
            if reachable:
                t0 = time.perf_counter()
                try:
                    _ollama.chat(
                        "Reply with the JSON {\"ok\": true}.", "ping",
                        {"type": "object", "properties": {"ok": {"type": "boolean"}},
                         "required": ["ok"]},
                    )
                    probe_ms = round((time.perf_counter() - t0) * 1000)
                except Exception as exc:
                    detail = f"reachable but the call failed: {exc}"
                    reachable = False
            vosk_ok, vosk_detail = _stt.available()
            sarvam_ok, sarvam_detail = _sarvam.available()
            return self._json({
                "sarvam_configured": sarvam_ok,
                "sarvam_detail": sarvam_detail,
                "question_bank_reviewed": _qbank.reviewed(),
                "hosted_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "hosted_model": os.environ.get("TRIAGE_MODEL", "claude-opus-5"),
                "ollama_host": _ollama.HOST,
                "ollama_model": _ollama.MODEL,
                "ollama_rich_prompt": _ollama.RICH_PROMPT,
                "ollama_reachable": reachable,
                "ollama_detail": detail,
                "ollama_probe_ms": probe_ms,
                "use_model": USE_MODEL,
                "vosk_available": vosk_ok,
                "vosk_detail": vosk_detail,
                "build": BUILD,
            })

        if self.path.startswith("/api/checks"):
            return self._json([
                {"id": d.id, "text": d.text, "source": d.source.value}
                for d in sorted(PROTOCOL.discriminators.values(), key=lambda d: d.text)
                if d.source in (Source.OBSERVE, Source.MEASURE, Source.RECORD)
            ])

        if self.path.startswith("/api/patient"):
            ref = self._ref()
            with GLOBAL_LOCK:
                now = (time.time() - BOARD_START) / 60.0
                w = board.patients.get(ref)
                if w is None:
                    return self._json(
                        {"error": f"no patient on the board with ref {ref!r}"}, 404)
                rec = BOARD_RECORDS.get(ref)
                # The three seeded demo beds never went through a real encounter,
                # so there is no snapshot for them -- say that plainly rather than
                # inventing detail the board never had.
                payload = dict(rec) if rec else {
                    "ref": ref, "name": None, "age": None, "sex": None,
                    "patient_id": None, "checks": [], "transcript": [],
                    "note": "Seeded demo patient -- admitted directly onto the "
                            "board, no encounter record behind it.",
                }
                payload["bed"] = w.bed
                payload["category"] = w.current.name
                payload["label"] = w.current.label
                payload["waited"] = round(w.waited(now))
                payload["starved"] = w.flagged_starved
                return self._json(payload)

        if self.path.startswith("/api/photo"):
            ref, photo_id = self._ref(), self._qs("id")
            # Live encounter first (still mid-conversation, not yet accepted);
            # BOARD_PHOTOS second (accepted, and the kiosk's Encounter was
            # probably reset for the next patient already) -- this is the
            # only reason that second store exists.
            enc = _lookup(ref)
            entries = enc.photos if enc else BOARD_PHOTOS.get(ref, [])
            entry = next((p for p in entries if p["id"] == photo_id), None)
            if entry is None:
                return self._json({"error": "no such photo"}, 404)
            return self._send(200, entry["bytes"], entry["mime"])

        return self._serve_file(self.path.lstrip("/"))

    def do_POST(self):
        global USE_MODEL

        # Raw audio, not JSON -- has to be special-cased before self._body()
        # runs json.loads on the request, which a WAV file is not.
        if self.path.startswith("/api/transcribe"):
            length = int(self.headers.get("Content-Length") or 0)
            audio = self.rfile.read(length) if length else b""
            status, payload = transcribe_audio(audio, self._qs("lang") or _sarvam.ENGLISH)
            return self._json(payload, status)

        # Raw image bytes, not JSON -- same reason as /api/transcribe above.
        # ref travels in the query string (?ref=...) since there is no JSON
        # body to carry it in. Exact path match, not startswith: that would
        # also swallow /api/photo/confirm and /api/photo/reject below, whose
        # ref travels in a JSON body instead.
        if urlsplit(self.path).path == "/api/photo":
            length = int(self.headers.get("Content-Length") or 0)
            image = self.rfile.read(length) if length else b""
            mime = self.headers.get("Content-Type") or "image/jpeg"
            if not image:
                return self._json({"error": "no image received"}, 400)
            try:
                with session(self._ref()) as enc:
                    entry = enc.add_photo(image, mime)
            except NoSuchPatient as exc:
                return self._no_such_patient(exc)
            return self._json({
                "id": entry["id"], "caption": entry["caption"],
                "candidate_checks": entry["candidate_checks"], "status": entry["status"],
                "tier": entry["tier"], "available": entry["available"], "detail": entry["detail"],
            })

        body = self._body()

        # These three don't need an existing session -- they CREATE one, or
        # they're global settings, not patient state.
        if self.path == "/api/patient/new":
            origin = body.get("origin") if body.get("origin") in ("kiosk", "nurse") else "kiosk"
            enc = new_patient(origin=origin)
            return self._json({"ref": enc.ref, **enc.state()})

        if self.path == "/api/extract_identity":
            # Stateless -- no ref, doesn't touch an Encounter. The kiosk calls
            # this once, right after a single spoken self-introduction, and
            # fills its own form fields with whatever comes back; nothing
            # here is recorded until the patient hits Continue.
            text = (body.get("text") or "").strip()
            english = text
            language = body.get("language") or _sarvam.ENGLISH
            if language != _sarvam.ENGLISH and _sarvam.needs_translation(text):
                # A name comes back in Latin letters, which is what the record
                # and the nurse screen use; the patient still sees and can fix
                # every field it fills in before anything is submitted.
                try:
                    english = _sarvam.translate(text, language, _sarvam.ENGLISH)
                except _sarvam.Unavailable:
                    pass          # extract from the original; may well find nothing
            result = _identity.extract_identity(english, use_model=USE_MODEL)
            return self._json({
                "name": result.name, "age": result.age_years,
                "age_months": result.age_months, "sex": result.sex,
                "tier": result.tier, "heard": text,
            })

        if self.path == "/api/tts":
            # Speaks a question in the patient's language. Tied to a live
            # session so this can't be used as an open text-to-speech proxy
            # that spends the Sarvam account's credits and rate limit.
            ref = body.get("ref", "")
            if _lookup(ref) is None:
                return self._no_such_patient(NoSuchPatient(ref))
            try:
                wav = _sarvam.synthesize(body.get("text") or "", body.get("language") or "")
            except _sarvam.Unavailable as exc:
                return self._json({"error": str(exc)}, 503)
            return self._send(200, wav, "audio/wav")

        if self.path == "/api/tier":
            with GLOBAL_LOCK:
                USE_MODEL = bool(body.get("use_model"))
            return self._json({"use_model": USE_MODEL})

        if self.path == "/api/reset":
            # "New patient" at a physical kiosk: whoever was using it is
            # done, so their ref is abandoned (not merged into, not reused --
            # a fresh Encounter, so nothing of the last patient can leak into
            # the next one), and this same terminal gets a new one.
            old_ref = body.get("ref", "")
            if old_ref:
                with GLOBAL_LOCK:
                    ENCOUNTERS.pop(old_ref, None)
            # /api/reset is only ever called from the nurse dashboard's own
            # "New patient" button (kiosk.js never calls it -- a finished
            # kiosk session just gets a fresh /api/patient/new).
            enc = new_patient(origin="nurse")
            return self._json({"ref": enc.ref, **enc.state()})

        # Everything else acts on one specific, already-existing patient.
        try:
            with session(body.get("ref", "")) as enc:
                if self.path == "/api/narrative":
                    text = (body.get("text") or "").strip()
                    if text:
                        enc.add_narrative(text)
                    return self._json(enc.state())

                if self.path == "/api/identity":
                    enc.set_identity(body)
                    return self._json(enc.state())

                if self.path == "/api/language":
                    try:
                        enc.set_language(body.get("language") or "")
                    except ValueError as exc:
                        return self._json({"error": str(exc)}, 400)
                    return self._json(enc.state())

                if self.path == "/api/vitals":
                    enc.record_vitals(body)
                    return self._json(enc.state())

                if self.path == "/api/flacc":
                    enc.record_flacc(body)
                    return self._json(enc.state())

                if self.path == "/api/observe":
                    answer = Answer.TRUE if body.get("positive") else Answer.FALSE
                    enc.observe(body["check"], answer, body.get("note") or "observed by staff")
                    return self._json(enc.state())

                if self.path == "/api/answer":
                    outcome = enc.answer(body["check"], body.get("reply") or "")
                    return self._json({"outcome": outcome, **enc.state()})

                if self.path == "/api/shown":
                    enc.ledger.append(EventKind.SHOWN, "system", to=body.get("to", "staff"))
                    return self._json(enc.state())

                if self.path == "/api/accept":
                    if not enc.belief.answers and not enc.transcript:
                        return self._json(
                            {"error": "nothing recorded yet -- there is no decision to accept"},
                            400)
                    if enc.override:
                        return self._json(
                            {"error": "this encounter was overridden, not accepted"}, 400)

                    enc.accept(body.get("clinician", "rn.k.mensah"))
                    with GLOBAL_LOCK:
                        w = board.patients[enc.ref]
                    return self._json({"bed": w.bed, "capacity": BED_COUNT, **enc.state()})

                if self.path == "/api/override":
                    try:
                        enc.record_override(
                            body.get("clinician", ""), body["category"],
                            body.get("reason", "clinical_judgement"), body.get("note", ""),
                        )
                    except ValueError as exc:
                        # The ledger refuses an unattributable override.
                        # Surfacing the real reason beats a generic 400.
                        return self._json({"error": str(exc)}, 400)
                    return self._json(enc.state())

                if self.path == "/api/photo/confirm":
                    entry = enc.confirm_photo(body.get("id", ""), body.get("check_id"))
                    if entry is None:
                        return self._json({"error": "no such photo"}, 404)
                    return self._json(enc.state())

                if self.path == "/api/photo/reject":
                    entry = enc.reject_photo(body.get("id", ""))
                    if entry is None:
                        return self._json({"error": "no such photo"}, 404)
                    return self._json(enc.state())
        except NoSuchPatient as exc:
            return self._no_such_patient(exc)

        return self._json({"error": "no such route"}, 404)

    def _serve_file(self, name: str) -> None:
        path = (WEB / name).resolve()
        if not path.is_file() or WEB.resolve() not in path.parents:
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), ctype)


def _port_is_taken(port: int) -> bool:
    import socket
    with socket.socket() as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    # Refuse to start quietly on a busy port. Without this the bind fails, this
    # process dies, the OLD server keeps answering, and every edit appears to do
    # nothing -- which has cost more debugging time than any real bug here.
    if _port_is_taken(PORT):
        print("")
        print(f"  Port {PORT} is already in use.")
        print("")
        print("  An older server is still running and will serve STALE code.")
        print("  Free it, or pick another port:")
        print("")
        print(f"    PowerShell:  Get-NetTCPConnection -LocalPort {PORT} -State Listen |"
              " ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }")
        print(f'    or:          $env:TRIAGE_PORT = "{PORT + 1}"; python serve.py')
        print("")
        raise SystemExit(1)

    seed_board()
    print(f"\n  Triage station running at http://localhost:{PORT}")
    print(f"  Protocol: {PROTOCOL.name}")
    print(f"  Extraction: {'model ladder' if USE_MODEL else 'keyword (instant)'}"
          f" -- switch it on the page\n")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
