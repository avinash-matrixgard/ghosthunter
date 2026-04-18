"""Ghosthunter chat orchestrator.

A single REPL that owns the entire investigation lifecycle. Users never
have to leave this prompt — they load billing files, pick spikes, run
investigations, drop notes mid-flight, and start new investigations,
all from one chat session.

State machine:

    IDLE  ──/load──▶  READY  ──/spike N──▶  INVESTIGATING
                                                    │
                       ◀──── /quit / conclude ──────┘

Slash command surface (see `/help`):

    /load FILE [FILE...]   parse one or more billing files
    /list                  reshow detected spikes
    /spike N               investigate spike N
    /history               show what's been investigated this session
    /help                  context-sensitive help
    /exit                  leave chat session

Inside an investigation, the AdvisorProvider's prompt also accepts:

    <file path>      paste a file containing the command output
    <pasted lines>   ending with a line containing only ###
    /skip            skip the current command (Opus tries another angle)
    /note <text>     send Opus a note (and skip this command)
    /hypotheses      show current hypothesis state
    /quit            end investigation, return to spike picker
"""
from __future__ import annotations

import asyncio
import glob as glob_mod
import shlex

from ghosthunter.chat_io import read_line
from ghosthunter.memory import (
    MemoryHit,
    default_wing_for_files,
    get_palace,
    is_available as palace_is_available,
)
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ghosthunter.evidence import Evidence
from ghosthunter.hypothesis import Hypothesis
from ghosthunter.investigator import (
    Budget,
    InvestigationEvent,
    InvestigationResult,
    Investigator,
    PendingCommand,
)
from ghosthunter.models.executor import Executor
from ghosthunter.models.reasoner import Reasoner
from ghosthunter.providers.advisor import AdvisorProvider, AdvisorSpikeSwitch
from ghosthunter.providers.billing_file import (
    BillingFileError,
    load_spikes_from_files,
)
from ghosthunter.providers.gcp import CostSpike
from ghosthunter.security.validator import SecurityValidator


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
@dataclass
class SessionHistoryEntry:
    timestamp: datetime
    spike_label: str
    succeeded: bool
    summary: str
    commands_used: int


@dataclass
class ChatSession:
    console: Console
    spikes: list[CostSpike] = field(default_factory=list)
    loaded_files: list[Path] = field(default_factory=list)
    history: list[SessionHistoryEntry] = field(default_factory=list)
    current_hypotheses: list[Hypothesis] = field(default_factory=list)
    current_evidence: list[Evidence] = field(default_factory=list)
    in_investigation: bool = False
    # Cloud provider for this session — "gcp" (default) or "aws". Drives
    # which allowlist, reasoner prompt, and sandbox env get used during
    # investigations. Set at mode-picker time and/or sniffed on /load.
    provider: str = "gcp"
    # Memory palace — persistent cross-session knowledge about this billing
    # account. `wing` is the current wing (e.g. "[COMPANY]"), picked from the
    # loaded files on /load.
    wing: str = "default"
    memory_enabled: bool = False


# ---------------------------------------------------------------------------
# Mode picker
# ---------------------------------------------------------------------------
@dataclass
class ModeOption:
    key: str
    title: str
    icon: str
    description: str
    available: bool
    badge: str | None = None  # e.g. "recommended", "coming v1.1"


MODES: list[ModeOption] = [
    ModeOption(
        key="paranoid",
        title="Paranoid (advisor)",
        icon="🔒",
        description=(
            "Ghosthunter never touches your cloud. You provide billing exports,\n"
            "    it proposes read-only commands, you run them in your own terminal\n"
            "    and paste the output back. Zero credentials. Works in any\n"
            "    locked-down corporate environment."
        ),
        available=True,
        badge="recommended",
    ),
    ModeOption(
        key="active",
        title="Active (direct)",
        icon="⚡",
        description=(
            "Ghosthunter has read-only GCP credentials and runs commands itself.\n"
            "    Faster loop, but requires a service account or your own creds.\n"
            "    Needs ~/.ghosthunter/config.toml. Use only on a sandbox project\n"
            "    where your IAM is scoped to viewer-tier roles."
        ),
        available=True,
    ),
    ModeOption(
        key="demo",
        title="Demo",
        icon="🎭",
        description=(
            "Replay a bundled investigation. GCP: DNS attack, NAT runaway,\n"
            "    BigQuery full scan, orphaned disks, GKE autoscaler loop.\n"
            "    AWS: NAT gateway runaway, S3 lifecycle miss. No API calls,\n"
            "    no cloud, no setup — best way to see how Ghosthunter works."
        ),
        available=True,
    ),
    ModeOption(
        key="audit",
        title="Audit log",
        icon="📋",
        description="Show past investigations from ~/.ghosthunter/audit.log.",
        available=True,
    ),
    ModeOption(
        key="autonomous",
        title="Autonomous (with guardrails)",
        icon="🤖",
        description=(
            "Ghosthunter runs the entire investigation without per-command\n"
            "    approval. Strict budget and IAM guardrails."
        ),
        available=False,
        badge="coming v1.1",
    ),
    ModeOption(
        key="aws",
        title="AWS (paranoid advisor)",
        icon="☁️",
        description=(
            "Same flow as paranoid GCP, but for AWS. You export a Cost Explorer\n"
            "    CSV / JSON or a CUR file, Ghosthunter proposes read-only `aws`\n"
            "    commands, you run them and paste the output back. Zero AWS\n"
            "    credentials in Ghosthunter. Active-mode AWS lands in v1.1."
        ),
        available=True,
    ),
    ModeOption(
        key="azure",
        title="Azure provider",
        icon="☁️",
        description="Cost spike investigations against Azure Cost Management.",
        available=False,
        badge="coming v1.2",
    ),
]


def _print_mode_picker(console: Console) -> None:
    console.print(
        Panel(
            (
                "[bold]Ghosthunter[/bold] — interactive cost-spike investigator\n\n"
                "Pick how you want to run:"
            ),
            border_style="cyan",
        )
    )
    console.print()

    for i, mode in enumerate(MODES, start=1):
        if mode.available:
            num_style = "bold cyan"
            title_style = "bold"
            desc_style = "white"
        else:
            num_style = "dim"
            title_style = "dim"
            desc_style = "dim"

        badge = ""
        if mode.badge == "recommended":
            badge = " [green](recommended)[/green]"
        elif mode.badge:
            badge = f" [yellow]({mode.badge})[/yellow]"

        console.print(
            f"  [{num_style}]{i}.[/{num_style}] {mode.icon}  "
            f"[{title_style}]{mode.title}[/{title_style}]{badge}"
        )
        console.print(f"     [{desc_style}]{mode.description}[/{desc_style}]")
        console.print()

    console.print(
        f"  [dim]q.[/dim]  Quit"
    )
    console.print()


def _pick_mode(console: Console) -> ModeOption | None:
    """Show the mode picker and return the chosen mode (or None on quit)."""
    while True:
        _print_mode_picker(console)
        try:
            answer = read_line("Pick a mode [1-7, q]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None

        if not answer or answer in ("q", "quit", "exit"):
            return None

        try:
            idx = int(answer) - 1
        except ValueError:
            console.print(
                f"[yellow]Not a number: '{answer}'. Pick 1-{len(MODES)} or q.[/yellow]\n"
            )
            continue

        if idx < 0 or idx >= len(MODES):
            console.print(
                f"[yellow]Out of range. Pick 1-{len(MODES)} or q.[/yellow]\n"
            )
            continue

        mode = MODES[idx]
        if not mode.available:
            console.print(
                f"[yellow]{mode.title} is {mode.badge or 'not available yet'}. "
                "Pick another mode.[/yellow]\n"
            )
            continue

        return mode


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def run_chat(
    initial_files: list[Path] | None = None,
    console: Console | None = None,
    skip_mode_picker: bool = False,
) -> None:
    """Open a chat session and block until the user types /exit.

    By default, shows a mode picker on startup so the user chooses paranoid
    / active / demo / audit. Pass `skip_mode_picker=True` to jump straight
    into paranoid mode (used when files are provided on the CLI).
    """
    console = console or Console()

    # Mode picker — only when no files were pre-loaded
    chosen_mode_key: str | None = None
    if not skip_mode_picker and not initial_files:
        mode = _pick_mode(console)
        if mode is None:
            console.print("[dim]bye.[/dim]")
            return
        if mode.key == "demo":
            from ghosthunter.demo import run_demo
            asyncio.run(run_demo(console))
            return
        if mode.key == "audit":
            from ghosthunter.cli import _render_audit_table
            _render_audit_table(console)
            return
        if mode.key == "active":
            from ghosthunter.cli import _run_active_mode_interactive
            _run_active_mode_interactive(console)
            return
        chosen_mode_key = mode.key
        # mode.key in ("paranoid", "aws") → fall through to the chat loop.
        # AWS mode is paranoid-flavored with a different provider key.

    session = ChatSession(console=console)
    if chosen_mode_key == "aws":
        session.provider = "aws"

    _print_welcome(console)

    if initial_files:
        _cmd_load(session, initial_files)

    while True:
        try:
            line = read_line("ghosthunter> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye.[/dim]")
            return

        if not line:
            continue

        if line.startswith("/"):
            try:
                _dispatch_slash(session, line)
            except _ExitChat:
                console.print("[dim]bye.[/dim]")
                return
            continue

        # Free text outside an investigation — gentle hint
        console.print(
            "[dim]Type [bold]/help[/bold] for commands. To start an "
            "investigation: [bold]/load <file>[/bold] then "
            "[bold]/spike <n>[/bold].[/dim]"
        )


# ---------------------------------------------------------------------------
# Slash dispatcher (top-level chat REPL)
# ---------------------------------------------------------------------------
class _ExitChat(Exception):
    """Internal sentinel that propagates /exit out of nested calls."""


def _dispatch_slash(session: ChatSession, line: str) -> None:
    parts = shlex.split(line)
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in ("/exit", "/quit") and not session.in_investigation:
        raise _ExitChat()

    if cmd == "/help":
        _print_help(session.console, in_investigation=session.in_investigation)
        return

    if cmd == "/load":
        if not args:
            session.console.print(
                "[yellow]Usage: /load FILE [FILE ...]   "
                "(globs work, e.g. /load Billing*.csv)[/yellow]"
            )
            return
        expanded = _expand_paths(args)
        if not expanded:
            session.console.print(
                f"[red]No files matched: {' '.join(args)}[/red]"
            )
            return
        _cmd_load(session, expanded)
        return

    if cmd == "/list":
        if not session.spikes:
            session.console.print(
                "[yellow]No spikes loaded. Use /load FILE first.[/yellow]"
            )
            return
        _render_spike_table(session.console, session.spikes)
        return

    if cmd == "/spike":
        if not session.spikes:
            session.console.print(
                "[yellow]No spikes loaded. Use /load FILE first.[/yellow]"
            )
            return
        if not args:
            session.console.print("[yellow]Usage: /spike N[/yellow]")
            return
        try:
            n = int(args[0])
        except ValueError:
            session.console.print(f"[yellow]Not a number: {args[0]}[/yellow]")
            return
        if n < 0 or n >= len(session.spikes):
            session.console.print(
                f"[yellow]Spike index out of range (0-{len(session.spikes)-1})[/yellow]"
            )
            return
        _cmd_investigate(session, session.spikes[n])
        return

    if cmd == "/history":
        _render_history(session.console, session.history)
        return

    if cmd == "/recall":
        if not args:
            session.console.print(
                "[yellow]Usage: /recall <query>   — search memory palace[/yellow]"
            )
            return
        _cmd_recall(session, " ".join(args))
        return

    if cmd == "/remember":
        if not args:
            session.console.print(
                "[yellow]Usage: /remember <text>   — save a fact to the palace[/yellow]"
            )
            return
        _cmd_remember(session, " ".join(args))
        return

    if cmd == "/palace":
        _cmd_palace_status(session)
        return

    session.console.print(
        f"[yellow]Unknown command '{cmd}'. Type /help.[/yellow]"
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Memory palace commands
# ---------------------------------------------------------------------------
def _cmd_recall(session: ChatSession, query: str) -> None:
    """Search the palace and print results."""
    if not palace_is_available():
        _print_palace_unavailable(session)
        return
    palace = get_palace()
    hits = palace.recall(query, wing=session.wing or None, n=10)
    if not hits:
        session.console.print(
            f"[dim]no memories found for '{query}' in wing '{session.wing}'[/dim]"
        )
        return
    session.console.print(
        Panel(
            "\n".join(
                f"• {_format_hit(h)}" for h in hits
            ),
            title=f"[cyan]recall[/cyan]  '{query}'  —  wing: {session.wing}",
            border_style="cyan",
        )
    )


def _cmd_remember(session: ChatSession, text: str) -> None:
    """Save a curated fact to the palace."""
    if not palace_is_available():
        _print_palace_unavailable(session)
        return
    palace = get_palace()
    ok = palace.remember(
        content=text,
        wing=session.wing,
        hall="facts",
        source="chat /remember",
    )
    if ok:
        session.console.print(
            f"[green]✓[/green] remembered in wing [bold]{session.wing}[/bold]: {text}"
        )
    else:
        session.console.print(
            "[red]failed to save memory — run /palace to check status[/red]"
        )


def _cmd_palace_status(session: ChatSession) -> None:
    """Show palace availability and diagnostic info."""
    palace = get_palace()
    status = palace.status()
    lines = [
        f"  [bold]Available:[/bold]     {'yes' if status.available else 'no'}",
        f"  [bold]Storage:[/bold]       {status.storage_path}",
        f"  [bold]Current wing:[/bold]  {session.wing}",
    ]
    if status.tool_count:
        lines.append(f"  [bold]Tools:[/bold]         {status.tool_count}")
    if status.reason:
        lines.append(f"  [yellow]Note:[/yellow]          {status.reason}")
    if not status.available:
        lines.append("")
        lines.append("[dim]To enable memory, install mempalace and mcp:[/dim]")
        lines.append("  [bold].venv/bin/pip install mempalace mcp[/bold]")
    session.console.print(
        Panel(
            "\n".join(lines),
            title="Memory palace",
            border_style="cyan" if status.available else "yellow",
        )
    )


def _print_palace_unavailable(session: ChatSession) -> None:
    session.console.print(
        "[yellow]Memory palace is not available. "
        "Run /palace for details.[/yellow]"
    )


def _render_startup_recall(session: ChatSession) -> None:
    """After /load, peek at what the palace already knows about this wing.

    Runs three broad queries to surface the most useful facts up front:
    root causes, project mappings, and any recent user notes. Hits are
    deduped and capped so we don't flood the screen.
    """
    palace = get_palace()
    probes = [
        "root cause",
        "project",
        "recent investigation",
    ]
    seen: set[str] = set()
    hits: list[MemoryHit] = []
    for q in probes:
        for h in palace.recall(q, wing=session.wing, n=5):
            if h.content and h.content not in seen:
                seen.add(h.content)
                hits.append(h)

    if not hits:
        session.console.print(
            f"[dim]Memory palace wing: [bold]{session.wing}[/bold]  "
            "(empty — use /remember to save facts, /recall to search)[/dim]"
        )
        return

    lines = []
    for h in hits[:6]:
        # Show only the first line of multi-line memories so conclusions
        # don't blow up the panel
        first_line = h.content.split("\n", 1)[0][:120]
        tag = f"[dim]({h.hall or '?'})[/dim]"
        lines.append(f"• {first_line}  {tag}")

    session.console.print(
        Panel(
            "\n".join(lines),
            title=f"[cyan]Palace recall[/cyan]  wing: {session.wing}  "
                  f"({len(hits)} hits)",
            border_style="cyan",
            subtitle="[dim]use /recall <query> to search for more[/dim]",
        )
    )


def _format_hit(hit: MemoryHit) -> str:
    tag = ""
    if hit.hall or hit.room:
        tag = f" [dim]({hit.hall or '?'} · {hit.room or '?'})[/dim]"
    score = f" [dim]{hit.score:.2f}[/dim]" if hit.score is not None else ""
    return f"{hit.content}{tag}{score}"


def _expand_paths(args: list[str]) -> list[Path]:
    """Expand each arg through glob, falling back to a literal path.

    Shell globs don't expand inside our REPL because we read directly from
    prompt_toolkit, not via a shell. So /load Billing*.csv arrives as the
    literal string 'Billing*.csv'. We expand here.

    Order:
      1. Expand ~ and env vars
      2. If the arg contains glob characters (* ? [), run glob.glob
      3. Otherwise treat it as a literal path
    Duplicates are dropped, results sorted.
    """
    seen: dict[str, None] = {}
    for raw in args:
        expanded = str(Path(raw).expanduser())
        # Also expand environment variables
        import os
        expanded = os.path.expandvars(expanded)

        if any(ch in expanded for ch in "*?["):
            matches = glob_mod.glob(expanded)
            if not matches:
                continue
            for m in sorted(matches):
                seen.setdefault(m, None)
        else:
            seen.setdefault(expanded, None)
    return [Path(p) for p in seen.keys()]


def _cmd_load(session: ChatSession, files: list[Path]) -> None:
    session.console.print(f"[dim]Loading {len(files)} file(s)…[/dim]")
    try:
        spikes = load_spikes_from_files(files)
    except BillingFileError as exc:
        session.console.print(f"[red]{exc}[/red]")
        return
    if not spikes:
        session.console.print("[yellow]No spikes detected.[/yellow]")
        return
    session.spikes = spikes
    session.loaded_files = list(files)
    session.wing = default_wing_for_files(files)
    session.memory_enabled = palace_is_available()

    # Auto-sniff provider from files (import locally to avoid CLI dependency
    # cycles in import order). If all files signal AWS, switch the session;
    # otherwise leave provider as whatever the mode picker set.
    from ghosthunter.cli import _sniff_provider_from_file
    sniffed = {_sniff_provider_from_file(f) for f in files}
    sniffed.discard(None)
    if sniffed == {"aws"} and session.provider != "aws":
        session.provider = "aws"
        session.console.print("[dim]Detected AWS billing data — session set to AWS.[/dim]")
    elif sniffed == {"gcp"} and session.provider != "gcp":
        session.provider = "gcp"
        session.console.print("[dim]Detected GCP billing data — session set to GCP.[/dim]")

    session.console.print(
        f"[green]✓[/green] Loaded {len(spikes)} spikes from {len(files)} file(s)."
    )
    if session.memory_enabled:
        _render_startup_recall(session)
    _render_spike_table(session.console, spikes)
    session.console.print(
        "[dim]Pick one with [bold]/spike N[/bold] to start an investigation.[/dim]"
    )


def _build_billing_context(
    session: ChatSession, spike: CostSpike | None = None
) -> str:
    """Describe the data Opus has up front. Important: tells Opus what's
    knowable from the billing files vs. what it has to ask the user.

    If `spike` is provided AND the memory palace is available, prior
    knowledge about that specific spike is recalled and included.
    """
    if not session.loaded_files:
        return ""
    file_list = "\n".join(f"  - {p.name}" for p in session.loaded_files)
    groupings = sorted({getattr(s, "grouping", "service") for s in session.spikes})

    parts = [
        f"The user uploaded {len(session.loaded_files)} billing file(s):\n"
        f"{file_list}\n",
        f"From these files, Ghosthunter detected {len(session.spikes)} spikes "
        f"grouped by: {', '.join(groupings)}.\n",
        "IMPORTANT: separate Console-export files cannot be joined. If a row "
        "is grouped by 'project', you cannot determine which services that "
        "project's cost belongs to from the billing data alone. Same for "
        "service-grouped rows — you cannot determine which projects they "
        "live in. If you need that join, ASK THE USER (next_action.type="
        "need_info), or propose a single read-only command that pulls it.",
    ]

    # ---- Prior knowledge from the memory palace ----
    if spike is not None and session.memory_enabled:
        memory_block = _recall_memories_for_spike(session, spike)
        if memory_block:
            parts.append(memory_block)

    return "\n".join(parts)


def _recall_memories_for_spike(
    session: ChatSession, spike: CostSpike
) -> str | None:
    """Query the palace for prior knowledge about this spike, return
    a block suitable for appending to the initial Opus prompt."""
    palace = get_palace()
    queries = [
        f"{spike.service} root cause",
        f"{spike.service} project",
        f"{spike.service} in {session.wing}",
    ]
    seen: set[str] = set()
    hits: list[MemoryHit] = []
    for q in queries:
        for h in palace.recall(q, wing=session.wing, n=4):
            if h.content and h.content not in seen:
                seen.add(h.content)
                hits.append(h)
    if not hits:
        return None

    lines = [
        "\n## Prior knowledge from memory palace",
        f"Wing: {session.wing}. These are facts saved from previous "
        f"investigations or user corrections. Treat them as ground truth "
        f"unless the billing data contradicts them.",
    ]
    for h in hits[:8]:
        tag = ""
        if h.hall:
            tag = f" [{h.hall}]"
        lines.append(f"  - {h.content}{tag}")
    return "\n".join(lines)


def _save_conclusion_to_palace(
    session: ChatSession, spike: CostSpike, result
) -> None:
    """Auto-save the conclusion of a successful investigation."""
    if not session.memory_enabled:
        return
    if not result.succeeded or not result.conclusion:
        return
    palace = get_palace()
    c = result.conclusion
    root_cause = c.get("root_cause", "?")
    confidence = c.get("confidence", "?")
    evidence = c.get("evidence_summary") or []
    recs = c.get("recommendations") or []
    lines = [
        f"ROOT CAUSE: {root_cause}  ({confidence}% confidence)",
        f"Spike: [{getattr(spike, 'grouping', 'service')}] {spike.service}  "
        f"(${spike.current_cost:,.0f})",
    ]
    if evidence:
        lines.append("Evidence:")
        for e in evidence[:5]:
            lines.append(f"  - {e}")
    if recs:
        lines.append("Recommendations:")
        for r in recs[:5]:
            lines.append(f"  → {r}")
    content = "\n".join(lines)
    ok = palace.remember(
        content=content,
        wing=session.wing,
        room=spike.service,
        hall="conclusions",
        source="auto-save on conclude",
    )
    if ok:
        session.console.print(
            f"[dim]✓ conclusion saved to palace wing '{session.wing}'[/dim]"
        )


def _cmd_investigate(session: ChatSession, spike: CostSpike) -> None:
    """Run an investigation, with mid-flight /spike N support.

    If the user types /spike N during the investigation, AdvisorSpikeSwitch
    propagates out, we record what we have, and start a fresh investigation
    on the new spike. Loops until a normal completion / abort.
    """
    while True:
        label = f"[{getattr(spike, 'grouping', 'service')}] {spike.service}"
        session.console.print(
            Panel(
                f"[bold]Investigating[/bold] {label}\n"
                f"Current cost: ${spike.current_cost:,.2f}",
                border_style="bright_yellow",
            )
        )
        _render_top_contributors(session.console, spike)

        session.in_investigation = True
        session.current_hypotheses = []
        session.current_evidence = []

        investigator, provider = _build_investigator(session, spike)
        context = _build_billing_context(session, spike)

        try:
            result: InvestigationResult = asyncio.run(
                investigator.investigate(spike, additional_context=context)
            )
        except AdvisorSpikeSwitch as switch:
            session.in_investigation = False
            target = switch.target_index
            if target < 0 or target >= len(session.spikes):
                session.console.print(
                    f"[red]Spike index {target} out of range "
                    f"(0-{len(session.spikes)-1}). Staying here.[/red]"
                )
                # Resume the previous investigation? No — it's already
                # been torn down. Drop back to the chat prompt.
                return
            new_spike = session.spikes[target]
            new_label = f"[{getattr(new_spike, 'grouping', 'service')}] {new_spike.service}"
            session.console.print(
                f"[cyan]→ switching to spike {target}: {new_label}[/cyan]\n"
            )
            spike = new_spike
            continue
        except Exception as exc:  # noqa: BLE001 — show and recover
            session.console.print(f"[red]Investigation crashed: {exc}[/red]")
            session.in_investigation = False
            return

        session.in_investigation = False
        _render_result(session.console, result)
        _record_history(session, label, result)
        _save_conclusion_to_palace(session, spike, result)
        return


def _make_memory_hook(session: ChatSession, spike: CostSpike):
    """Build a memory_hook closure for the Investigator.

    Fires on need_info answers and /note messages. Each becomes a palace
    memory under the current wing, room=service, hall per kind.
    """
    if not session.memory_enabled:
        return None

    palace = get_palace()

    def _hook(kind: str, text: str) -> None:
        hall = "facts" if kind == "need_info_answer" else "user_notes"
        source = f"auto-save: {kind}"
        try:
            ok = palace.remember(
                content=text,
                wing=session.wing,
                room=spike.service,
                hall=hall,
                source=source,
            )
            if ok:
                session.console.print(
                    f"[dim]✓ saved to palace ({hall})[/dim]"
                )
        except Exception:
            pass

    return _hook


def _build_investigator(
    session: ChatSession,
    spike: CostSpike | None = None,
) -> tuple[Investigator, AdvisorProvider]:
    """Build an Investigator wired to print to the chat console.

    The advisor provider gets a callback that lets the user inspect
    hypothesis state mid-investigation via /hypotheses.
    """
    provider_key = getattr(session, "provider", "gcp") or "gcp"
    validator = SecurityValidator(provider=provider_key)

    def show_hypotheses() -> None:
        if not session.current_hypotheses:
            session.console.print("[dim](no hypotheses yet)[/dim]")
            return
        _render_hypotheses(session.console, session.current_hypotheses)

    def show_spike_list() -> None:
        if not session.spikes:
            session.console.print("[dim](no spikes loaded)[/dim]")
            return
        _render_spike_table(session.console, session.spikes)

    provider = AdvisorProvider(
        validator=validator,
        console=session.console,
        on_show_hypotheses=show_hypotheses,
        on_list_spikes=show_spike_list,
        provider_key=provider_key,
    )

    async def auto_approve(_: PendingCommand) -> str:
        return "approve"

    async def event_hook(event: InvestigationEvent) -> None:
        await _on_event(session, event)

    memory_hook = _make_memory_hook(session, spike) if spike is not None else None

    investigator = Investigator(
        provider=provider,  # type: ignore[arg-type]
        reasoner=Reasoner(provider=provider_key),
        executor=Executor(provider=provider_key),
        validator=validator,
        approval_hook=auto_approve,
        event_hook=event_hook,
        budget=Budget(),
        memory_hook=memory_hook,
    )
    return investigator, provider


# ---------------------------------------------------------------------------
# Event rendering
# ---------------------------------------------------------------------------
async def _on_event(session: ChatSession, event: InvestigationEvent) -> None:
    kind = event.kind
    payload = event.payload
    c = session.console

    if kind == "step_started":
        c.print("\n[dim]thinking…[/dim]")

    elif kind == "hypotheses_updated":
        # Cache for /hypotheses, also render
        hyps = [_dict_to_hypothesis(h) for h in payload["hypotheses"]]
        session.current_hypotheses = hyps
        _render_hypotheses(c, hyps)

    elif kind == "reasoning":
        # Opus's voice — show as a panel so it stands out
        c.print(
            Panel(
                payload["text"],
                title="[bold]Opus[/bold]",
                border_style="magenta",
                expand=False,
            )
        )

    elif kind == "opus_asks":
        # The advisor provider's ask_user prints its own panel; this event
        # is just for any UI that wants to log "Opus asked X".
        pass

    elif kind == "command_blocked":
        from ghosthunter.ui import render_command_blocked
        render_command_blocked(
            c,
            command=payload.get("command"),
            layer=payload.get("layer", "?"),
            reason=payload.get("reason", "(no reason given)"),
        )

    elif kind == "command_proposed":
        # The AdvisorProvider's execute_command will print the command
        # panel itself; nothing to do here.
        pass

    elif kind == "command_executed":
        result = payload["result"]
        c.print(
            f"[dim]received {len(result.stdout):,} chars in "
            f"{result.duration_seconds:.1f}s[/dim]"
        )

    elif kind == "evidence_added":
        e: Evidence = payload["evidence"]
        session.current_evidence.append(e)
        c.print(
            Panel(
                e.summary,
                title=f"[green]Evidence {e.id}[/green]",
                border_style="green",
                expand=False,
            )
        )

    elif kind == "user_note":
        c.print(
            f"[cyan]→ note sent to Opus:[/cyan] {payload['note']}"
        )

    elif kind == "command_rejected_by_user":
        c.print("[yellow]→ command skipped[/yellow]")

    elif kind == "concluded":
        c.print("\n[bold green]✓ Investigation concluded[/bold green]")

    elif kind == "aborted":
        c.print(f"\n[bold red]✗ Aborted:[/bold red] {payload['reason']}")


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------
def _print_welcome(console: Console) -> None:
    console.print(
        Panel(
            (
                "[bold]Ghosthunter[/bold] — interactive cost-spike investigator\n\n"
                "Advisor mode: Ghosthunter never touches your cloud. You paste\n"
                "billing files, it proposes read-only commands, you run them in\n"
                "your own terminal and paste the output back.\n\n"
                "Quick start:\n"
                "  [cyan]/load report.csv[/cyan]      parse one or more billing files\n"
                "  [cyan]/spike 0[/cyan]              investigate the largest spike\n"
                "  [cyan]/help[/cyan]                 see all commands\n"
                "  [cyan]/exit[/cyan]                 leave the session"
            ),
            title="welcome",
            border_style="cyan",
        )
    )


def _print_help(console: Console, in_investigation: bool) -> None:
    if in_investigation:
        console.print(
            Panel(
                (
                    "[bold]Inside an investigation[/bold] — chat naturally\n\n"
                    "[bold]Keyboard:[/bold]\n"
                    "  [cyan]Enter[/cyan]            send  ·  "
                    "[cyan]Esc then Enter[/cyan] = newline  ·  "
                    "[cyan]Paste[/cyan] auto multi-line\n"
                    "  [cyan]↑ / ↓[/cyan]           history  ·  "
                    "[cyan]Ctrl+R[/cyan] search  ·  "
                    "[cyan]Ctrl+C[/cyan] cancel input\n\n"
                    "[bold]What you can type:[/bold]\n"
                    "  [cyan]<question>[/cyan]       Opus answers next turn\n"
                    "  [cyan]<pasted output>[/cyan]  compressed and added as evidence\n"
                    "  [cyan]<file path>[/cyan]      read file as command output\n\n"
                    "[bold]Slash commands:[/bold]\n"
                    "  [cyan]/note <text>[/cyan]     explicit form of a note\n"
                    "  [cyan]/hypotheses[/cyan]      show current hypothesis state\n"
                    "  [cyan]/list[/cyan]            reshow the spike table\n"
                    "  [cyan]/spike N[/cyan]         abandon and investigate spike N instead\n"
                    "  [cyan]/skip[/cyan]            skip this command\n"
                    "  [cyan]/paste[/cyan]           legacy paste mode (ends with ###)\n"
                    "  [cyan]/quit[/cyan]            end investigation, back to spike picker\n"
                    "  [cyan]/help[/cyan]            this help"
                ),
                title="help",
                border_style="cyan",
            )
        )
        return

    console.print(
        Panel(
            (
                "[bold]Chat commands[/bold]\n\n"
                "  [cyan]/load FILE [FILE ...][/cyan]   parse billing CSV/JSON files\n"
                "  [cyan]/list[/cyan]                   reshow detected spikes\n"
                "  [cyan]/spike N[/cyan]                investigate spike N\n"
                "  [cyan]/history[/cyan]                what you've investigated this session\n\n"
                "[bold]Memory palace[/bold] [dim](optional — needs mempalace + mcp)[/dim]\n"
                "  [cyan]/recall <query>[/cyan]         search past facts and conclusions\n"
                "  [cyan]/remember <text>[/cyan]        save a fact to the current wing\n"
                "  [cyan]/palace[/cyan]                 show palace status & install hint\n\n"
                "  [cyan]/help[/cyan]                   this help\n"
                "  [cyan]/exit[/cyan]                   leave the session\n\n"
                "[dim]During an investigation you'll get an extra set of commands\n"
                "for output collection — type /help inside one to see them.[/dim]"
            ),
            title="help",
            border_style="cyan",
        )
    )


def _render_spike_table(console: Console, spikes: list[CostSpike]) -> None:
    table = Table(title=f"Detected cost spikes ({len(spikes)})")
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


def _render_top_contributors(console: Console, spike: CostSpike) -> None:
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
                display = desc if len(desc) <= 96 else desc[:93] + "…"
                console.print(f"    [dim italic]{display}[/dim italic]")


def _render_hypotheses(console: Console, hypotheses: list[Hypothesis]) -> None:
    if not hypotheses:
        return
    console.print()
    for h in hypotheses:
        confidence_color = (
            "bright_green" if h.confidence >= 85
            else "green" if h.confidence >= 60
            else "yellow" if h.confidence >= 30
            else "red"
        )
        bar_full = h.confidence // 5
        bar = "█" * bar_full + "░" * (20 - bar_full)
        console.print(
            f"  [bold cyan]{h.id}[/bold cyan] "
            f"[{confidence_color}]{bar}[/{confidence_color}] "
            f"[{confidence_color}]{h.confidence:3}%[/{confidence_color}] "
            f"[dim]{h.status:>10}[/dim]  {h.description}"
        )


def _render_result(console: Console, result: InvestigationResult) -> None:
    if result.succeeded and result.conclusion:
        c = result.conclusion
        body_parts = [
            f"[bold]Root cause:[/bold] [bright_green]"
            f"{c.get('root_cause', '?')}[/bright_green]",
            f"[bold]Confidence:[/bold] {c.get('confidence', '?')}%",
        ]
        if c.get("evidence_summary"):
            body_parts.append("\n[bold]Evidence:[/bold]")
            for e in c["evidence_summary"]:
                body_parts.append(f"  • {e}")
        if c.get("recommendations"):
            body_parts.append("\n[bold]Recommendations:[/bold]")
            for r in c["recommendations"]:
                body_parts.append(f"  → {r}")
        if c.get("not_verified"):
            body_parts.append("\n[bold yellow]Not verified:[/bold yellow]")
            for n in c["not_verified"]:
                body_parts.append(f"  ? {n}")
        console.print(
            Panel(
                "\n".join(body_parts),
                title="conclusion",
                border_style="bright_green",
            )
        )
    elif result.aborted_reason:
        console.print(
            Panel(
                result.aborted_reason,
                title="aborted",
                border_style="red",
            )
        )

    console.print(
        f"[dim]Commands used: {result.budget.commands_used}/"
        f"{result.budget.max_commands}  •  "
        f"Time: {result.budget.seconds_used:.0f}s/"
        f"{result.budget.max_seconds:.0f}s[/dim]\n"
    )


def _render_history(
    console: Console, history: list[SessionHistoryEntry]
) -> None:
    if not history:
        console.print("[dim]No investigations yet this session.[/dim]")
        return
    table = Table(title=f"This session ({len(history)} investigations)")
    table.add_column("Time", style="cyan")
    table.add_column("Spike")
    table.add_column("Result")
    table.add_column("Cmds", justify="right")
    table.add_column("Outcome")
    for h in history:
        table.add_row(
            h.timestamp.strftime("%H:%M:%S"),
            h.spike_label,
            "[green]concluded[/green]" if h.succeeded else "[red]aborted[/red]",
            str(h.commands_used),
            h.summary[:60],
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _record_history(
    session: ChatSession, label: str, result: InvestigationResult
) -> None:
    summary = (
        result.conclusion.get("root_cause", "—")
        if result.succeeded and result.conclusion
        else (result.aborted_reason or "—")
    )
    session.history.append(
        SessionHistoryEntry(
            timestamp=datetime.now(),
            spike_label=label,
            succeeded=result.succeeded,
            summary=summary,
            commands_used=result.budget.commands_used,
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
