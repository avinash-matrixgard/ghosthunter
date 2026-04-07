"""Evidence chain.

Each piece of evidence is a compressed observation produced by Sonnet from
a command's output. Evidence is linked to the hypotheses it supports or
refutes, with a numeric weight that drives confidence updates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

EvidenceRelation = Literal["supports", "refutes", "neutral"]


@dataclass
class Evidence:
    """A single observation derived from a command result.

    Attributes
    ----------
    id:
        Stable ID like "E1", "E2", ... assigned by the EvidenceChain.
    summary:
        Sonnet's compressed text. This is what Opus actually reads.
    command:
        The command that produced the underlying output.
    relations:
        Mapping of hypothesis ID -> (relation, weight). Opus declares
        these as it interprets the evidence. Weight is 0–100 and
        represents how strongly this evidence shifts confidence.
    created_at:
        Wall-clock timestamp for the audit log.
    """

    id: str
    summary: str
    command: str
    relations: dict[str, tuple[EvidenceRelation, int]] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)

    def supports(self, hypothesis_id: str) -> bool:
        rel = self.relations.get(hypothesis_id)
        return rel is not None and rel[0] == "supports"

    def refutes(self, hypothesis_id: str) -> bool:
        rel = self.relations.get(hypothesis_id)
        return rel is not None and rel[0] == "refutes"

    def weight_for(self, hypothesis_id: str) -> int:
        rel = self.relations.get(hypothesis_id)
        return rel[1] if rel else 0


class EvidenceChain:
    """Append-only collection of evidence with auto-incrementing IDs."""

    def __init__(self) -> None:
        self._items: list[Evidence] = []

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def add(
        self,
        summary: str,
        command: str,
        relations: dict[str, tuple[EvidenceRelation, int]] | None = None,
    ) -> Evidence:
        evidence = Evidence(
            id=f"E{len(self._items) + 1}",
            summary=summary,
            command=command,
            relations=relations or {},
        )
        self._items.append(evidence)
        return evidence

    def get(self, evidence_id: str) -> Evidence | None:
        for e in self._items:
            if e.id == evidence_id:
                return e
        return None

    def all_for(self, hypothesis_id: str) -> list[Evidence]:
        return [
            e
            for e in self._items
            if e.supports(hypothesis_id) or e.refutes(hypothesis_id)
        ]
