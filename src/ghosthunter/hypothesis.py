"""Hypothesis lifecycle: spawn, update confidence, conclude/eliminate.

Confidence is the source of truth. Status is derived:
  >= 85  -> confirmed
  <=  5  -> eliminated
  else   -> active
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

from ghosthunter.evidence import Evidence

HypothesisStatus = Literal["active", "eliminated", "confirmed"]

CONFIRM_THRESHOLD = 85
ELIMINATE_THRESHOLD = 5


@dataclass
class Hypothesis:
    id: str
    description: str
    confidence: int
    status: HypothesisStatus = "active"
    evidence_for: list[str] = field(default_factory=list)
    evidence_against: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.confidence = _clamp(self.confidence)
        self._sync_status()

    def apply_evidence(self, evidence: Evidence) -> None:
        """Update confidence and evidence lists from a single observation."""
        weight = evidence.weight_for(self.id)
        if weight == 0:
            return
        if evidence.supports(self.id):
            self.confidence = _clamp(self.confidence + weight)
            if evidence.id not in self.evidence_for:
                self.evidence_for.append(evidence.id)
        elif evidence.refutes(self.id):
            self.confidence = _clamp(self.confidence - weight)
            if evidence.id not in self.evidence_against:
                self.evidence_against.append(evidence.id)
        self._sync_status()

    def _sync_status(self) -> None:
        # Don't downgrade a manually-set terminal status by mistake.
        if self.confidence >= CONFIRM_THRESHOLD:
            self.status = "confirmed"
        elif self.confidence <= ELIMINATE_THRESHOLD:
            self.status = "eliminated"
        else:
            self.status = "active"


class HypothesisManager:
    """Holds the current set of competing hypotheses (typically 2–4).

    The reasoner is the source of truth for which hypotheses exist and
    what their confidences are at any moment — this manager just stores
    them and applies evidence updates between turns.
    """

    def __init__(self) -> None:
        self._hypotheses: dict[str, Hypothesis] = {}

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._hypotheses)

    def __iter__(self):
        return iter(self._hypotheses.values())

    def get(self, hypothesis_id: str) -> Hypothesis | None:
        return self._hypotheses.get(hypothesis_id)

    def all(self) -> list[Hypothesis]:
        return list(self._hypotheses.values())

    def active(self) -> list[Hypothesis]:
        return [h for h in self._hypotheses.values() if h.status == "active"]

    def confirmed(self) -> list[Hypothesis]:
        return [h for h in self._hypotheses.values() if h.status == "confirmed"]

    def add(
        self,
        description: str,
        confidence: int,
        hypothesis_id: str | None = None,
    ) -> Hypothesis:
        hid = hypothesis_id or self._next_id()
        if hid in self._hypotheses:
            raise ValueError(f"hypothesis {hid} already exists")
        h = Hypothesis(id=hid, description=description, confidence=confidence)
        self._hypotheses[hid] = h
        return h

    def replace_all(self, hypotheses: Iterable[Hypothesis]) -> None:
        """Reset state to a snapshot from the reasoner.

        Used when a new reasoner step returns the full hypothesis list —
        we adopt it wholesale rather than trying to diff.
        """
        self._hypotheses = {h.id: h for h in hypotheses}

    # ------------------------------------------------------------------
    # Evidence application
    # ------------------------------------------------------------------
    def apply_evidence(self, evidence: Evidence) -> None:
        for h in self._hypotheses.values():
            h.apply_evidence(evidence)

    # ------------------------------------------------------------------
    # Termination check
    # ------------------------------------------------------------------
    def should_conclude(self) -> Hypothesis | None:
        """Return a confirmed hypothesis if one exists, else None."""
        for h in self._hypotheses.values():
            if h.status == "confirmed":
                return h
        return None

    def leading(self) -> Hypothesis | None:
        """Highest-confidence active hypothesis (for UI display)."""
        active = self.active()
        if not active:
            return None
        return max(active, key=lambda h: h.confidence)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _next_id(self) -> str:
        return f"H{len(self._hypotheses) + 1}"


def _clamp(value: int) -> int:
    return max(0, min(100, int(value)))
