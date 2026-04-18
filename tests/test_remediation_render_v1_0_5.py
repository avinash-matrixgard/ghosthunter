"""Regression tests for v1.0.5 fix-first conclusion layout.

Motivation: on the customer Cloud DNS investigation, the tool produced a
real root cause but the user reported that (a) the recommendations
felt like an afterthought buried below a long prose block and (b) the
commands weren't easy to copy-paste. We:

1. Reordered the conclusion so the "What to do now" block renders
   first.
2. Extended the recommendations schema so each item can be an object
   with urgency / description / command / verification — strings are
   still accepted for back-compat.
3. Render structured recommendations with the same paste-safe ASCII
   command block used for mid-investigation commands.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

import pytest
from rich.console import Console


# ---------------------------------------------------------------------------
# Small shims so the renderer can run without the full Investigator stack
# ---------------------------------------------------------------------------
@dataclass
class _FakeBudget:
    max_commands: int = 15
    max_cost_usd: float = 1.0
    max_seconds: float = 600.0
    commands_used: int = 4
    cost_used_usd: float = 0.05
    seconds_used: float = 120.0


@dataclass
class _FakeSpike:
    service: str = "Cloud DNS"
    current_cost: float = 499.0
    previous_cost: float = 44.0


@dataclass
class _FakeResult:
    succeeded: bool = True
    conclusion: dict[str, Any] | None = None
    aborted_reason: str | None = None
    budget: _FakeBudget = field(default_factory=_FakeBudget)
    spike: _FakeSpike = field(default_factory=_FakeSpike)


def _render_to_string(result: _FakeResult) -> str:
    """Run cli._render_result against a Rich Console backed by a StringIO."""
    from ghosthunter import cli as cli_mod

    buf = io.StringIO()
    fake_console = Console(
        file=buf,
        force_terminal=False,
        width=120,
        legacy_windows=False,
    )
    original = cli_mod.console
    cli_mod.console = fake_console
    try:
        cli_mod._render_result(result)
    finally:
        cli_mod.console = original
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fix-first layout
# ---------------------------------------------------------------------------
class TestLayoutOrder:
    def _output_with_full_conclusion(self) -> str:
        return _render_to_string(
            _FakeResult(
                conclusion={
                    "root_cause": "External cache-busting queries on example.com",
                    "confidence": 90,
                    "evidence_summary": [
                        "200 sampled log entries all target example.com",
                        "97 distinct case permutations",
                    ],
                    "not_verified": [
                        "Exact query count per day",
                        "Whether other zones are also targeted",
                    ],
                    "recommendations": [
                        {
                            "urgency": "immediate",
                            "description": "Block attack traffic at the edge",
                            "command": "gcloud compute security-policies create acme-dns-block --description='DDoS block'",
                        },
                    ],
                }
            )
        )

    def test_recommendations_appear_before_root_cause(self):
        out = self._output_with_full_conclusion()
        idx_fix = out.find("What to do now")
        idx_root = out.find("Root cause")
        assert idx_fix != -1, "missing 'What to do now' header"
        assert idx_root != -1, "missing 'Root cause' header"
        assert idx_fix < idx_root, (
            "recommendations should render BEFORE root cause in v1.0.5 "
            "(fix-first layout)"
        )

    def test_evidence_comes_after_root_cause(self):
        out = self._output_with_full_conclusion()
        idx_root = out.find("Root cause")
        idx_ev = out.find("Evidence")
        assert idx_root < idx_ev, "Evidence must render after Root cause"

    def test_not_verified_comes_last(self):
        out = self._output_with_full_conclusion()
        idx_ev = out.find("Evidence")
        idx_not_verified = out.find("couldn't verify")
        assert idx_ev < idx_not_verified, "'What we couldn't verify' is last"


# ---------------------------------------------------------------------------
# Structured recommendation rendering
# ---------------------------------------------------------------------------
class TestStructuredRecommendations:
    def _render_recs(self, recs: list) -> str:
        return _render_to_string(
            _FakeResult(
                conclusion={
                    "root_cause": "test",
                    "confidence": 80,
                    "recommendations": recs,
                }
            )
        )

    def test_urgency_labels_shown(self):
        out = self._render_recs([
            {"urgency": "immediate", "description": "Do the thing NOW"},
            {"urgency": "this_week", "description": "Schedule it"},
            {"urgency": "this_month", "description": "Plan a project"},
            {"urgency": "monitoring", "description": "Set an alert"},
        ])
        assert "NOW" in out
        assert "THIS WEEK" in out
        assert "THIS MONTH" in out
        assert "MONITORING" in out

    def test_commands_render_in_plain_ascii(self):
        """The whole point of the v1.0.5 change — copy-paste a command
        from a recommendation must produce exactly that command, with
        no Unicode border or wrap breakage."""
        cmd = (
            "gcloud compute security-policies rules create 1000 "
            "--security-policy=acme-block --action=deny-403 "
            "--description='block example DNS flooders'"
        )
        out = self._render_recs([
            {
                "urgency": "immediate",
                "description": "Drop flood traffic at the edge",
                "command": cmd,
            }
        ])
        assert cmd in out, (
            "command was mangled in remediation rendering — "
            "copy-paste would break"
        )
        # No box-drawing chars anywhere in the output.
        banned = "│─╭╮╰╯┃━┏┓┗┛┌┐└┘"
        offenders = [c for c in banned if c in out]
        assert not offenders, (
            f"remediation output contains Unicode borders: {offenders}"
        )

    def test_verification_command_rendered_separately(self):
        verify = "gcloud compute security-policies describe acme-block --format=json"
        out = self._render_recs([
            {
                "urgency": "immediate",
                "description": "Create the policy",
                "command": "gcloud compute security-policies create acme-block",
                "verification": verify,
            }
        ])
        assert "Run this command" in out
        assert "Verify with" in out
        assert verify in out

    def test_description_without_command_still_renders(self):
        """Opus is explicitly told to omit ``command`` when it's not
        sure of the exact syntax — the description stands alone."""
        out = self._render_recs([
            {
                "urgency": "this_week",
                "description": "Open a ticket with GCP support about the upstream resolver pattern",
            },
        ])
        assert "ticket with GCP support" in out
        # No "Run this command" header since no command.
        assert "Run this command" not in out

    def test_urgency_sorted_canonically(self):
        """Regardless of the order Opus emits them, we render in
        immediate → week → month → monitoring order."""
        out = self._render_recs([
            {"urgency": "monitoring", "description": "Z monitoring task"},
            {"urgency": "this_month", "description": "Y monthly task"},
            {"urgency": "immediate", "description": "A urgent task"},
            {"urgency": "this_week", "description": "B weekly task"},
        ])
        ia = out.find("A urgent task")
        ib = out.find("B weekly task")
        iy = out.find("Y monthly task")
        iz = out.find("Z monitoring task")
        assert 0 <= ia < ib < iy < iz, (
            f"urgency ordering wrong: immediate={ia}, week={ib}, "
            f"month={iy}, monitoring={iz}"
        )


# ---------------------------------------------------------------------------
# Legacy string-shape back-compat
# ---------------------------------------------------------------------------
class TestLegacyStringRecommendations:
    def test_plain_string_recommendations_still_render(self):
        """Pre-v1.0.5 conclusions used a list of strings. Any existing
        audit log or on-disk artifact must still render."""
        out = _render_to_string(
            _FakeResult(
                conclusion={
                    "root_cause": "something",
                    "confidence": 75,
                    "recommendations": [
                        "Enable Cloud DNS response policy zones",
                        "Export metrics to Cloud Monitoring",
                    ],
                }
            )
        )
        assert "response policy zones" in out
        assert "Export metrics" in out
        assert "What to do now" in out

    def test_mixed_string_and_object_shapes(self):
        out = _render_to_string(
            _FakeResult(
                conclusion={
                    "root_cause": "mixed case",
                    "confidence": 75,
                    "recommendations": [
                        {
                            "urgency": "immediate",
                            "description": "Run the canonical fix",
                            "command": "gcloud foo bar",
                        },
                        "Open a ticket with vendor support",
                    ],
                }
            )
        )
        assert "Run the canonical fix" in out
        assert "gcloud foo bar" in out
        assert "Open a ticket with vendor support" in out


# ---------------------------------------------------------------------------
# Authenticity rule — tool schema doesn't bake in scenario-specific content
# ---------------------------------------------------------------------------
def test_schema_does_not_contain_scenario_specific_content():
    """Defence against accidentally hardcoding pattern shortcuts into
    the tool schema (see CLAUDE.md authenticity rule)."""
    from ghosthunter.models.reasoner import INVESTIGATION_TOOL

    schema_text = str(INVESTIGATION_TOOL).lower()
    forbidden_fragments = [
        "bigquery",
        "cloud dns",
        "nat gateway",
        "g5.4xlarge",
        "example",
        "scenario",
    ]
    offenders = [f for f in forbidden_fragments if f in schema_text]
    assert not offenders, (
        f"INVESTIGATION_TOOL schema contains scenario-specific strings: "
        f"{offenders}. Authenticity rule requires the schema stay generic."
    )
