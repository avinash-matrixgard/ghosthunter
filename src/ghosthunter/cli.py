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
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.status import Status
from rich.table import Table

from ghosthunter.config import (
    AUDIT_LOG_PATH,
    CONFIG_PATH,
    AWSConfig,
    BudgetConfig,
    Config,
    migrate_config_in_place,
)
from ghosthunter.investigator import (
    Budget,
    InvestigationEvent,
    Investigator,
    PendingCommand,
)
from ghosthunter.models.executor import Executor
from ghosthunter.models.reasoner import Reasoner
from ghosthunter.providers.advisor import AdvisorProvider
from ghosthunter.providers.aws import AWSProvider, AWS_BILLING_TEMPLATE
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

    provider = Prompt.ask(
        "Cloud provider", choices=["gcp", "aws"], default="gcp"
    )
    lookback_days = int(Prompt.ask("Lookback days", default="30"))

    if provider == "gcp":
        project_id = Prompt.ask("GCP project ID")
        billing_dataset = Prompt.ask(
            "Billing export dataset (e.g. my-proj.billing_export)"
        )
        cfg = Config(
            provider="gcp",
            project_id=project_id,
            billing_dataset=billing_dataset,
            lookback_days=lookback_days,
            budget=BudgetConfig(),
        )
    else:  # aws
        aws_profile = Prompt.ask(
            "AWS named profile (blank = default credential chain)", default=""
        )
        aws_region = Prompt.ask("AWS region", default="us-east-1")
        account_id = Prompt.ask("AWS account ID (12 digits, optional)", default="")
        cfg = Config(
            provider="aws",
            aws=AWSConfig(
                profile=aws_profile,
                region=aws_region,
                account_id=account_id,
            ),
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
             "Enables advisor mode (no cloud credentials needed).",
    ),
    active: bool = typer.Option(
        False,
        "--active",
        help="Use active mode: Ghosthunter directly queries the cloud and "
             "runs commands. Requires read-only credentials and "
             "~/.ghosthunter/config.toml.",
    ),
    provider: str = typer.Option(
        "auto",
        "--provider",
        help="Cloud provider: 'gcp', 'aws', or 'auto' (sniff from billing "
             "files / config). Default: auto.",
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

    resolved_provider = _resolve_provider(provider, all_files, for_active=active)

    if active:
        _run_active_mode(
            spike_index=spike_index,
            list_only=list_only,
            provider=resolved_provider,
        )
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
        provider=resolved_provider,
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
def billing_template(
    provider: str = typer.Option(
        "gcp",
        "--provider",
        help="Which provider's export recipe to show: 'gcp' or 'aws'.",
    ),
) -> None:
    """Show how to export the billing data Ghosthunter advisor mode needs."""
    if provider == "aws":
        console.print(
            Panel(
                AWS_BILLING_TEMPLATE,
                title="Export your AWS billing data",
                border_style="cyan",
            )
        )
        return

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
    _run_active_mode(spike_index=0, list_only=False, provider=cfg.provider or "gcp")


_AUDIT_PROVIDER_STYLES = {
    "gcp": "[blue]gcp[/blue]",
    "aws": "[yellow]aws[/yellow]",
}


def _build_audit_table(lines: list[str]) -> Table:
    """Render the audit log as a Rich table. Shared between `audit` CLI
    command and the chat mode-picker audit view.

    Columns: Time, Provider, Service, Result, Commands, Root cause/reason.
    For AWS rows the Commands column also shows the Cost Explorer API
    call count written by the active-mode runner (e.g. ``7 · ce:3``).
    """
    table = Table(title=f"Audit log ({AUDIT_LOG_PATH})")
    table.add_column("Time", style="cyan")
    table.add_column("Provider")
    table.add_column("Service")
    table.add_column("Result")
    table.add_column("Commands", justify="right")
    table.add_column("Root cause / reason")

    for line in lines:
        entry = json.loads(line)
        provider = entry.get("provider") or "gcp"
        provider_label = _AUDIT_PROVIDER_STYLES.get(
            provider, f"[dim]{provider}[/dim]"
        )
        result_label = (
            "[green]concluded[/green]"
            if entry["succeeded"]
            else "[red]aborted[/red]"
        )
        # `conclusion` can be None for aborted/failed runs, not just absent —
        # `entry.get("conclusion", {})` would return None and break .get().
        summary = (
            (entry.get("conclusion") or {}).get("root_cause")
            or entry.get("aborted_reason")
            or "—"
        )
        cmd_cell = str(entry["commands_used"])
        ce_calls = entry.get("ce_api_calls")
        if isinstance(ce_calls, int) and ce_calls > 0:
            cmd_cell += f" · ce:{ce_calls}"
        table.add_row(
            entry["timestamp"],
            provider_label,
            entry["service"],
            result_label,
            cmd_cell,
            summary,
        )
    return table


def _render_audit_table(console_arg: Console) -> None:
    """Audit log entry from the chat mode picker."""
    if not AUDIT_LOG_PATH.exists():
        console_arg.print("[yellow]No audit log yet.[/yellow]")
        return

    with AUDIT_LOG_PATH.open() as f:
        lines = f.readlines()
    console_arg.print(_build_audit_table(lines[-50:]))


def _run_active_mode(
    spike_index: int, list_only: bool, provider: str = "gcp"
) -> None:
    """Active mode: Ghosthunter directly queries the cloud and runs commands."""
    cfg = _require_config()
    if cfg.provider and cfg.provider != provider:
        console.print(
            f"[yellow]Config has provider={cfg.provider}, command-line "
            f"picked {provider}. Using {provider}.[/yellow]"
        )

    if provider == "aws":
        aws_cfg = cfg.aws or AWSConfig()
        # Cost Explorer API is metered (~$0.01 per request). Warn once per
        # machine and persist the ack so scripted runs aren't interrupted.
        if not aws_cfg.ce_api_cost_ack:
            console.print(
                "[yellow]Cost Explorer API calls are billed at ~$0.01 each.[/yellow]\n"
                "[dim]A typical investigation makes 2-6 calls. This notice is "
                "shown once; Ghosthunter persists your acknowledgment in "
                "~/.ghosthunter/config.toml.[/dim]"
            )
            if not Confirm.ask("Proceed?", default=True):
                raise typer.Exit(0)
            aws_cfg.ce_api_cost_ack = True
            cfg.aws = aws_cfg
            cfg.save()

        ce_calls: list[dict] = []

        def _record_ce_call(op: str, params: dict) -> None:
            ce_calls.append({"operation": op, "params": params})

        prov = AWSProvider(
            profile=aws_cfg.profile,
            region=aws_cfg.region,
            account_id=aws_cfg.account_id,
            on_ce_call=_record_ce_call,
        )
        label = (
            f"{aws_cfg.account_id or 'default'} "
            f"({aws_cfg.region}, profile={aws_cfg.profile or 'default'})"
        )
    else:
        prov = GCPProvider(
            project_id=cfg.project_id,
            billing_dataset=cfg.billing_dataset,
        )
        label = cfg.project_id

    console.print(
        f"[cyan]Fetching billing data for {label} "
        f"(last {cfg.lookback_days} days)...[/cyan]"
    )
    spikes = prov.fetch_billing_spikes(lookback_days=cfg.lookback_days)
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

    investigator = _build_active_investigator(prov, cfg, provider=provider)
    result = asyncio.run(investigator.investigate(spike))
    _render_result(result)
    extra: dict = {"provider": provider}
    if provider == "aws":
        extra["ce_api_calls"] = len(ce_calls)
    _append_audit_log(result, extra=extra)


def _run_advisor_mode(
    billing_files: list[Path],
    spike_index: int,
    list_only: bool,
    provider: str = "gcp",
) -> None:
    """Advisor mode: parse billing files, advise commands, never touch cloud."""
    console.print(
        Panel(
            f"[bold]Advisor mode[/bold] ([cyan]{provider}[/cyan]) — "
            "Ghosthunter will [bold]not[/bold] touch your cloud.\n"
            "It reads your billing export, proposes read-only commands,\n"
            "and asks you to run them yourself and paste back the output.",
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

    investigator = _build_advisor_investigator(provider=provider)
    result = asyncio.run(investigator.investigate(spike))
    _render_result(result)
    _append_audit_log(result, extra={"provider": provider})


# ---------------------------------------------------------------------------
# Provider resolution — sniff from files and/or config
# ---------------------------------------------------------------------------
_AWS_COLUMN_SIGNATURES = (
    "lineitem/",
    "product/productname",
    "unblendedcost",
    "blendedcost",
    "usageaccountid",
    "timeperiodstart",
    "linked account",
    "usagetype",
)


def _sniff_provider_from_file(path: Path) -> str | None:
    """Peek at a billing file's header row and guess the provider.

    Returns 'aws', 'gcp', or None if we can't tell.
    """
    import csv as _csv
    import json as _json

    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            with path.open() as f:
                data = _json.load(f)
            # CE get-cost-and-usage response shape is unambiguously AWS.
            if isinstance(data, dict) and "ResultsByTime" in data:
                return "aws"
            # Otherwise look at first row's keys.
            if isinstance(data, dict):
                for key in ("rows", "data", "billing", "results"):
                    if key in data and isinstance(data[key], list):
                        data = data[key]
                        break
            if isinstance(data, list) and data and isinstance(data[0], dict):
                keys = " ".join(str(k).lower() for k in data[0].keys())
                if any(sig in keys for sig in _AWS_COLUMN_SIGNATURES):
                    return "aws"
                if "service.description" in keys or "project.id" in keys:
                    return "gcp"
            return None

        with path.open(newline="") as f:
            reader = _csv.reader(f)
            headers = next(reader, [])
        header_blob = " ".join(h.lower() for h in headers)
        if any(sig in header_blob for sig in _AWS_COLUMN_SIGNATURES):
            return "aws"
        if "service description" in header_blob or "project id" in header_blob:
            return "gcp"
        return None
    except Exception:
        return None


def _resolve_provider(
    flag_value: str, files: list[Path], for_active: bool
) -> str:
    """Pick 'gcp' or 'aws' based on --provider, billing files, and config.

    Precedence:
      1. Explicit --provider (not 'auto') wins.
      2. Sniff billing files — if all signal AWS or all signal GCP, use that.
      3. Config file provider (if present).
      4. Default to 'gcp'.
    """
    value = (flag_value or "auto").lower()
    if value in ("gcp", "aws"):
        return value

    if value != "auto":
        console.print(
            f"[red]Unknown provider '{flag_value}'. Use 'gcp', 'aws', or 'auto'.[/red]"
        )
        raise typer.Exit(1)

    # --- Auto: sniff ---
    if files:
        sniffed = {_sniff_provider_from_file(p) for p in files}
        sniffed.discard(None)
        if sniffed == {"aws"}:
            return "aws"
        if sniffed == {"gcp"}:
            return "gcp"
        # Mixed or unknown → fall through to config / default.

    # --- Config ---
    if CONFIG_PATH.exists():
        try:
            cfg = Config.load()
            if cfg.provider in ("gcp", "aws"):
                return cfg.provider
        except Exception:
            pass

    if for_active and not CONFIG_PATH.exists():
        console.print(
            "[yellow]No config and no billing files to sniff. "
            "Defaulting to --provider=gcp.[/yellow]"
        )
    return "gcp"


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
             "GCP: dns_cache_bypass, nat_egress_runaway, bigquery_full_scan, "
             "orphaned_disks, gke_autoscaler_loop. "
             "AWS: aws_nat_gateway_runaway, aws_s3_lifecycle_miss.",
    ),
    provider: Optional[str] = typer.Option(
        None,
        "--provider",
        help="Only pick scenarios for this provider: 'gcp' or 'aws'. "
             "Random choice across all providers if omitted.",
    ),
) -> None:
    """Replay a bundled investigation. No API calls, no setup."""
    from ghosthunter.demo import run_demo

    asyncio.run(run_demo(console, scenario_id=scenario, provider_filter=provider))


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
    console.print(_build_audit_table(lines[-limit:]))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _require_config() -> Config:
    try:
        # Silently upgrade old configs that predate the `provider` field.
        # No-op when the file already carries the new shape or is missing.
        migrate_config_in_place()
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


def _build_active_investigator(
    provider_obj, cfg: Config, provider: str = "gcp"
) -> Investigator:
    budget = Budget(
        max_commands=cfg.budget.max_commands,
        max_cost_usd=cfg.budget.max_cost_usd,
        max_seconds=cfg.budget.max_seconds,
    )
    return Investigator(
        provider=provider_obj,
        reasoner=Reasoner(provider=provider),
        executor=Executor(provider=provider),
        validator=SecurityValidator(provider=provider),
        approval_hook=_interactive_approval,
        event_hook=_InvestigationRenderer(console),
        budget=budget,
    )


async def _auto_approve(_: PendingCommand) -> str:
    """Advisor-mode approval is collapsed into the print-and-wait flow,
    so the investigator-level approval prompt is a no-op."""
    return "approve"


def _build_advisor_investigator(provider: str = "gcp") -> Investigator:
    """Investigator with AdvisorProvider — never touches the cloud."""
    validator = SecurityValidator(provider=provider)
    advisor = AdvisorProvider(
        validator=validator, console=console, provider_key=provider
    )
    return Investigator(
        provider=advisor,  # type: ignore[arg-type]
        reasoner=Reasoner(provider=provider),
        executor=Executor(provider=provider),
        validator=validator,
        approval_hook=_auto_approve,  # AdvisorProvider handles user interaction
        event_hook=_InvestigationRenderer(console),
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


class _InvestigationRenderer:
    """Event → console renderer with animated spinner phases.

    Mirrors the Claude Code UX: a ``✻``-style spinner runs whenever the
    backend is doing work the user can't see (Opus reasoning, Sonnet
    Layer-6 validation, Sonnet compression) and stops around anything
    the user needs to read or respond to (hypothesis bars, reasoning
    panel, proposed command, need_info question).

    One instance per investigation — the spinner state is per-run and
    must not leak across calls.
    """

    # Label for each "thinking" phase the spinner covers.
    _PHASE_LABELS = {
        "step_started":       "Opus is reasoning",
        "command_approved":   "Sonnet is validating the command",
        "command_executed":   "Sonnet is compressing command output",
    }

    def __init__(self, console: Console) -> None:
        self.console = console
        self._status: Status | None = None
        self._start_ts: float = 0.0

    async def __call__(self, event: InvestigationEvent) -> None:
        kind = event.kind

        # --- "thinking" phases: start/replace the spinner ---
        if kind in self._PHASE_LABELS:
            self._spin(self._PHASE_LABELS[kind])
            return

        # Every other event terminates whatever was spinning before it.
        self._stop_spin()

        if kind == "hypotheses_updated":
            self.console.print("\n[bold]Hypotheses:[/bold]")
            for h in event.payload["hypotheses"]:
                bar = "█" * (h["confidence"] // 5)
                self.console.print(
                    f"  {h['id']} [{h['status']:>10}] {h['confidence']:3}% "
                    f"{bar:20} {h['description']}"
                )
            return

        if kind == "reasoning":
            text = (event.payload.get("text") or "").strip()
            if text:
                self.console.print()
                self.console.print(
                    Panel(
                        text,
                        title="[bold magenta]Opus[/bold magenta]",
                        border_style="magenta",
                        expand=False,
                    )
                )
            return

        if kind == "command_blocked":
            self.console.print(
                f"[red]✗ blocked ({event.payload['layer']}):[/red] "
                f"{event.payload['reason']}"
            )
            return

        if kind == "command_rejected_by_user":
            self.console.print("[dim]→ command skipped[/dim]")
            return

        if kind == "evidence_added":
            e = event.payload["evidence"]
            summary = (e.summary or "").split("\n", 1)[0][:120]
            self.console.print(f"[green]+ {e.id}[/green] {summary}")
            return

        if kind == "concluded":
            self.console.print(
                "\n[bold green]✓ Investigation concluded[/bold green]"
            )
            return

        if kind == "aborted":
            self.console.print(
                f"\n[bold red]✗ Aborted:[/bold red] {event.payload['reason']}"
            )
            return

        # command_proposed / opus_asks / spike_selected / command_approved
        # results / user_note: the AdvisorProvider (or spike-selection
        # phase) handles the visible output for these. Nothing extra for
        # the renderer to do.

    # ------------------------------------------------------------------
    def _spin(self, text: str) -> None:
        """Start (or replace) the spinner with a new phase label."""
        self._stop_spin()
        self._status = self.console.status(
            f"[cyan]✻ {text}…[/cyan]",
            spinner="dots12",
        )
        self._status.start()
        self._start_ts = time.monotonic()

    def _stop_spin(self) -> None:
        """Stop the spinner if running; print a dim elapsed-time footer."""
        if self._status is None:
            return
        elapsed = time.monotonic() - self._start_ts
        self._status.stop()
        self._status = None
        if elapsed >= 0.5:
            # A faint "(3.2s)" line under the spinner so the user sees
            # how long each backend phase actually took.
            self.console.print(f"[dim]  ({elapsed:.1f}s)[/dim]")


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


def _append_audit_log(result, extra: dict | None = None) -> None:
    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "service": result.spike.service,
        "succeeded": result.succeeded,
        "commands_used": result.budget.commands_used,
        "conclusion": result.conclusion,
        "aborted_reason": result.aborted_reason,
    }
    if extra:
        entry.update(extra)
    with AUDIT_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


if __name__ == "__main__":
    app()
