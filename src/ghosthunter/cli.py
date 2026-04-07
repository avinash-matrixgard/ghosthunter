"""Ghosthunter CLI.

Commands:
    init        — interactively create ~/.ghosthunter/config.toml
    investigate — run a real investigation against your GCP project
    demo        — replay a bundled investigation (no API calls, no GCP)
    audit       — show the audit log of past investigations
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from ghosthunter.config import AUDIT_LOG_PATH, CONFIG_PATH, BudgetConfig, Config
from ghosthunter.investigator import (
    Budget,
    InvestigationEvent,
    Investigator,
    PendingCommand,
)
from ghosthunter.models.executor import Executor
from ghosthunter.models.reasoner import Reasoner
from ghosthunter.providers.gcp import GCPProvider
from ghosthunter.security.validator import SecurityValidator

app = typer.Typer(
    name="ghosthunter",
    help="Investigate WHY your cloud costs spiked, not just what changed.",
    no_args_is_help=True,
)
console = Console()


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------
@app.command()
def init() -> None:
    """Interactively create ~/.ghosthunter/config.toml."""
    console.print("[bold cyan]Ghosthunter setup[/bold cyan]\n")

    if CONFIG_PATH.exists():
        if not Confirm.ask(
            f"Config already exists at {CONFIG_PATH}. Overwrite?", default=False
        ):
            console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)

    project_id = Prompt.ask("GCP project ID")
    billing_dataset = Prompt.ask(
        "Billing export dataset (e.g. my-proj.billing_export)"
    )
    lookback_days = int(Prompt.ask("Lookback days", default="30"))

    cfg = Config(
        project_id=project_id,
        billing_dataset=billing_dataset,
        lookback_days=lookback_days,
        budget=BudgetConfig(),
    )
    cfg.save()

    console.print(f"\n[green]✓[/green] Saved config to {CONFIG_PATH}")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            "[yellow]![/yellow] Set ANTHROPIC_API_KEY in your shell to run "
            "investigations."
        )


# ---------------------------------------------------------------------------
# investigate
# ---------------------------------------------------------------------------
@app.command()
def investigate(
    spike_index: int = typer.Option(
        0,
        "--spike",
        help="Which detected spike to investigate (0 = largest).",
    ),
    list_only: bool = typer.Option(
        False,
        "--list",
        help="Only list detected spikes; do not investigate.",
    ),
) -> None:
    """Run a real investigation against your GCP project."""
    cfg = _require_config()
    _require_api_key()

    provider = GCPProvider(
        project_id=cfg.project_id,
        billing_dataset=cfg.billing_dataset,
    )

    console.print(
        f"[cyan]Fetching billing data for {cfg.project_id} "
        f"(last {cfg.lookback_days} days)...[/cyan]"
    )
    spikes = provider.fetch_billing_spikes(lookback_days=cfg.lookback_days)
    if not spikes:
        console.print("[green]No material cost spikes detected.[/green]")
        raise typer.Exit(0)

    _render_spike_table(spikes)
    if list_only:
        raise typer.Exit(0)

    if spike_index >= len(spikes):
        console.print(
            f"[red]Spike index {spike_index} out of range (have {len(spikes)}).[/red]"
        )
        raise typer.Exit(1)

    spike = spikes[spike_index]
    console.print(
        f"\n[bold]Investigating[/bold] {spike.service} "
        f"({spike.change_percent:+.1f}%, ${spike.absolute_change:+,.0f})\n"
    )

    investigator = _build_investigator(provider, cfg)
    result = asyncio.run(investigator.investigate(spike))

    _render_result(result)
    _append_audit_log(result)


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------
@app.command()
def demo(
    scenario: Optional[str] = typer.Option(
        None,
        "--scenario",
        help="Scenario id to replay (random if omitted). "
             "e.g. dns_cache_bypass, nat_egress_runaway, bigquery_full_scan, "
             "orphaned_disks, gke_autoscaler_loop",
    ),
) -> None:
    """Replay a bundled investigation. No API calls, no setup."""
    from ghosthunter.demo import run_demo

    asyncio.run(run_demo(console, scenario_id=scenario))


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------
@app.command()
def audit(
    limit: int = typer.Option(20, "--limit", help="Max entries to show."),
) -> None:
    """Show the audit log of past investigations."""
    if not AUDIT_LOG_PATH.exists():
        console.print("[yellow]No audit log yet.[/yellow]")
        raise typer.Exit(0)

    with AUDIT_LOG_PATH.open() as f:
        lines = f.readlines()

    table = Table(title=f"Audit log ({AUDIT_LOG_PATH})")
    table.add_column("Time", style="cyan")
    table.add_column("Service")
    table.add_column("Result")
    table.add_column("Commands", justify="right")
    table.add_column("Root cause / reason")

    for line in lines[-limit:]:
        entry = json.loads(line)
        result_label = (
            "[green]concluded[/green]"
            if entry["succeeded"]
            else "[red]aborted[/red]"
        )
        summary = (
            entry.get("conclusion", {}).get("root_cause")
            or entry.get("aborted_reason")
            or "—"
        )
        table.add_row(
            entry["timestamp"],
            entry["service"],
            result_label,
            str(entry["commands_used"]),
            summary,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _require_config() -> Config:
    try:
        return Config.load()
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _require_api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            "[red]ANTHROPIC_API_KEY is not set. Export it and try again.[/red]"
        )
        raise typer.Exit(1)


def _build_investigator(provider: GCPProvider, cfg: Config) -> Investigator:
    budget = Budget(
        max_commands=cfg.budget.max_commands,
        max_cost_usd=cfg.budget.max_cost_usd,
        max_seconds=cfg.budget.max_seconds,
    )
    return Investigator(
        provider=provider,
        reasoner=Reasoner(),
        executor=Executor(),
        validator=SecurityValidator(),
        approval_hook=_interactive_approval,
        event_hook=_print_event,
        budget=budget,
    )


async def _interactive_approval(pending: PendingCommand) -> str:
    """Show the proposed command and ask the user."""
    console.print("\n[bold yellow]Proposed command:[/bold yellow]")
    console.print(f"  [white]{pending.command}[/white]")
    if pending.tests_hypothesis:
        console.print(f"  Tests: {pending.tests_hypothesis}")
    if pending.rationale:
        console.print(f"  Why:   {pending.rationale}")

    answer = Prompt.ask(
        "[bold]Run this?[/bold] [y]es / [n]o / [a]bort",
        choices=["y", "n", "a"],
        default="y",
    )
    return {"y": "approve", "n": "reject", "a": "abort"}[answer]


async def _print_event(event: InvestigationEvent) -> None:
    """Minimal event printer. Rich UI gets a richer renderer later."""
    kind = event.kind
    if kind == "hypotheses_updated":
        console.print("\n[bold]Hypotheses:[/bold]")
        for h in event.payload["hypotheses"]:
            bar = "█" * (h["confidence"] // 5)
            console.print(
                f"  {h['id']} [{h['status']:>10}] {h['confidence']:3}% "
                f"{bar:20} {h['description']}"
            )
    elif kind == "command_blocked":
        console.print(
            f"[red]✗ blocked ({event.payload['layer']}):[/red] "
            f"{event.payload['reason']}"
        )
    elif kind == "command_executed":
        result = event.payload["result"]
        console.print(
            f"[dim]ran in {result.duration_seconds:.1f}s, "
            f"exit={result.exit_code}[/dim]"
        )
    elif kind == "evidence_added":
        e = event.payload["evidence"]
        console.print(f"[green]+ {e.id}[/green] {e.summary[:120]}...")
    elif kind == "concluded":
        console.print("\n[bold green]✓ Investigation concluded[/bold green]")
    elif kind == "aborted":
        console.print(
            f"\n[bold red]✗ Aborted:[/bold red] {event.payload['reason']}"
        )


def _render_spike_table(spikes) -> None:
    table = Table(title="Detected cost spikes")
    table.add_column("#", justify="right")
    table.add_column("Service")
    table.add_column("Previous", justify="right")
    table.add_column("Current", justify="right")
    table.add_column("Change %", justify="right")
    table.add_column("Δ $", justify="right")
    for i, s in enumerate(spikes):
        table.add_row(
            str(i),
            s.service,
            f"${s.previous_cost:,.0f}",
            f"${s.current_cost:,.0f}",
            f"{s.change_percent:+.1f}%",
            f"${s.absolute_change:+,.0f}",
        )
    console.print(table)


def _render_result(result) -> None:
    if result.succeeded and result.conclusion:
        c = result.conclusion
        console.print("\n[bold]Root cause:[/bold]")
        console.print(f"  {c.get('root_cause', '?')} "
                      f"({c.get('confidence', '?')}%)")
        if c.get("evidence_summary"):
            console.print("\n[bold]Evidence:[/bold]")
            for e in c["evidence_summary"]:
                console.print(f"  • {e}")
        if c.get("recommendations"):
            console.print("\n[bold]Recommendations:[/bold]")
            for r in c["recommendations"]:
                console.print(f"  → {r}")
        if c.get("not_verified"):
            console.print("\n[bold]Not verified:[/bold]")
            for n in c["not_verified"]:
                console.print(f"  ? {n}")

    console.print(
        f"\n[dim]Commands used: {result.budget.commands_used}/"
        f"{result.budget.max_commands}  •  "
        f"Time: {result.budget.seconds_used:.0f}s/"
        f"{result.budget.max_seconds:.0f}s[/dim]"
    )


def _append_audit_log(result) -> None:
    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "service": result.spike.service,
        "succeeded": result.succeeded,
        "commands_used": result.budget.commands_used,
        "conclusion": result.conclusion,
        "aborted_reason": result.aborted_reason,
    }
    with AUDIT_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


if __name__ == "__main__":
    app()
