"""The audit trail, and clinician overrides.

Two things the Round 2 brief asks for directly:

    "Clinical accountability and liability mean any recommendation must remain
     reviewable and overridable by a licensed clinician, with a clear audit
     trail and compliance with health-data regulation."

    "Capture at least one clinician override and show what the system logs."

JURISDICTION: UK. UK GDPR plus the Data Protection Act 2018, health data as
special category under Article 9. Lawful basis for the care record is Article
6(1)(e) public task with Article 9(2)(h) health and social care -- NOT consent,
which is the common mistake: a patient cannot meaningfully refuse the basis on
which they are being treated, so consent is the wrong instrument here. Consent
is recorded separately, and only for things that genuinely are optional, like
recording audio.

That choice of jurisdiction determines what an override has to record. It is not
enough to log "a nurse changed it". The record needs an identifiable clinician, a
timestamp, what was recommended, what it was changed to, why, and the state of
the system at that moment -- otherwise nobody reviewing an incident can tell
whether the tool misled the clinician or the clinician overruled a correct call.

TAMPER EVIDENCE. Each event carries the hash of the one before it, so altering
history after the fact breaks the chain and `verify()` says so. That is not the
same as tamper-PROOF -- anyone who can rewrite the whole ledger can recompute
every digest. It makes silent edits detectable, which is what an audit trail is
actually for.

IMMUTABILITY. The CTAS manual is explicit:

    "The triage nurse documents the reassessment findings and any changes in the
     patient's acuity score, however, THE INITIAL TRIAGE SCORE IS NEVER CHANGED."

So nothing here mutates. Re-scoring appends, overriding appends, and the first
decision stays legible forever.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

from .core import Category

GENESIS = "0" * 64


class EventKind(StrEnum):
    OBSERVED = "observed"        # a check was answered, and by what source
    DECIDED = "decided"          # the engine produced a category
    SHOWN = "shown"              # the recommendation was put in front of a clinician
    ACCEPTED = "accepted"        # clinician agreed
    OVERRIDDEN = "overridden"    # clinician changed it -- see Direction
    RESCORED = "rescored"        # re-assessment produced a new category
    ESCALATED = "escalated"      # the system raised priority on its own
    CONSENT = "consent"          # separately recorded, and refusable


class Direction(StrEnum):
    """Which way an override went.

    The distinction is the whole point. The system may raise a priority on its
    own; it may never lower one. So every de-escalation in the ledger is, by
    construction, a human decision -- and those are the ones an incident review
    will want to find quickly.
    """

    ESCALATION = "escalation"        # clinician made it MORE urgent
    DE_ESCALATION = "de-escalation"  # clinician made it LESS urgent


class ReasonCode(StrEnum):
    """Structured reasons, so override patterns are countable rather than prose.

    Override RATE, broken down by reason, is the adoption metric: it says whether
    staff trust the tool, and a spike in one reason says which part is wrong.
    """

    CLINICAL_JUDGEMENT = "clinical_judgement"      # looks worse/better than the score
    INFORMATION_MISSING = "information_missing"    # tool did not know something
    INFORMATION_WRONG = "information_wrong"        # tool had it wrong
    PROTOCOL_EXCEPTION = "protocol_exception"      # local policy differs
    RESOURCE_CONSTRAINT = "resource_constraint"    # no bay, no staff
    OTHER = "other"


def _digest(seq: int, at: str, kind: str, actor: str, payload: dict, prev: str) -> str:
    blob = json.dumps(
        {"seq": seq, "at": at, "kind": kind, "actor": actor,
         "payload": payload, "prev": prev},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    at: str
    kind: EventKind
    actor: str
    payload: dict
    prev_hash: str
    digest: str

    def recompute(self) -> str:
        return _digest(self.seq, self.at, self.kind, self.actor, self.payload, self.prev_hash)

    def __str__(self) -> str:
        return f"[{self.seq:03d}] {self.at}  {self.kind:<12} {self.actor:<18} {self.payload}"


@dataclass
class Ledger:
    """Append-only. There is deliberately no update or delete."""

    patient_ref: str
    events: list[Event] = field(default_factory=list)

    # ------------------------------------------------------------------ write

    def append(self, kind: EventKind, actor: str, **payload) -> Event:
        prev = self.events[-1].digest if self.events else GENESIS
        seq = len(self.events)
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        event = Event(
            seq=seq, at=at, kind=kind, actor=actor,
            payload=payload, prev_hash=prev,
            digest=_digest(seq, at, kind, actor, payload, prev),
        )
        self.events.append(event)
        return event

    def record_decision(self, decision, *, evidence: list[str] | None = None) -> Event:
        c = getattr(decision, "confidence", None)
        return self.append(
            EventKind.DECIDED,
            actor="system",
            category=decision.category.name,
            label=decision.category.label,
            fired=decision.fired,
            fired_on=list(decision.fired_on),
            from_vitals=decision.from_vitals,
            # Flattened to plain values. An audit record has to be legible years
            # later, by someone without this codebase to unpickle it against.
            confidence=(
                {
                    "band": c.band,
                    "worst_case": c.worst_case.label,
                    "p_assigned": round(c.p_assigned, 4),
                    "unresolved": c.unresolved,
                }
                if c is not None
                else None
            ),
            evidence=evidence or [],
        )

    def record_override(
        self,
        *,
        clinician: str,
        recommended: Category,
        chosen: Category,
        reason: ReasonCode,
        note: str = "",
        decision=None,
        evidence: list[str] | None = None,
    ) -> Event:
        """What a UK clinical audit actually needs to see.

        `clinician` must identify a person. "nurse" or "staff" is not an audit
        record -- if an incident review cannot name who made the call, the trail
        has failed at the one job it has.
        """
        if not clinician or clinician.lower() in {"nurse", "staff", "clinician", "unknown"}:
            raise ValueError(
                f"override needs an identifiable clinician, got {clinician!r} -- "
                "an unattributable override is not an audit record"
            )
        if recommended is chosen:
            raise ValueError("that is an acceptance, not an override")

        # Lower Category value means more urgent, so a smaller number is an escalation.
        direction = (
            Direction.ESCALATION if chosen < recommended else Direction.DE_ESCALATION
        )

        return self.append(
            EventKind.OVERRIDDEN,
            actor=clinician,
            recommended=recommended.name,
            recommended_label=recommended.label,
            chosen=chosen.name,
            chosen_label=chosen.label,
            direction=direction.value,
            bands_moved=abs(int(recommended) - int(chosen)),
            reason=reason.value,
            note=note,
            # The state at the moment of the decision, not now. Without this you
            # cannot tell whether the tool misled the clinician or was overruled.
            system_said=(
                {
                    "fired": decision.fired,
                    "fired_on": list(decision.fired_on),
                    "from_vitals": decision.from_vitals,
                }
                if decision is not None
                else None
            ),
            evidence_at_the_time=evidence or [],
        )

    # ------------------------------------------------------------------- read

    def verify(self) -> tuple[bool, str]:
        """Recompute the chain. Detects edits, insertions and deletions."""
        prev = GENESIS
        for i, e in enumerate(self.events):
            if e.seq != i:
                return False, f"sequence break at {i}: event claims seq {e.seq}"
            if e.prev_hash != prev:
                return False, f"chain break at {i}: expected prev {prev[:12]}, got {e.prev_hash[:12]}"
            if e.recompute() != e.digest:
                return False, f"event {i} has been altered since it was written"
            prev = e.digest
        return True, f"{len(self.events)} events, chain intact"

    def of_kind(self, kind: EventKind) -> list[Event]:
        return [e for e in self.events if e.kind is kind]

    @property
    def overrides(self) -> list[Event]:
        return self.of_kind(EventKind.OVERRIDDEN)

    @property
    def de_escalations(self) -> list[Event]:
        """Every one of these is a human decision, by construction."""
        return [e for e in self.overrides if e.payload["direction"] == Direction.DE_ESCALATION]

    @property
    def initial_decision(self) -> Event | None:
        """Never overwritten -- the manual is explicit about that."""
        decided = self.of_kind(EventKind.DECIDED)
        return decided[0] if decided else None

    def override_rate(self) -> float:
        shown = len(self.of_kind(EventKind.SHOWN))
        return len(self.overrides) / shown if shown else 0.0

    def render(self) -> str:
        return "\n".join(str(e) for e in self.events)


def override_metrics(ledgers: list[Ledger]) -> dict:
    """Fleet-wide adoption signal.

    Near-zero override rate is not success -- it usually means staff have stopped
    reading and are clicking accept, which is the automation-bias failure. A rate
    that climbs in one reason code says which part of the tool is wrong.
    """
    shown = sum(len(l.of_kind(EventKind.SHOWN)) for l in ledgers)
    overrides = [e for l in ledgers for e in l.overrides]
    by_reason: dict[str, int] = {}
    for e in overrides:
        by_reason[e.payload["reason"]] = by_reason.get(e.payload["reason"], 0) + 1
    de_esc = sum(1 for e in overrides if e.payload["direction"] == Direction.DE_ESCALATION)
    return {
        "shown": shown,
        "overrides": len(overrides),
        "override_rate": len(overrides) / shown if shown else 0.0,
        "de_escalations": de_esc,
        "escalations": len(overrides) - de_esc,
        "by_reason": dict(sorted(by_reason.items(), key=lambda kv: -kv[1])),
    }


# UK GDPR / DPA 2018 posture, stated as data so it can be shown rather than claimed.
DATA_PROTECTION = {
    "jurisdiction": "UK -- UK GDPR + Data Protection Act 2018",
    "special_category": "Article 9 health data",
    "lawful_basis": "Art 6(1)(e) public task + Art 9(2)(h) health and social care",
    "consent": (
        "NOT the lawful basis for the care record. Recorded separately and only "
        "for genuinely optional processing, such as capturing audio, which must "
        "be refusable without losing access to triage."
    ),
    "audio": "discarded after extraction; only the cited spans are retained",
    "retention": "audit events retained per the NHS records retention schedule",
    "egress": (
        "the engine runs locally and never calls out. With the local model tier "
        "no narrative leaves the building at all. EXCEPTION: a session in an "
        "Indian language sends that patient's audio and text to Sarvam AI "
        "(api.sarvam.ai, a third-party cloud) for transcription, translation "
        "and speech; English sessions never do. Retention there is set per "
        "workspace in Sarvam's dashboard, not by this system."
    ),
    "override_record": (
        "identifiable clinician, timestamp, recommended vs chosen, direction, "
        "structured reason, and the system state at that moment"
    ),
}
