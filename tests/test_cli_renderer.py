"""CLI event renderer — animated spinner phases + static panels.

Locks down the Claude-Code-style UX added in response to "this looks
frozen while Opus is thinking". The renderer fires a `rich.status.Status`
spinner whenever the backend is doing opaque work and stops it around
anything the user needs to read or respond to.

Spinner phases (covered by `_PHASE_LABELS`):
  step_started     → "Opus is reasoning"
  command_approved → "Sonnet is validating the command"
  command_executed → "Sonnet is compressing command output"

Every other event stops the spinner before rendering its own output.
"""
from __future__ import annotations

import asyncio

import pytest
from rich.console import Console

from ghosthunter.cli import _InvestigationRenderer
from ghosthunter.investigator import InvestigationEvent


@pytest.fixture
def renderer_pair():
    """Return (renderer, console) where the console records everything."""
    console = Console(record=True, width=120, force_terminal=False)
    return _InvestigationRenderer(console), console


async def _feed(renderer, events: list[InvestigationEvent]) -> None:
    for e in events:
        await renderer(e)


# ---------------------------------------------------------------------------
# Spinner lifecycle
# ---------------------------------------------------------------------------
class TestSpinnerLifecycle:
    def test_step_started_starts_spinner(self, renderer_pair):
        renderer, _ = renderer_pair
        asyncio.run(renderer(InvestigationEvent("step_started", {})))
        assert renderer._status is not None, (
            "step_started should start the spinner"
        )
        # Clean up so pytest doesn't leave a Live display running.
        renderer._stop_spin()

    def test_hypotheses_updated_stops_spinner(self, renderer_pair):
        renderer, _ = renderer_pair

        async def go():
            await renderer(InvestigationEvent("step_started", {}))
            await renderer(
                InvestigationEvent(
                    "hypotheses_updated",
                    {
                        "hypotheses": [
                            {
                                "id": "H1",
                                "status": "active",
                                "confidence": 60,
                                "description": "A",
                            }
                        ]
                    },
                )
            )

        asyncio.run(go())
        assert renderer._status is None, (
            "hypotheses_updated should stop the spinner before rendering bars"
        )

    def test_every_thinking_phase_has_a_label(self):
        assert "step_started" in _InvestigationRenderer._PHASE_LABELS
        assert "command_approved" in _InvestigationRenderer._PHASE_LABELS
        assert "command_executed" in _InvestigationRenderer._PHASE_LABELS
        # All labels are non-empty strings — catches typos in constants.
        for kind, label in _InvestigationRenderer._PHASE_LABELS.items():
            assert isinstance(label, str) and label, (
                f"empty label for {kind!r}"
            )

    def test_phase_transition_replaces_spinner(self, renderer_pair):
        """step_started → command_approved should swap the spinner text."""
        renderer, _ = renderer_pair

        async def go():
            await renderer(InvestigationEvent("step_started", {}))
            assert renderer._status is not None
            first_status = renderer._status
            await renderer(InvestigationEvent("command_approved", {"command": "x"}))
            # A new Status object replaces the old one.
            assert renderer._status is not None
            assert renderer._status is not first_status

        asyncio.run(go())
        renderer._stop_spin()

    def test_concluded_stops_spinner(self, renderer_pair):
        renderer, _ = renderer_pair

        async def go():
            await renderer(InvestigationEvent("step_started", {}))
            await renderer(
                InvestigationEvent("concluded", {"conclusion": {"root_cause": "x"}})
            )

        asyncio.run(go())
        assert renderer._status is None

    def test_aborted_stops_spinner(self, renderer_pair):
        renderer, _ = renderer_pair

        async def go():
            await renderer(InvestigationEvent("step_started", {}))
            await renderer(InvestigationEvent("aborted", {"reason": "timeout"}))

        asyncio.run(go())
        assert renderer._status is None


# ---------------------------------------------------------------------------
# Static content rendered to the console
# ---------------------------------------------------------------------------
class TestStaticRenders:
    def test_hypothesis_bars_show_confidence(self, renderer_pair):
        renderer, console = renderer_pair
        asyncio.run(
            renderer(
                InvestigationEvent(
                    "hypotheses_updated",
                    {
                        "hypotheses": [
                            {
                                "id": "H1",
                                "status": "active",
                                "confidence": 78,
                                "description": "NAT gateway egress",
                            },
                            {
                                "id": "H2",
                                "status": "eliminated",
                                "confidence": 3,
                                "description": "Misconfigured resolver",
                            },
                        ]
                    },
                )
            )
        )
        out = console.export_text()
        assert "Hypotheses" in out
        assert "H1" in out and "78%" in out
        assert "NAT gateway egress" in out
        assert "H2" in out and "3%" in out

    def test_reasoning_renders_opus_panel(self, renderer_pair):
        renderer, console = renderer_pair
        asyncio.run(
            renderer(
                InvestigationEvent(
                    "reasoning", {"text": "Evidence favors H1 strongly."}
                )
            )
        )
        out = console.export_text()
        assert "Opus" in out
        assert "Evidence favors H1 strongly." in out

    def test_empty_reasoning_is_silent(self, renderer_pair):
        """Don't render an empty Opus panel — it looks broken."""
        renderer, console = renderer_pair
        asyncio.run(renderer(InvestigationEvent("reasoning", {"text": ""})))
        assert "Opus" not in console.export_text()

    def test_command_blocked_is_red(self, renderer_pair):
        renderer, console = renderer_pair
        asyncio.run(
            renderer(
                InvestigationEvent(
                    "command_blocked",
                    {"layer": "L2", "reason": "not in allowlist"},
                )
            )
        )
        out = console.export_text()
        assert "L2" in out
        assert "not in allowlist" in out

    def test_evidence_added_prints_id_and_summary(self, renderer_pair):
        renderer, console = renderer_pair

        class _Evidence:
            id = "E1"
            summary = "5 NAT gateways found; one owns 78% of VPC traffic."

        asyncio.run(
            renderer(
                InvestigationEvent("evidence_added", {"evidence": _Evidence()})
            )
        )
        out = console.export_text()
        assert "E1" in out
        assert "NAT gateways" in out

    def test_concluded_prints_success_line(self, renderer_pair):
        renderer, console = renderer_pair
        asyncio.run(
            renderer(
                InvestigationEvent("concluded", {"conclusion": {"root_cause": "x"}})
            )
        )
        assert "concluded" in console.export_text().lower()

    def test_aborted_prints_reason(self, renderer_pair):
        renderer, console = renderer_pair
        asyncio.run(
            renderer(
                InvestigationEvent("aborted", {"reason": "user quit"})
            )
        )
        out = console.export_text()
        assert "Aborted" in out
        assert "user quit" in out


# ---------------------------------------------------------------------------
# Events the renderer intentionally ignores
# ---------------------------------------------------------------------------
class TestSilentEvents:
    """AdvisorProvider + CLI handle these; the renderer must NOT print."""

    @pytest.mark.parametrize(
        "kind,payload",
        [
            ("spike_selected", {"spike": object()}),
            ("command_proposed", {"pending": object()}),
            ("opus_asks", {"question": "which project?"}),
            ("user_note", {"note": "try something different"}),
        ],
    )
    def test_no_output_and_no_spinner(self, renderer_pair, kind, payload):
        renderer, console = renderer_pair
        asyncio.run(renderer(InvestigationEvent(kind, payload)))
        assert console.export_text().strip() == "", (
            f"renderer should be silent on {kind!r}"
        )
        assert renderer._status is None


# ---------------------------------------------------------------------------
# Independence — renderers don't share state across investigations
# ---------------------------------------------------------------------------
class TestRendererIsolation:
    def test_two_renderers_have_independent_spinners(self):
        c1 = Console(record=True, width=80, force_terminal=False)
        c2 = Console(record=True, width=80, force_terminal=False)
        r1 = _InvestigationRenderer(c1)
        r2 = _InvestigationRenderer(c2)

        async def go():
            await r1(InvestigationEvent("step_started", {}))
            # r2 stays idle.
            assert r1._status is not None
            assert r2._status is None

        asyncio.run(go())
        r1._stop_spin()
