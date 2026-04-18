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
from ghosthunter.ui import render_command_blocked
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
    from ghosthunter.preflight import run_preflight_aws, run_preflight_gcp

    cfg = _require_config()
    if cfg.provider and cfg.provider != provider:
        console.print(
            f"[yellow]Config has provider={cfg.provider}, command-line "
            f"picked {provider}. Using {provider}.[/yellow]"
        )

    # Preflight: catch missing deps / bad creds / missing permissions
    # BEFORE any cloud call. Turns "crash with a traceback" into
    # "panel + prompt". See src/ghosthunter/preflight.py for the full
    # check list per provider.
    preflight_runner = run_preflight_aws if provider == "aws" else run_preflight_gcp
    if not preflight_runner(cfg, console):
        console.print(
            "[yellow]Preflight aborted — fix the issue above and re-run.[/yellow]"
        )
        raise typer.Exit(1)

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

        # Surface the identity the preflight just verified, so the user
        # can confirm they're hitting the account they expected.
        identity = getattr(aws_cfg, "_last_sts_identity", None)
        if identity:
            console.print(
                f"[dim]Verified identity: account {identity.get('Account')} · "
                f"{identity.get('Arn', 'unknown ARN').rsplit('/', 1)[-1]}[/dim]"
            )

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

    # Mid-investigation spike switches (`/spike N` typed at an Opus
    # question prompt) raise AdvisorSpikeSwitch, which the chat session
    # catches and restarts on the new target. The direct CLI path
    # didn't catch it pre-v1.0.4, so it leaked as an unhandled traceback.
    # We now loop: each pass investigates one spike; AdvisorSpikeSwitch
    # simply rebinds the target and starts fresh. A small cap on
    # switches guards against any pathological loop.
    current_index = spike_index
    switch_cap = 20
    switches_used = 0

    while True:
        spike = _pick_spike(spikes, current_index)
        console.print(
            f"\n[bold]Investigating[/bold] {spike.service} "
            f"(${spike.current_cost:,.0f})"
        )
        _render_top_contributors(spike)
        console.print()

        investigator = _build_advisor_investigator(provider=provider)
        try:
            result = asyncio.run(investigator.investigate(spike))
        except Exception as exc:
            # Lazy import — AdvisorSpikeSwitch lives in the advisor
            # provider, which is an optional module.
            try:
                from ghosthunter.providers.advisor import AdvisorSpikeSwitch
            except ImportError:
                raise
            if not isinstance(exc, AdvisorSpikeSwitch):
                raise
            switches_used += 1
            if switches_used > switch_cap:
                console.print(
                    f"[yellow]Too many spike switches in one session "
                    f"({switch_cap}). Start a fresh `ghosthunter "
                    f"investigate` to continue.[/yellow]"
                )
                raise typer.Exit(1) from exc
            if exc.target_index < 0 or exc.target_index >= len(spikes):
                console.print(
                    f"[yellow]No spike #{exc.target_index} — "
                    f"valid range is 0..{len(spikes) - 1}. Staying on "
                    f"{spike.service}.[/yellow]"
                )
                continue
            console.print(
                f"[dim]Switching to spike #{exc.target_index} "
                f"({spikes[exc.target_index].service}) per your "
                f"request…[/dim]"
            )
            current_index = exc.target_index
            continue
        break

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

# FOCUS 1.0 has the same column schema across all providers — the only
# native signal is the per-row ``ProviderName`` value. These are the
# strings the major clouds write there.
_FOCUS_PROVIDER_NAME_AWS = ("aws",)
_FOCUS_PROVIDER_NAME_GCP = ("google cloud", "google")
# Azure/Oracle/etc. aren't supported providers — we return None for them
# and let the caller fall through to config / default.

# How many rows to peek at when majority-voting on a FOCUS file's
# ``ProviderName`` or ``ServiceName`` column.
_FOCUS_PEEK_ROWS = 200


def _sniff_provider_from_file(path: Path) -> str | None:
    """Peek at a billing file's header row and guess the provider.

    Returns 'aws', 'gcp', or None if we can't tell. Understands:
      - AWS CUR columns (``lineItem/*``, ``UsageAccountId``, …)
      - AWS Cost Explorer JSON (``ResultsByTime``)
      - GCP Console CSV (``Service description`` / ``Project ID``)
      - FOCUS 1.0 CSV (uses the per-row ``ProviderName`` column; falls
        back to majority-voting ``ServiceName`` prefixes if needed)

    For FOCUS data that's majority-Azure/Oracle/etc., returns None so
    the caller can print a helpful "we don't have a reasoner for that
    provider, defaulting to …" note rather than silently mis-routing.
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
                # FOCUS JSON — same logic as CSV below.
                if "providername" in keys:
                    return _sniff_focus_rows(data[:_FOCUS_PEEK_ROWS])
            return None

        with path.open(newline="") as f:
            reader = _csv.DictReader(f)
            headers = [h.lower() for h in (reader.fieldnames or [])]
            header_blob = " ".join(headers)
            if any(sig in header_blob for sig in _AWS_COLUMN_SIGNATURES):
                return "aws"
            if "service description" in header_blob or "project id" in header_blob:
                return "gcp"
            # FOCUS 1.0: distinguish by peeking at actual data rows.
            if "providername" in headers or "servicename" in headers:
                sample: list[dict[str, Any]] = []
                for row in reader:
                    sample.append(row)
                    if len(sample) >= _FOCUS_PEEK_ROWS:
                        break
                return _sniff_focus_rows(sample)
        return None
    except Exception:
        return None


def _sniff_focus_rows(rows: list[dict[str, Any]]) -> str | None:
    """Majority-vote a FOCUS row sample into aws / gcp / None.

    Uses ``ProviderName`` when present (it's the canonical FOCUS column
    for this). Falls back to ``ServiceName`` prefix matching (``Amazon
    …`` / ``AWS …`` → aws; ``Cloud …``, ``BigQuery``, ``Compute Engine``
    → gcp).

    Returns None when the sample is dominated by a provider Ghosthunter
    doesn't support (Azure, Oracle, …) so the caller falls through to
    config / default rather than silently mis-routing to the wrong
    reasoner rules.
    """
    if not rows:
        return None

    counts = {"aws": 0, "gcp": 0, "other": 0}

    for row in rows:
        # Prefer the explicit ProviderName column.
        provider_name = None
        for k, v in row.items():
            if str(k).lower() == "providername" and isinstance(v, str):
                provider_name = v.strip().lower()
                break

        if provider_name:
            if any(m in provider_name for m in _FOCUS_PROVIDER_NAME_AWS):
                counts["aws"] += 1
                continue
            if any(m in provider_name for m in _FOCUS_PROVIDER_NAME_GCP):
                counts["gcp"] += 1
                continue
            counts["other"] += 1
            continue

        # No ProviderName column — classify by ServiceName prefix.
        service_name = None
        for k, v in row.items():
            if str(k).lower() == "servicename" and isinstance(v, str):
                service_name = v.strip()
                break
        if not service_name:
            continue
        if (
            service_name.startswith("Amazon ")
            or service_name.startswith("AWS ")
            or service_name.startswith("AmazonCloudWatch")
            or service_name == "EC2 - Other"
        ):
            counts["aws"] += 1
        elif (
            service_name.startswith("Cloud ")
            or service_name in ("BigQuery", "Compute Engine", "Kubernetes Engine")
            or service_name.startswith("Vertex ")
            or service_name.startswith("Gemini ")
        ):
            counts["gcp"] += 1
        else:
            counts["other"] += 1

    # Require a clear majority and that it's a supported provider.
    total = sum(counts.values())
    if total == 0:
        return None
    if counts["aws"] >= counts["gcp"] and counts["aws"] > counts["other"]:
        return "aws"
    if counts["gcp"] > counts["aws"] and counts["gcp"] > counts["other"]:
        return "gcp"
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
    """Show the top SKUs/projects/locations driving a spike, if known.

    When a contributor has a ``ChargeDescription`` (FOCUS 1.0 / CUR),
    print the description on a second indented line — this is often
    what actually tells the user (and the reasoner) what a cryptic SKU
    ID is: e.g. ``4GQWNPC9K2PZAY97`` followed by ``$1.624 per On Demand
    Linux g5.4xlarge Instance Hour``.
    """
    if not spike.top_contributors:
        return
    descriptions = getattr(spike, "contributor_descriptions", {}) or {}
    for dim, items in spike.top_contributors.items():
        if not items:
            continue
        console.print(f"[dim]Top {dim}s:[/dim]")
        for name, cost in items[:5]:
            console.print(f"  • {name:<60} ${cost:>12,.2f}")
            desc = descriptions.get(f"{dim}:{name}")
            if desc:
                # Trim very long descriptions so the terminal stays tidy
                # — Opus still gets the full text via the prompt.
                display = desc if len(desc) <= 96 else desc[:93] + "…"
                console.print(f"    [dim italic]{display}[/dim italic]")


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

    The spinner's label includes a short **what it's working on** preview
    so the user sees e.g.::

        ✻ Opus is reasoning · turn 4 · 2 hypotheses active…
        ✻ Sonnet is validating · aws ce get-cost-and-usage --time-period…
        ✻ Sonnet is compressing · 18.4 KB of command output…

    One instance per investigation — the spinner state is per-run and
    must not leak across calls.
    """

    def __init__(self, console: Console) -> None:
        self.console = console
        self._status: Status | None = None
        self._start_ts: float = 0.0
        # Running counters for the reasoning spinner's detail line.
        self._turn: int = 0
        self._active_hypotheses: int = 0

    async def __call__(self, event: InvestigationEvent) -> None:
        kind = event.kind

        # --- "thinking" phases — start/replace the spinner with a
        #     context-rich label ---
        if kind == "step_started":
            self._turn += 1
            suffix = f"turn {self._turn}"
            if self._active_hypotheses:
                suffix += f" · {self._active_hypotheses} active hypothes{'es' if self._active_hypotheses != 1 else 'is'}"
            self._spin(f"Opus is reasoning · {suffix}")
            return

        if kind == "command_proposed":
            # Sonnet Layer-6 validation happens between command_proposed
            # and command_approved in the investigator loop. That's the
            # right moment to spin "Sonnet is validating" — not when
            # validation already finished (which is command_approved).
            pending = event.payload.get("pending")
            cmd = getattr(pending, "command", "") or ""
            preview = _preview_command(cmd)
            self._spin(f"Sonnet is validating · {preview}")
            return

        if kind == "compressing":
            cmd = event.payload.get("command") or ""
            byte_count = int(event.payload.get("bytes") or 0)
            preview = _preview_command(cmd)
            size = _fmt_bytes(byte_count)
            self._spin(
                f"Sonnet is compressing · {size} from '{preview}'"
            )
            return

        # Every other event terminates whatever was spinning before it.
        self._stop_spin()

        if kind == "hypotheses_updated":
            hyps = event.payload["hypotheses"]
            self._active_hypotheses = sum(
                1 for h in hyps if h.get("status") == "active"
            )
            self.console.print("\n[bold]Hypotheses:[/bold]")
            for h in hyps:
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

        if kind == "command_approved":
            # Sonnet validation passed. The AdvisorProvider is about to
            # take over and ask the user to paste output. A single dim
            # line tells the user *why* we're paused.
            self.console.print(
                "[dim]  ✓ command passed Layer-6 validation[/dim]"
            )
            return

        if kind == "command_blocked":
            render_command_blocked(
                self.console,
                command=event.payload.get("command"),
                layer=event.payload.get("layer", "?"),
                reason=event.payload.get("reason", "(no reason given)"),
            )
            return

        if kind == "command_rejected_by_user":
            self.console.print("[dim]→ command skipped[/dim]")
            return

        if kind == "command_executed":
            # Advisor mode: user just pasted output. Print a dim size/duration
            # line so the user sees we're moving from exec → compress.
            result = event.payload.get("result")
            if result is not None:
                size = _fmt_bytes(len(getattr(result, "stdout", "") or ""))
                dur = getattr(result, "duration_seconds", 0)
                self.console.print(
                    f"[dim]  ← received {size} in {dur:.1f}s[/dim]"
                )
            return

        if kind == "evidence_added":
            e = event.payload["evidence"]
            summary = (e.summary or "").split("\n", 1)[0][:120]
            self.console.print(f"[green]+ {e.id}[/green] {summary}")
            return

        if kind == "user_note":
            note = (event.payload.get("note") or "").strip()
            if note:
                self.console.print(
                    f"[dim]  → note to Opus: {note[:80]}[/dim]"
                )
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

        # spike_selected / opus_asks are handled by the AdvisorProvider's
        # own panels; nothing extra for the renderer to do.

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


# ---------------------------------------------------------------------------
# Spinner detail-line helpers
# ---------------------------------------------------------------------------
_MAX_CMD_PREVIEW = 60


def _preview_command(command: str) -> str:
    """Squash a command into one line ≤60 chars with an ellipsis."""
    single_line = " ".join(command.split())
    if len(single_line) <= _MAX_CMD_PREVIEW:
        return single_line
    return single_line[: _MAX_CMD_PREVIEW - 1] + "…"


def _fmt_bytes(n: int) -> str:
    """Format a byte count as a short human-readable string."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


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

        # ---- FIX-FIRST LAYOUT ----
        # Recommendations render first so the user's eye lands on "what
        # do I do next?" not on a long prose paragraph. Commands inside
        # recommendations print in the same paste-safe ASCII format as
        # mid-investigation command proposals (no Unicode borders,
        # soft_wrap, markup disabled) so triple-click copies cleanly.
        if c.get("recommendations"):
            console.print("\n[bold]What to do now[/bold]")
            _render_recommendations(c["recommendations"])

        # ---- Root cause + confidence ----
        console.print("\n[bold]Root cause[/bold]")
        console.print(
            f"  {c.get('root_cause', '?')} "
            f"[dim]({c.get('confidence', '?')}% confidence)[/dim]"
        )

        # ---- Evidence that supports the call ----
        if c.get("evidence_summary"):
            console.print("\n[bold]Evidence[/bold]")
            for e in c["evidence_summary"]:
                console.print(f"  • {e}")

        # ---- Honest gaps ----
        if c.get("not_verified"):
            console.print("\n[bold]What we couldn't verify[/bold]")
            for n in c["not_verified"]:
                console.print(f"  ? {n}")

    console.print(
        f"\n[dim]Commands used: {result.budget.commands_used}/"
        f"{result.budget.max_commands}  •  "
        f"Time: {result.budget.seconds_used:.0f}s/"
        f"{result.budget.max_seconds:.0f}s[/dim]"
    )


# Urgency → short label shown to the user. Order matters (rendered in
# this sequence regardless of the order Opus emitted them).
_URGENCY_LABELS: dict[str, str] = {
    "immediate": "[bold red]NOW[/bold red]",
    "this_week": "[bold yellow]THIS WEEK[/bold yellow]",
    "this_month": "[bold blue]THIS MONTH[/bold blue]",
    "monitoring": "[bold cyan]MONITORING[/bold cyan]",
}
_URGENCY_ORDER = ("immediate", "this_week", "this_month", "monitoring")


def _render_recommendations(items: list) -> None:
    """Render a ``conclusion.recommendations`` list.

    Each item is either a plain string (legacy v1.0.4-and-older shape,
    still emitted by Opus if it slips schema) or an object:
        {urgency, description, command?, verification?}

    Structured items get an urgency label, the description, and a
    paste-safe command block for both ``command`` and ``verification``
    when present.
    """
    # Split into structured + legacy strings, then sort structured by
    # canonical urgency order while preserving Opus's order within
    # each bucket.
    structured: list[dict] = []
    legacy_strings: list[str] = []
    for item in items:
        if isinstance(item, dict):
            structured.append(item)
        elif isinstance(item, str):
            legacy_strings.append(item)
        else:
            legacy_strings.append(str(item))

    def _urgency_rank(item: dict) -> int:
        urg = item.get("urgency", "monitoring")
        try:
            return _URGENCY_ORDER.index(urg)
        except ValueError:
            return len(_URGENCY_ORDER)

    # Python's sort is stable, so items with the same urgency preserve
    # the order Opus emitted them.
    structured.sort(key=_urgency_rank)

    # If there's exactly one recommendation with a command, we push it
    # onto the clipboard via OSC 52 so terminals that support it give
    # the user the "done" action ready to paste. When multiple
    # recommendations have commands, auto-copying one would be
    # arbitrary — users run /copy on the investigation's last proposed
    # command, or select manually. So: only auto-copy when
    # unambiguous.
    commands_in_recs = [
        it.get("command") for it in structured
        if it.get("command")
    ]

    for item in structured:
        urgency = item.get("urgency", "monitoring")
        label = _URGENCY_LABELS.get(urgency, urgency.upper())
        description = item.get("description", "") or "(no description)"
        console.print(f"\n  {label}  {description}")

        command = item.get("command")
        verification = item.get("verification")

        if command:
            # Same paste-safe layout as the mid-investigation command
            # proposals. No borders, no Rich markup parsing on the
            # command itself, soft_wrap so the terminal handles visual
            # wrap without breaking the string.
            console.print("    [dim]-- Run this command --[/dim]")
            console.print(
                "      " + command,
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
        if verification:
            console.print("    [dim]-- Verify with --[/dim]")
            console.print(
                "      " + verification,
                markup=False,
                highlight=False,
                soft_wrap=True,
            )

    if len(commands_in_recs) == 1:
        # Exactly one remediation command — auto-copy it. Multiple
        # commands → don't guess which the user wants, they pick
        # manually.
        try:
            from ghosthunter.clipboard import write_osc52
            if write_osc52(
                commands_in_recs[0],
                stream=getattr(console, "file", None),
            ):
                console.print(
                    "\n[dim italic]The single remediation command "
                    "above has been placed on your clipboard.[/dim italic]"
                )
        except Exception:
            pass

    for s in legacy_strings:
        console.print(f"  → {s}")


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
