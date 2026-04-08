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
from rich.panel import Panel
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
from ghosthunter.providers.advisor import AdvisorProvider
from ghosthunter.providers.billing_file import (
    BillingFileError,
    load_spikes_from_files,
)
from ghosthunter.providers.gcp import GCPProvider
from ghosthunter.security.validator import SecurityValidator

app = typer.Typer(
    name="ghosthunter",
    help="Investigate WHY your cloud costs spiked, not just what changed.",
    invoke_without_command=True,
    no_args_is_help=False,
)
console = Console()


@app.callback()
def _default(ctx: typer.Context) -> None:
    """Run with no subcommand → drop into the chat orchestrator with mode picker."""
    if ctx.invoked_subcommand is not None:
        return
    from ghosthunter.chat import run_chat

    _require_api_key()
    run_chat(initial_files=None, console=console, skip_mode_picker=False)
    raise typer.Exit(0)


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
    files: list[Path] = typer.Argument(
        None,
        metavar="[FILES...]",
        help="One or more billing CSV/JSON files. Shell globs work: "
             "`ghosthunter investigate *.csv`. Equivalent to passing "
             "each file with -f.",
    ),
    billing_file: list[Path] = typer.Option(
        [],
        "--billing-file",
        "-f",
        help="Path to a billing CSV/JSON you exported yourself. "
             "Pass -f multiple times to merge several breakdowns "
             "(by service + by SKU + by project, etc.). "
             "Enables advisor mode (no GCP credentials needed).",
    ),
    active: bool = typer.Option(
        False,
        "--active",
        help="Use active mode: Ghosthunter directly queries GCP and runs "
             "commands. Requires read-only credentials and ~/.ghosthunter/config.toml.",
    ),
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
    """Run an investigation.

    Default is advisor mode: you supply a billing export file with
    `--billing-file`, Ghosthunter never touches your cloud, and you run
    each proposed command yourself in your own terminal. Use `--active`
    if you have read-only credentials configured and want Ghosthunter to
    run commands directly.
    """
    # API key is only required if we're actually going to run an investigation.
    # `--list` just shows spikes from the file and needs no API access.
    if not list_only:
        _require_api_key()

    # Combine positional args and -f flags so both styles work
    all_files: list[Path] = list(files or []) + list(billing_file or [])

    if active and all_files:
        console.print(
            "[red]Use either --active or billing files, not both.[/red]"
        )
        raise typer.Exit(1)

    if active:
        _run_active_mode(spike_index=spike_index, list_only=list_only)
        return

    if not all_files:
        console.print(
            "[red]Advisor mode requires at least one billing file.[/red]\n"
            "Examples:\n"
            "  [bold]ghosthunter investigate report.csv[/bold]\n"
            "  [bold]ghosthunter investigate by-service.csv by-sku.csv[/bold]\n"
            "  [bold]ghosthunter investigate -f file1.csv -f file2.csv[/bold]\n\n"
            "Run [bold]ghosthunter billing-template[/bold] for export instructions."
        )
        raise typer.Exit(1)

    _run_advisor_mode(
        billing_files=all_files,
        spike_index=spike_index,
        list_only=list_only,
    )


@app.command()
def chat(
    files: list[Path] = typer.Argument(
        None,
        metavar="[FILES...]",
        help="Optional billing files to /load on startup. "
             "Shell globs work: `ghosthunter chat *.csv`. "
             "When files are provided, the mode picker is skipped and you "
             "go straight into paranoid (advisor) mode.",
    ),
) -> None:
    """Open the interactive chat orchestrator (recommended).

    With no arguments, shows a mode picker so you can choose paranoid
    (advisor), active, demo, or audit. With files, jumps straight into
    paranoid mode.
    """
    from ghosthunter.chat import run_chat

    _require_api_key()
    has_files = bool(files)
    run_chat(
        initial_files=list(files or []),
        console=console,
        skip_mode_picker=has_files,
    )


@app.command()
def billing_template() -> None:
    """Show how to export the billing data Ghosthunter advisor mode needs."""
    console.print(
        Panel(
            (
                "[bold cyan]Option A — One rich BigQuery export (RECOMMENDED)[/bold cyan]\n"
                "If you have BigQuery billing export enabled, run this once in your\n"
                "own terminal. One file, every dimension, full hypothesis power.\n\n"
                "[dim]bq query --nouse_legacy_sql --format=csv \\\n"
                "  'SELECT\n"
                "     service.description           AS service,\n"
                "     sku.description               AS sku,\n"
                "     project.id                    AS project,\n"
                "     IFNULL(location.region, location.location) AS location,\n"
                "     DATE(usage_start_time)        AS usage_start_date,\n"
                "     SUM(cost)                     AS cost\n"
                "   FROM `YOUR_PROJECT.billing_export.gcp_billing_export_v1_*`\n"
                "   WHERE DATE(usage_start_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 60 DAY)\n"
                "   GROUP BY service, sku, project, location, usage_start_date\n"
                "   ORDER BY usage_start_date' > billing.csv[/dim]\n\n"
                "Then: [bold]ghosthunter investigate -f billing.csv[/bold]\n\n"

                "[bold cyan]Option B — Console downloads (no BQ needed)[/bold cyan]\n"
                "Console only lets you download one grouping at a time, so do\n"
                "two or three downloads and merge them with multiple -f flags.\n\n"
                "  1. Go to [cyan]https://console.cloud.google.com/billing[/cyan]\n"
                "  2. Pick your billing account → [bold]Reports[/bold]\n"
                "  3. Pick the date range covering the spike\n"
                "  4. Group by [bold]Service[/bold] → Download CSV → save as by-service.csv\n"
                "  5. Change Group by → [bold]SKU[/bold] → Download CSV → save as by-sku.csv\n"
                "  6. (optional) Group by [bold]Project[/bold] → Download CSV → save as by-project.csv\n\n"
                "Then: [bold]ghosthunter investigate \\\n"
                "        -f by-service.csv -f by-sku.csv -f by-project.csv[/bold]\n\n"

                "[dim]Recognized columns (case-insensitive, multiple aliases per field):[/dim]\n"
                "  • [bold]service[/bold]  (required) — service / Service description / service.description\n"
                "  • [bold]cost[/bold]     (required) — cost / Cost ($) / Subtotal ($) / amount\n"
                "  • [bold]date[/bold]     (optional) — usage_start_time / Usage start date / date\n"
                "  • [bold]sku[/bold]      (optional) — sku / SKU description / sku.description\n"
                "  • [bold]project[/bold]  (optional) — project / Project ID / project.id\n"
                "  • [bold]location[/bold] (optional) — location / region / location.region\n\n"
                "Extra dimensions don't change spike detection — they sharpen the\n"
                "hypotheses Opus forms by showing WHERE inside the service the cost moved."
            ),
            title="Export your billing data",
            border_style="cyan",
        )
    )


def _run_active_mode_interactive(console_arg: Console) -> None:
    """Active-mode entry from the chat mode picker.

    Loads config (or prints an error if missing), fetches spikes, and
    runs an interactive investigation against the largest one.
    """
    try:
        cfg = Config.load()
    except FileNotFoundError:
        console_arg.print(
            Panel(
                (
                    "[bold]Active mode requires a config file.[/bold]\n\n"
                    f"Run [bold]ghosthunter init[/bold] to create one at\n"
                    f"  {CONFIG_PATH}\n\n"
                    "You'll need a GCP project ID and the BigQuery billing\n"
                    "export dataset (e.g. `my-proj.billing_export`)."
                ),
                title="config missing",
                border_style="red",
            )
        )
        return
    _run_active_mode(spike_index=0, list_only=False)


def _render_audit_table(console_arg: Console) -> None:
    """Audit log entry from the chat mode picker."""
    if not AUDIT_LOG_PATH.exists():
        console_arg.print("[yellow]No audit log yet.[/yellow]")
        return

    with AUDIT_LOG_PATH.open() as f:
        lines = f.readlines()

    table = Table(title=f"Audit log ({AUDIT_LOG_PATH})")
    table.add_column("Time", style="cyan")
    table.add_column("Service")
    table.add_column("Result")
    table.add_column("Commands", justify="right")
    table.add_column("Root cause / reason")

    for line in lines[-50:]:
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
    console_arg.print(table)


def _run_active_mode(spike_index: int, list_only: bool) -> None:
    """Old behavior: Ghosthunter directly queries GCP and runs commands."""
    cfg = _require_config()
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

    spike = _pick_spike(spikes, spike_index)
    console.print(
        f"\n[bold]Investigating[/bold] {spike.service} "
        f"({spike.change_percent:+.1f}%, ${spike.absolute_change:+,.0f})\n"
    )

    investigator = _build_active_investigator(provider, cfg)
    result = asyncio.run(investigator.investigate(spike))
    _render_result(result)
    _append_audit_log(result)


def _run_advisor_mode(
    billing_files: list[Path],
    spike_index: int,
    list_only: bool,
) -> None:
    """Default behavior: parse billing files, advise commands, never touch GCP."""
    console.print(
        Panel(
            "[bold]Advisor mode[/bold] — Ghosthunter will [bold]not[/bold] touch "
            "your cloud.\nIt reads your billing export, proposes read-only "
            "commands,\nand asks you to run them yourself and paste back the output.",
            border_style="bright_yellow",
        )
    )
    if len(billing_files) > 1:
        console.print(
            f"[dim]Merging {len(billing_files)} billing files…[/dim]"
        )

    try:
        spikes = load_spikes_from_files(billing_files)
    except BillingFileError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    if not spikes:
        console.print("[yellow]No services found in those billing files.[/yellow]")
        raise typer.Exit(0)

    _render_spike_table(spikes)
    if list_only:
        raise typer.Exit(0)

    spike = _pick_spike(spikes, spike_index)
    console.print(
        f"\n[bold]Investigating[/bold] {spike.service} "
        f"(${spike.current_cost:,.0f})"
    )
    _render_top_contributors(spike)
    console.print()

    investigator = _build_advisor_investigator()
    result = asyncio.run(investigator.investigate(spike))
    _render_result(result)
    _append_audit_log(result)


def _render_top_contributors(spike) -> None:
    """Show the top SKUs/projects/locations driving a spike, if known."""
    if not spike.top_contributors:
        return
    for dim, items in spike.top_contributors.items():
        if not items:
            continue
        console.print(f"[dim]Top {dim}s:[/dim]")
        for name, cost in items[:5]:
            console.print(f"  • {name:<60} ${cost:>12,.2f}")


def _pick_spike(spikes, spike_index: int):
    if spike_index >= len(spikes):
        console.print(
            f"[red]Spike index {spike_index} out of range (have {len(spikes)}).[/red]"
        )
        raise typer.Exit(1)
    return spikes[spike_index]


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
def palace(
    action: str = typer.Argument(
        "status",
        metavar="[status|tools|install-check]",
        help="status = show availability; "
             "tools = list MCP tools from the server; "
             "install-check = diagnose why palace isn't available.",
    ),
) -> None:
    """Inspect the MemPalace memory integration.

    The palace is OPTIONAL. If `mempalace` and `mcp` aren't installed,
    all memory features silently no-op. This command is for checking
    whether it's wired up correctly and debugging MCP connection issues.
    """
    from ghosthunter.memory import get_palace, is_available

    client = get_palace()

    if action == "install-check":
        console.print(
            Panel(
                (
                    f"[bold]mcp installed:[/bold]        "
                    f"{'yes' if _module_exists('mcp') else 'no'}\n"
                    f"[bold]mempalace installed:[/bold]  "
                    f"{'yes' if _module_exists('mempalace') else 'no'}\n"
                    f"[bold]Storage path:[/bold]         {client.storage_path}\n"
                    f"\n"
                    f"[dim]To install both into the current venv:[/dim]\n"
                    f"  [bold].venv/bin/pip install mempalace mcp[/bold]\n"
                ),
                title="palace install check",
                border_style="cyan" if is_available() else "yellow",
            )
        )
        return

    if action == "tools":
        if not is_available():
            console.print(
                "[yellow]Palace not available. Run "
                "[bold]ghosthunter palace install-check[/bold] for details.[/yellow]"
            )
            raise typer.Exit(1)
        tools = client.list_tools()
        if not tools:
            console.print("[red]No tools returned. Server may have failed to start.[/red]")
            raise typer.Exit(1)
        table = Table(title=f"MemPalace MCP tools ({len(tools)})")
        table.add_column("#", justify="right")
        table.add_column("Tool name", style="cyan")
        table.add_column("Description")
        for i, t in enumerate(tools):
            desc = (t.get("description") or "").split("\n", 1)[0][:80]
            table.add_row(str(i + 1), t.get("name") or "?", desc)
        console.print(table)
        return

    # default: status
    status = client.status()
    lines = [
        f"[bold]Available:[/bold]     {'yes' if status.available else 'no'}",
        f"[bold]Storage:[/bold]       {status.storage_path}",
    ]
    if status.tool_count:
        lines.append(f"[bold]Tools:[/bold]         {status.tool_count}")
    if status.reason:
        lines.append(f"[yellow]Note:[/yellow]          {status.reason}")
    console.print(
        Panel(
            "\n".join(lines),
            title="memory palace status",
            border_style="cyan" if status.available else "yellow",
        )
    )


def _module_exists(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


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


def _build_active_investigator(provider: GCPProvider, cfg: Config) -> Investigator:
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


async def _auto_approve(_: PendingCommand) -> str:
    """Advisor-mode approval is collapsed into the print-and-wait flow,
    so the investigator-level approval prompt is a no-op."""
    return "approve"


def _build_advisor_investigator() -> Investigator:
    """Investigator with AdvisorProvider — never touches GCP."""
    validator = SecurityValidator()
    provider = AdvisorProvider(validator=validator, console=console)
    return Investigator(
        provider=provider,  # type: ignore[arg-type]
        reasoner=Reasoner(),
        executor=Executor(),
        validator=validator,
        approval_hook=_auto_approve,  # AdvisorProvider handles user interaction
        event_hook=_print_event,
        budget=Budget(),
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
    table.add_column("Kind")
    table.add_column("Name")
    table.add_column("Previous", justify="right")
    table.add_column("Current", justify="right")
    table.add_column("Change %", justify="right")
    table.add_column("Δ $", justify="right")
    for i, s in enumerate(spikes):
        kind = getattr(s, "grouping", "service")
        if s.previous_cost > 0:
            pct_str = f"{s.change_percent:+.1f}%"
        elif s.change_percent == float("inf"):
            pct_str = "[red]new[/red]"
        else:
            pct_str = "[dim]n/a[/dim]"
        table.add_row(
            str(i),
            f"[dim]{kind}[/dim]",
            s.service,
            f"${s.previous_cost:,.0f}" if s.previous_cost > 0 else "[dim]—[/dim]",
            f"${s.current_cost:,.0f}",
            pct_str,
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
