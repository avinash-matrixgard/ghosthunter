"""Demo mode: replay a bundled investigation with no API calls and no GCP.

The demo loads `sample_data/demo_script.json` and walks through it as if
the real investigator were running. Pre-recorded responses mean the demo
is reproducible, free, and works offline.

The replay is intentionally NOT a fake `Investigator` — that would force
us to mock Anthropic clients. Instead it directly emits the same
`InvestigationEvent` objects the real investigator would emit, so the UI
renders identically.
"""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.prompt import Prompt

from ghosthunter.evidence import Evidence
from ghosthunter.hypothesis import Hypothesis
from ghosthunter.investigator import (
    Budget,
    InvestigationEvent,
    PendingCommand,
)
from ghosthunter.providers.gcp import CommandResult, CostSpike
from ghosthunter.security.validator import SecurityValidator
from ghosthunter.ui import RichStreamRenderer

SAMPLE_DIR = Path(__file__).resolve().parent.parent.parent / "sample_data"
DEMO_SCRIPT_PATH = SAMPLE_DIR / "demo_script.json"


async def run_demo(
    console: Console,
    scenario_id: str | None = None,
    provider_filter: str | None = None,
) -> None:
    """Replay one of the bundled investigations.

    A random scenario is picked unless `scenario_id` is provided. When
    `provider_filter` is set (``"gcp"`` / ``"aws"``), only scenarios
    matching that provider are considered.
    """
    if not DEMO_SCRIPT_PATH.exists():
        console.print(f"[red]Demo script not found at {DEMO_SCRIPT_PATH}[/red]")
        return

    with DEMO_SCRIPT_PATH.open() as f:
        bundle: dict[str, Any] = json.load(f)

    scenarios = bundle.get("scenarios") or [bundle]  # back-compat with old shape
    # Back-compat: old scenarios without a provider field are treated as GCP.
    for s in scenarios:
        s.setdefault("provider", "gcp")

    if provider_filter in ("gcp", "aws"):
        scenarios = [s for s in scenarios if s.get("provider") == provider_filter]
        if not scenarios:
            console.print(f"[red]No scenarios match provider '{provider_filter}'.[/red]")
            return

    script = _select_scenario(scenarios, scenario_id)
    if script is None:
        ids = ", ".join(s.get("id", "?") for s in scenarios)
        console.print(f"[red]Unknown scenario '{scenario_id}'. Available: {ids}[/red]")
        return

    provider = script.get("provider", "gcp")
    label = script.get("metadata", {}).get("scenario", script.get("id", ""))
    console.print(
        f"[bold magenta][DEMO][/bold magenta] Replaying bundled investigation: "
        f"[bold]{label}[/bold]\n"
        f"[dim]Provider: {provider}. No API calls, no cloud access.[/dim]\n"
    )

    renderer = RichStreamRenderer(console=console, demo=True)
    # Scope the validator to the scenario's provider so AWS scenarios
    # render as "allowed (L2)" instead of hitting the GCP allowlist.
    validator = SecurityValidator(provider=provider)
    budget = Budget()

    spike = _spike_from_script(script["spike"])
    await renderer(InvestigationEvent("spike_selected", {"spike": spike}))

    evidence_chain: list[Evidence] = []

    for step in script["steps"]:
        await asyncio.sleep(step.get("delay_seconds", 1.0))
        await renderer(InvestigationEvent("step_started", {}))

        # Hypotheses snapshot
        hypotheses = [_hypothesis_from_dict(h) for h in step["hypotheses"]]
        await renderer(
            InvestigationEvent(
                "hypotheses_updated",
                {"hypotheses": [h.__dict__ for h in hypotheses]},
            )
        )

        # If this step concludes, render and stop.
        if "conclude" in step:
            await renderer(InvestigationEvent("concluded", {"conclusion": step["conclude"]}))
            break

        # Otherwise the step proposes a command.
        command = step["command"]
        static_check = validator.is_allowed(command)
        semantic = _FakeSemanticResult(approved=True, reason="demo replay — pre-validated")
        pending = PendingCommand(
            command=command,
            tests_hypothesis=step.get("tests_hypothesis"),
            rationale=step.get("rationale"),
            static_check=static_check,
            semantic_check=semantic,  # type: ignore[arg-type]
        )
        await renderer(InvestigationEvent("command_proposed", {"pending": pending}))

        decision = _demo_prompt(console)
        if decision == "abort":
            await renderer(InvestigationEvent("aborted", {"reason": "demo aborted by user"}))
            return
        if decision == "reject":
            await renderer(InvestigationEvent("command_rejected_by_user", {"command": command}))
            continue

        # Fake execution
        result = CommandResult(
            command=command,
            stdout="(demo: raw output suppressed)",
            stderr="",
            exit_code=0,
            duration_seconds=2.3,
            truncated=False,
        )
        budget.commands_used += 1
        budget.seconds_used += result.duration_seconds
        await renderer(InvestigationEvent("command_executed", {"result": result}))

        # Pre-recorded compressed evidence
        evidence = Evidence(
            id=f"E{len(evidence_chain) + 1}",
            summary=step["compressed_evidence"],
            command=command,
        )
        evidence_chain.append(evidence)
        await renderer(InvestigationEvent("evidence_added", {"evidence": evidence}))

    console.print(
        f"\n[dim][DEMO] Commands used: {budget.commands_used}  •  "
        f"Time: {budget.seconds_used:.0f}s[/dim]"
    )
    console.print(
        "\n[dim]To run a real investigation against your GCP project, "
        "run [bold]ghosthunter init[/bold] then [bold]ghosthunter investigate[/bold].[/dim]"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _select_scenario(
    scenarios: list[dict[str, Any]], scenario_id: str | None
) -> dict[str, Any] | None:
    if scenario_id is None:
        return random.choice(scenarios)
    for s in scenarios:
        if s.get("id") == scenario_id:
            return s
    return None


def _spike_from_script(data: dict[str, Any]) -> CostSpike:
    return CostSpike(
        service=data["service"],
        current_cost=float(data["current_cost"]),
        previous_cost=float(data["previous_cost"]),
        change_percent=float(data["change_percent"]),
        daily_breakdown=[],
    )


def _hypothesis_from_dict(data: dict[str, Any]) -> Hypothesis:
    h = Hypothesis(
        id=data["id"],
        description=data["description"],
        confidence=data["confidence"],
        evidence_for=list(data.get("evidence_for", [])),
        evidence_against=list(data.get("evidence_against", [])),
    )
    # Force the script's status verbatim — the script is the source of truth
    # in demo mode (the real reasoner would derive it from confidence).
    h.status = data["status"]
    return h


def _demo_prompt(console: Console) -> str:
    answer = Prompt.ask(
        "[bold][DEMO] Run this?[/bold] [y]es / [n]o / [a]bort",
        choices=["y", "n", "a"],
        default="y",
    )
    return {"y": "approve", "n": "reject", "a": "abort"}[answer]


class _FakeSemanticResult:
    """Duck-typed stand-in for SemanticResult so we don't import the
    Sonnet executor module in demo mode."""

    def __init__(self, approved: bool, reason: str) -> None:
        self.approved = approved
        self.reason = reason
