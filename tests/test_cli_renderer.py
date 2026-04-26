"""CLI event renderer — animated spinner phases + static panels.

Locks down the Claude-Code-style UX added in response to "this looks
frozen while Opus is thinking". The renderer fires a `rich.status.Status`
spinner whenever the backend is doing opaque work and stops it around
anything the user needs to read or respond to.

Spinner phases, with context-rich labels so the user sees WHAT is being
worked on — not just that something is running:
  step_started       → "Opus is reasoning · turn N · K active hypotheses"
  command_proposed   → "Sonnet is validating · <first 60 chars of command>"
  compressing        → "Sonnet is compressing · <bytes> from '<command>'"

Note: the validation spinner fires at ``command_proposed``, not at
``command_approved``. In the investigator loop Sonnet's Layer-6 check
happens BETWEEN those two events, so ``command_approved`` marks
validation *done*, not *starting*. This was a bug in the first cut of
the spinner.

Every other event stops the spinner before rendering its own output.
"""

from __future__ import annotations

import asyncio

import pytest
from rich.console import Console

from ghosthunter.cli import (
    _fmt_bytes,
    _InvestigationRenderer,
    _preview_command,
)
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
        assert renderer._status is not None, "step_started should start the spinner"
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

    def test_command_proposed_starts_validation_spinner(self, renderer_pair):
        """Validation happens BEFORE command_approved, not after."""
        renderer, _ = renderer_pair

        class _P:
            command = "aws ec2 describe-instances --region us-east-1"

        asyncio.run(renderer(InvestigationEvent("command_proposed", {"pending": _P()})))
        assert renderer._status is not None
        renderer._stop_spin()

    def test_compressing_starts_compression_spinner(self, renderer_pair):
        renderer, _ = renderer_pair
        asyncio.run(
            renderer(
                InvestigationEvent(
                    "compressing",
                    {"command": "aws ec2 describe-instances", "bytes": 18400},
                )
            )
        )
        assert renderer._status is not None
        renderer._stop_spin()

    def test_phase_transition_replaces_spinner(self, renderer_pair):
        """step_started → command_proposed should swap the spinner text."""
        renderer, _ = renderer_pair

        class _P:
            command = "aws s3 ls"

        async def go():
            await renderer(InvestigationEvent("step_started", {}))
            assert renderer._status is not None
            first_status = renderer._status
            await renderer(InvestigationEvent("command_proposed", {"pending": _P()}))
            # A new Status object replaces the old one.
            assert renderer._status is not None
            assert renderer._status is not first_status

        asyncio.run(go())
        renderer._stop_spin()

    def test_concluded_stops_spinner(self, renderer_pair):
        renderer, _ = renderer_pair

        async def go():
            await renderer(InvestigationEvent("step_started", {}))
            await renderer(InvestigationEvent("concluded", {"conclusion": {"root_cause": "x"}}))

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
            renderer(InvestigationEvent("reasoning", {"text": "Evidence favors H1 strongly."}))
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

        asyncio.run(renderer(InvestigationEvent("evidence_added", {"evidence": _Evidence()})))
        out = console.export_text()
        assert "E1" in out
        assert "NAT gateways" in out

    def test_concluded_prints_success_line(self, renderer_pair):
        renderer, console = renderer_pair
        asyncio.run(renderer(InvestigationEvent("concluded", {"conclusion": {"root_cause": "x"}})))
        assert "concluded" in console.export_text().lower()

    def test_aborted_prints_reason(self, renderer_pair):
        renderer, console = renderer_pair
        asyncio.run(renderer(InvestigationEvent("aborted", {"reason": "user quit"})))
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
            ("opus_asks", {"question": "which project?"}),
        ],
    )
    def test_no_output_and_no_spinner(self, renderer_pair, kind, payload):
        renderer, console = renderer_pair
        asyncio.run(renderer(InvestigationEvent(kind, payload)))
        assert console.export_text().strip() == "", f"renderer should be silent on {kind!r}"
        assert renderer._status is None


class TestDetailLineHelpers:
    """Spinner labels are only useful if the detail text is readable."""

    def test_short_command_unchanged(self):
        assert _preview_command("aws s3 ls") == "aws s3 ls"

    def test_multiline_command_collapses_whitespace(self):
        cmd = "aws ec2 \\\n    describe-instances \\\n    --region us-east-1"
        assert "\n" not in _preview_command(cmd)

    def test_long_command_truncated_with_ellipsis(self):
        cmd = "aws ce get-cost-and-usage --time-period Start=2026-01-01,End=2026-04-01 --granularity DAILY"
        out = _preview_command(cmd)
        assert out.endswith("…")
        assert len(out) == 60

    @pytest.mark.parametrize(
        "n,expected_suffix",
        [
            (0, "B"),
            (512, "B"),
            (1023, "B"),
            (1024, "KB"),
            (18400, "KB"),
            (2 * 1024 * 1024, "MB"),
        ],
    )
    def test_fmt_bytes(self, n, expected_suffix):
        assert expected_suffix in _fmt_bytes(n)


def _spinner_text(renderer) -> str:
    """Extract the current spinner's display text.

    Rich's Status wraps a Spinner; the display string lives at
    ``status.renderable.text``. We stringify it to collapse any Rich
    Text objects so substring assertions work.
    """
    status = renderer._status
    assert status is not None, "spinner not running"
    return str(status.renderable.text)


class TestSpinnerContextEnrichment:
    """The detail-line text on each spinner should actually be populated."""

    def test_validation_spinner_shows_command_preview(self, renderer_pair):
        renderer, _ = renderer_pair

        class _P:
            command = "aws ec2 describe-instances --region us-east-1"

        asyncio.run(renderer(InvestigationEvent("command_proposed", {"pending": _P()})))
        text = _spinner_text(renderer)
        assert "aws ec2 describe-instances" in text
        assert "validating" in text.lower()
        renderer._stop_spin()

    def test_compression_spinner_shows_byte_count(self, renderer_pair):
        renderer, _ = renderer_pair
        asyncio.run(
            renderer(
                InvestigationEvent(
                    "compressing",
                    {"command": "aws ec2 describe-instances", "bytes": 18432},
                )
            )
        )
        text = _spinner_text(renderer)
        assert "18.0 KB" in text
        assert "compressing" in text.lower()
        renderer._stop_spin()

    def test_reasoning_spinner_tracks_turn_count(self, renderer_pair):
        renderer, _ = renderer_pair

        async def go():
            await renderer(InvestigationEvent("step_started", {}))
            text1 = _spinner_text(renderer)
            renderer._stop_spin()
            await renderer(InvestigationEvent("step_started", {}))
            text2 = _spinner_text(renderer)
            renderer._stop_spin()
            return text1, text2

        text1, text2 = asyncio.run(go())
        assert "turn 1" in text1
        assert "turn 2" in text2

    def test_reasoning_spinner_shows_active_hypothesis_count(self, renderer_pair):
        renderer, _ = renderer_pair

        async def go():
            await renderer(
                InvestigationEvent(
                    "hypotheses_updated",
                    {
                        "hypotheses": [
                            {"id": "H1", "status": "active", "confidence": 60, "description": "A"},
                            {"id": "H2", "status": "active", "confidence": 30, "description": "B"},
                            {
                                "id": "H3",
                                "status": "eliminated",
                                "confidence": 4,
                                "description": "C",
                            },
                        ]
                    },
                )
            )
            await renderer(InvestigationEvent("step_started", {}))
            return _spinner_text(renderer)

        text = asyncio.run(go())
        # 2 active, 1 eliminated → we say "2 active hypotheses"
        assert "2" in text and "active" in text
        renderer._stop_spin()


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
