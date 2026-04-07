"""Rich-based renderers for the investigation flow.

Pure presentation. The investigator emits structured events; this module
turns them into terminal output. Keep this module free of business logic
so it can be swapped out (e.g. for a JSON streamer) without touching
the investigator.
"""
from __future__ import annotations

from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from ghosthunter.evidence import Evidence
from ghosthunter.hypothesis import Hypothesis
from ghosthunter.investigator import InvestigationEvent, PendingCommand
from ghosthunter.providers.gcp import CommandResult, CostSpike

# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------
def _confidence_color(confidence: int) -> str:
    if confidence >= 85:
        return "bright_green"
    if confidence >= 60:
        return "green"
    if confidence >= 30:
        return "yellow"
    return "red"


def _status_badge(status: str) -> Text:
    colors = {
        "active": "yellow",
        "confirmed": "bright_green",
        "eliminated": "dim red",
    }
    return Text(f" {status} ", style=f"reverse {colors.get(status, 'white')}")


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------
def render_spike_panel(spike: CostSpike) -> Panel:
    body = Text.assemble(
        ("Service:  ", "bold"),
        f"{spike.service}\n",
        ("Previous: ", "bold"),
        f"${spike.previous_cost:>14,.2f}\n",
        ("Current:  ", "bold"),
        f"${spike.current_cost:>14,.2f}\n",
        ("Change:   ", "bold"),
        Text(
            f"{spike.change_percent:+.1f}% (${spike.absolute_change:+,.0f})",
            style="bold red" if spike.absolute_change > 0 else "bold green",
        ),
    )
    return Panel(body, title="Cost Spike", border_style="red", expand=False)


def render_hypotheses(hypotheses: list[Hypothesis]) -> Panel:
    if not hypotheses:
        return Panel("(no hypotheses yet)", title="Hypotheses", border_style="cyan")

    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(width=4)              # ID
    table.add_column(width=12)             # status
    table.add_column(width=20)             # bar
    table.add_column(width=5, justify="right")  # %
    table.add_column(ratio=1)              # description

    for h in hypotheses:
        bar = ProgressBar(
            total=100,
            completed=h.confidence,
            width=20,
            complete_style=_confidence_color(h.confidence),
            finished_style=_confidence_color(h.confidence),
        )
        table.add_row(
            Text(h.id, style="bold cyan"),
            _status_badge(h.status),
            bar,
            Text(f"{h.confidence}%", style=_confidence_color(h.confidence)),
            Text(h.description, style="white"),
        )

    return Panel(table, title="Hypotheses", border_style="cyan")


def render_pending_command(pending: PendingCommand) -> Panel:
    parts: list[Any] = [
        Text(pending.command, style="bold white"),
    ]
    if pending.tests_hypothesis:
        parts.append(Text(f"\nTests: {pending.tests_hypothesis}", style="dim"))
    if pending.rationale:
        parts.append(Text(f"\nWhy:   {pending.rationale}", style="dim"))
    return Panel(
        Group(*parts),
        title="Proposed command",
        border_style="yellow",
        expand=False,
    )


def render_command_result(result: CommandResult) -> Panel:
    style = "green" if result.succeeded else "red"
    header = Text.assemble(
        ("exit ", "dim"),
        Text(str(result.exit_code), style=style),
        ("  •  ", "dim"),
        f"{result.duration_seconds:.1f}s",
    )
    if result.truncated:
        header.append("  • truncated", style="yellow")
    return Panel(header, title="Result", border_style=style, expand=False)


def render_evidence(evidence: Evidence) -> Panel:
    return Panel(
        Text(evidence.summary, style="white"),
        title=f"Evidence {evidence.id}",
        border_style="green",
        subtitle=f"[dim]{evidence.command}[/dim]",
        expand=False,
    )


def render_conclusion(conclusion: dict[str, Any]) -> Panel:
    body = Text.assemble(
        ("Root cause: ", "bold"),
        Text(
            f"{conclusion.get('root_cause', '?')}\n",
            style="bold bright_green",
        ),
        ("Confidence: ", "bold"),
        f"{conclusion.get('confidence', '?')}%\n",
    )
    if conclusion.get("evidence_summary"):
        body.append("\nEvidence:\n", style="bold")
        for e in conclusion["evidence_summary"]:
            body.append(f"  • {e}\n")
    if conclusion.get("recommendations"):
        body.append("\nRecommendations:\n", style="bold")
        for r in conclusion["recommendations"]:
            body.append(f"  → {r}\n", style="cyan")
    if conclusion.get("not_verified"):
        body.append("\nNot verified:\n", style="bold yellow")
        for n in conclusion["not_verified"]:
            body.append(f"  ? {n}\n", style="dim")
    return Panel(body, title="Conclusion", border_style="bright_green")


# ---------------------------------------------------------------------------
# Stream renderer
# ---------------------------------------------------------------------------
class RichStreamRenderer:
    """Event hook implementation that prints panels as events arrive."""

    def __init__(self, console: Console | None = None, demo: bool = False) -> None:
        self.console = console or Console()
        self.demo = demo

    async def __call__(self, event: InvestigationEvent) -> None:
        kind = event.kind
        payload = event.payload

        if kind == "spike_selected":
            self.console.print(render_spike_panel(payload["spike"]))

        elif kind == "step_started":
            prefix = "[DEMO] " if self.demo else ""
            self.console.print(f"\n[dim]{prefix}thinking…[/dim]")

        elif kind == "hypotheses_updated":
            hyps = [_dict_to_hypothesis(h) for h in payload["hypotheses"]]
            self.console.print(render_hypotheses(hyps))

        elif kind == "command_proposed":
            self.console.print(render_pending_command(payload["pending"]))

        elif kind == "command_blocked":
            self.console.print(
                f"[red]✗ blocked at {payload['layer']}: {payload['reason']}[/red]"
            )

        elif kind == "command_rejected_by_user":
            self.console.print(
                f"[yellow]→ rejected by user: {payload['command']}[/yellow]"
            )

        elif kind == "command_executed":
            self.console.print(render_command_result(payload["result"]))

        elif kind == "evidence_added":
            self.console.print(render_evidence(payload["evidence"]))

        elif kind == "concluded":
            self.console.print(render_conclusion(payload["conclusion"]))

        elif kind == "aborted":
            self.console.print(
                Panel(
                    Text(payload["reason"], style="bold red"),
                    title="Aborted",
                    border_style="red",
                )
            )


def _dict_to_hypothesis(data: dict[str, Any]) -> Hypothesis:
    return Hypothesis(
        id=data["id"],
        description=data["description"],
        confidence=data["confidence"],
        evidence_for=list(data.get("evidence_for", [])),
        evidence_against=list(data.get("evidence_against", [])),
    )
