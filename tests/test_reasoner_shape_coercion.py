"""Unit tests for Opus tool_use payload coercion.

Before v1.0.2 the reasoner crashed with an opaque
``string indices must be integers, not 'str'`` TypeError whenever Opus
returned a hypothesis as a plain string instead of an object. These
tests pin the defensive behaviour: minor shape slips are now coerced,
un-coerceable payloads raise a typed ``ReasonerSchemaError`` that the
investigator catches and retries.

Real-world trigger: running advisor mode on the FOCUS 100K sample and
answering "no access" three times in a row — Opus's 4th response
returned hypotheses as a list of strings.
"""
from __future__ import annotations

import pytest

from ghosthunter.models.reasoner import (
    HypothesisStep,
    InvestigationStep,
    NextAction,
    ReasonerSchemaError,
    _coerce_hypothesis,
    _coerce_next_action,
)


# ---------------------------------------------------------------------------
# _coerce_hypothesis
# ---------------------------------------------------------------------------
class TestCoerceHypothesis:
    def test_canonical_dict_preserved(self):
        raw = {
            "id": "H1",
            "description": "EC2 instances scaled up",
            "confidence": 75,
            "status": "active",
            "evidence_for": ["E1", "E2"],
            "evidence_against": [],
        }
        out = _coerce_hypothesis(raw, idx=0)
        assert out is not None
        assert out.id == "H1"
        assert out.description == "EC2 instances scaled up"
        assert out.confidence == 75
        assert out.status == "active"
        assert out.evidence_for == ["E1", "E2"]

    def test_bare_string_becomes_hypothesis(self):
        """The real-world Opus slip-up — a string where a dict was expected."""
        out = _coerce_hypothesis(
            "H1: EC2 instances in us-east-1 likely upsized", idx=0
        )
        assert out is not None
        assert out.id == "H1"
        assert "EC2 instances" in out.description
        assert out.confidence == 50  # default
        assert out.status == "active"

    def test_empty_string_returns_none(self):
        assert _coerce_hypothesis("", idx=0) is None
        assert _coerce_hypothesis("   ", idx=0) is None

    def test_dict_missing_id_gets_synthesized(self):
        out = _coerce_hypothesis(
            {"description": "NAT gateway spike", "confidence": 60}, idx=2
        )
        assert out is not None
        assert out.id == "H3"  # synthesized from idx

    def test_dict_missing_description_is_unsalvageable(self):
        assert _coerce_hypothesis({"id": "H1", "confidence": 80}, idx=0) is None

    def test_confidence_clamped_to_range(self):
        out = _coerce_hypothesis(
            {"description": "x", "confidence": 9999}, idx=0
        )
        assert out is not None
        assert out.confidence == 100

        out = _coerce_hypothesis(
            {"description": "x", "confidence": -50}, idx=0
        )
        assert out is not None
        assert out.confidence == 0

    def test_confidence_non_numeric_falls_back_to_default(self):
        out = _coerce_hypothesis(
            {"description": "x", "confidence": "high"}, idx=0
        )
        assert out is not None
        assert out.confidence == 50

    def test_invalid_status_coerced_to_active(self):
        out = _coerce_hypothesis(
            {"description": "x", "status": "maybe"}, idx=0
        )
        assert out is not None
        assert out.status == "active"

    def test_non_dict_non_string_returns_none(self):
        assert _coerce_hypothesis(42, idx=0) is None
        assert _coerce_hypothesis(None, idx=0) is None
        assert _coerce_hypothesis(["a", "b"], idx=0) is None

    def test_evidence_lists_coerced_to_strings(self):
        out = _coerce_hypothesis(
            {
                "description": "x",
                "evidence_for": ["E1", 42, None],
            },
            idx=0,
        )
        assert out is not None
        # Non-string evidence entries get str()'d rather than dropped.
        assert out.evidence_for == ["E1", "42", "None"]


# ---------------------------------------------------------------------------
# _coerce_next_action
# ---------------------------------------------------------------------------
class TestCoerceNextAction:
    def test_canonical_command_preserved(self):
        raw = {
            "type": "command",
            "command": "gcloud logging read ...",
            "tests_hypothesis": "H1",
            "rationale": "Sample recent queries",
        }
        out = _coerce_next_action(raw)
        assert out.type == "command"
        assert out.command == "gcloud logging read ..."
        assert out.tests_hypothesis == "H1"

    def test_missing_next_action_falls_back_to_need_info(self):
        out = _coerce_next_action(None, fallback_reasoning="what team owns X?")
        assert out.type == "need_info"
        assert out.rationale == "what team owns X?"

    def test_next_action_as_string_falls_back_to_need_info(self):
        out = _coerce_next_action("run gcloud something")
        assert out.type == "need_info"

    def test_unknown_action_type_falls_back_to_need_info(self):
        # With a fallback_reasoning supplied, that text wins — it's the
        # user-facing voice and we don't want to override it with an
        # internal debug detail.
        out = _coerce_next_action(
            {"type": "frobnicate", "command": "??"},
            fallback_reasoning="(need more info)",
        )
        assert out.type == "need_info"
        assert out.rationale == "(need more info)"

    def test_unknown_action_type_without_fallback_surfaces_detail(self):
        # When no fallback is available, expose the bad type in the
        # rationale so downstream logs / diagnostics can see it.
        out = _coerce_next_action({"type": "frobnicate"}, fallback_reasoning="")
        assert out.type == "need_info"
        assert "frobnicate" in (out.rationale or "")

    def test_conclude_keeps_conclusion_dict(self):
        out = _coerce_next_action(
            {
                "type": "conclude",
                "conclusion": {
                    "root_cause": "runaway BigQuery",
                    "confidence": 90,
                },
            }
        )
        assert out.type == "conclude"
        assert out.conclusion == {
            "root_cause": "runaway BigQuery",
            "confidence": 90,
        }

    def test_conclude_with_non_dict_conclusion_drops_it(self):
        out = _coerce_next_action(
            {"type": "conclude", "conclusion": "it was BigQuery"}
        )
        assert out.type == "conclude"
        assert out.conclusion is None

    def test_non_string_command_field_dropped(self):
        out = _coerce_next_action({"type": "command", "command": 42})
        assert out.type == "command"
        assert out.command is None


# ---------------------------------------------------------------------------
# InvestigationStep.from_tool_input — end-to-end parsing
# ---------------------------------------------------------------------------
class TestFromToolInput:
    def _happy_payload(self) -> dict:
        return {
            "hypotheses": [
                {
                    "id": "H1",
                    "description": "EC2 upsize",
                    "confidence": 70,
                    "status": "active",
                },
                {
                    "id": "H2",
                    "description": "NAT spike",
                    "confidence": 30,
                    "status": "active",
                },
            ],
            "next_action": {
                "type": "command",
                "command": "aws ec2 describe-instances",
                "tests_hypothesis": "H1",
            },
            "reasoning": "Checking the bigger hypothesis first.",
        }

    def test_happy_payload_parses(self):
        step = InvestigationStep.from_tool_input(self._happy_payload())
        assert len(step.hypotheses) == 2
        assert step.next_action.type == "command"
        assert step.reasoning.startswith("Checking")

    def test_hypotheses_as_strings_coerced_not_crashed(self):
        """The bug this whole module exists to prevent."""
        payload = self._happy_payload()
        payload["hypotheses"] = [
            "H1: EC2 upsize is most likely",
            "H2: NAT gateway data processing spike",
        ]
        step = InvestigationStep.from_tool_input(payload)
        assert len(step.hypotheses) == 2
        assert all(isinstance(h, HypothesisStep) for h in step.hypotheses)
        assert step.hypotheses[0].id == "H1"
        assert "EC2" in step.hypotheses[0].description

    def test_mixed_string_and_dict_hypotheses(self):
        payload = self._happy_payload()
        payload["hypotheses"] = [
            "H1: first as string",
            {"id": "H2", "description": "second as dict", "confidence": 40},
        ]
        step = InvestigationStep.from_tool_input(payload)
        assert len(step.hypotheses) == 2
        assert step.hypotheses[0].description.startswith("H1:")
        assert step.hypotheses[1].description == "second as dict"
        assert step.hypotheses[1].confidence == 40

    def test_explicit_empty_hypotheses_is_legal(self):
        """Opus may legitimately return an empty list at conclude time —
        we preserve this over-strict behaviour because the old code did."""
        step = InvestigationStep.from_tool_input(
            {"hypotheses": [], "next_action": {"type": "conclude"}}
        )
        assert step.hypotheses == []
        assert step.next_action.type == "conclude"

    def test_all_hypotheses_unsalvageable_raises(self):
        """Distinct from the empty-list case: Opus sent items but every
        single one was un-coerceable. That's a real shape slip — the
        investigator will retry with a corrective nudge."""
        with pytest.raises(ReasonerSchemaError) as exc_info:
            InvestigationStep.from_tool_input(
                {
                    "hypotheses": [None, 42, ""],
                    "next_action": {"type": "conclude"},
                }
            )
        assert "no valid hypotheses" in str(exc_info.value)

    def test_hypotheses_not_a_list_raises(self):
        with pytest.raises(ReasonerSchemaError) as exc_info:
            InvestigationStep.from_tool_input(
                {"hypotheses": "H1: foo", "next_action": {"type": "conclude"}}
            )
        assert "not a list" in str(exc_info.value)

    def test_missing_next_action_becomes_need_info(self):
        """Opus sometimes drops next_action entirely. We recover with need_info
        rather than crashing — the investigator can still surface the
        reasoning text to the user."""
        payload = self._happy_payload()
        del payload["next_action"]
        step = InvestigationStep.from_tool_input(payload)
        assert step.next_action.type == "need_info"

    def test_top_level_non_dict_raises(self):
        with pytest.raises(ReasonerSchemaError):
            InvestigationStep.from_tool_input("not a dict")  # type: ignore[arg-type]

    def test_schema_error_carries_raw_payload(self):
        bad = {"hypotheses": "oops", "next_action": {"type": "conclude"}}
        with pytest.raises(ReasonerSchemaError) as exc_info:
            InvestigationStep.from_tool_input(bad)
        assert exc_info.value.raw_payload == bad
