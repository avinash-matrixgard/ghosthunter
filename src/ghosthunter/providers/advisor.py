"""Advisor-mode 'provider'.

In advisor mode Ghosthunter never touches GCP. Instead, when the
investigator wants to run a command, this provider:

1. Re-validates the command against the security layers (defense in depth)
2. Prints the command to the user in a copyable panel
3. Waits for the user to either:
     - paste a path to a file containing the output
     - paste the output directly until they enter the EOF marker
     - skip the command (Opus will be told)
     - quit the investigation
4. Wraps whatever the user provided in a `CommandResult` and returns it

The class is duck-type compatible with `GCPProvider`: same `execute_command`
signature, same return type. The investigator does not need to know which
mode it's in.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from ghosthunter.chat_io import read_line
from ghosthunter.providers.gcp import (
    CommandRejectedError,
    CommandResult,
    GCPProviderError,
)
from ghosthunter.security.validator import SecurityValidator

# Legacy multi-line paste terminator, still accepted for users who type it
# out of habit. Bracketed paste (prompt_toolkit) handles multi-line paste
# natively now, so this is a fallback.
EOF_MARKER = "###"
MAX_OUTPUT_BYTES = 5_000_000  # 5 MB cap on user-provided output

# Slash commands the user can type instead of pasting output
SLASH_HELP = "/help"
SLASH_SKIP = "/skip"
SLASH_QUIT = "/quit"
SLASH_NOTE = "/note"
SLASH_HYPOTHESES = "/hypotheses"
SLASH_PASTE = "/paste"
SLASH_SPIKE = "/spike"
SLASH_LIST = "/list"
SLASH_COPY = "/copy"


class AdvisorAborted(GCPProviderError):
    """Raised when the user types /quit to end the current investigation."""


class AdvisorSkipped(GCPProviderError):
    """Raised when the user skips a specific command via /skip."""


class AdvisorNote(GCPProviderError):
    """Raised when the user types /note <text> or free-form text.

    The investigator catches this, injects the text as a user message
    in the Opus conversation, and proceeds to the next reasoning turn.
    The current command is treated as skipped.
    """

    def __init__(self, note: str) -> None:
        super().__init__("user provided a note instead of command output")
        self.note = note


class AdvisorSpikeSwitch(GCPProviderError):
    """Raised when the user types /spike N mid-investigation.

    Propagates all the way out of the investigator so the chat session
    can abandon the current investigation and start a new one on the
    target spike. NOT caught by the investigator's exception handlers.
    """

    def __init__(self, target_index: int) -> None:
        super().__init__(f"user wants to switch to spike {target_index}")
        self.target_index = target_index


class AdvisorProvider:
    """Read-only 'execution' that delegates to the human at the keyboard."""

    def __init__(
        self,
        validator: SecurityValidator | None = None,
        console: Console | None = None,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
        on_show_hypotheses: callable | None = None,
        on_list_spikes: callable | None = None,
        provider_key: str = "gcp",
    ) -> None:
        # When the caller passes an explicit validator we trust its provider;
        # otherwise we scope the default validator to this advisor's provider.
        self.validator = validator or SecurityValidator(provider=provider_key)
        self.provider_key = provider_key
        self.console = console or Console()
        self.max_output_bytes = max_output_bytes
        # Optional hooks wired by the chat session so the user can inspect
        # state and switch spikes mid-investigation.
        self.on_show_hypotheses = on_show_hypotheses
        self.on_list_spikes = on_list_spikes
        # Most-recent command Opus proposed in this session. Tracked so
        # `/copy` has something to put on the clipboard even if the user
        # scrolled past the panel. Cleared on ``/quit``.
        self._last_proposed_command: str | None = None

    # ------------------------------------------------------------------
    # Conversational API (used when Opus emits next_action.type=need_info)
    # ------------------------------------------------------------------
    async def ask_user(self, question: str) -> str:
        """Show Opus's question in a panel and read the user's free-text answer.

        All slash commands work here (/spike, /list, /quit, /skip,
        /hypotheses, /help). Free text is returned as Opus's answer.
        """
        self.console.print()
        self.console.print(
            Panel(
                question,
                title="[bold cyan]Opus is asking you[/bold cyan]",
                border_style="cyan",
                expand=False,
            )
        )
        self.console.print(
            "[dim]Type your answer, or use a slash command "
            "(/spike N, /list, /skip, /quit, /help).[/dim]"
        )

        loop = asyncio.get_event_loop()

        while True:
            line = await loop.run_in_executor(None, self._prompt_oneline)
            stripped = line.strip()

            if not stripped:
                continue

            # Slash commands → dispatch through the shared handler.
            # Most raise typed exceptions which propagate up; passive
            # ones (help, list, hypotheses) return and we re-prompt.
            if stripped.startswith("/"):
                # /skip in this context means "decline to answer" — return
                # a sentinel string instead of raising AdvisorSkipped.
                if stripped.lower() == SLASH_SKIP:
                    return "(user declined to answer — proceed with what you have)"
                self._handle_slash(stripped)
                continue

            # Plain text → Opus's answer
            return line

    def _prompt_oneline(self) -> str:
        try:
            return self._prompt("> ")
        except (EOFError, KeyboardInterrupt):
            return ""

    @staticmethod
    def _prompt(prefix: str) -> str:
        """Read one buffer from the shared prompt_toolkit session.

        The buffer may span multiple lines (from bracketed paste or
        Shift+Enter). Returns the raw string.
        """
        return read_line(prefix)

    # ------------------------------------------------------------------
    # Investigator-facing API (matches GCPProvider.execute_command)
    # ------------------------------------------------------------------
    async def execute_command(self, command: str) -> CommandResult:
        # Defense in depth: re-validate even though the investigator
        # already did.
        check = self.validator.is_allowed(command)
        if not check.allowed:
            raise CommandRejectedError(
                f"command rejected at advisor execution ({check.layer}): {check.reason}"
            )

        self._print_command_panel(command)

        # Run the blocking input loop off the asyncio thread so we don't
        # freeze the event loop while the user is typing. _collect_output
        # raises AdvisorAborted / AdvisorSkipped / AdvisorNote /
        # AdvisorSpikeSwitch directly when the user types a slash command.
        loop = asyncio.get_event_loop()
        start = loop.time()
        stdout = await loop.run_in_executor(None, self._collect_output)
        duration = loop.time() - start
        truncated = False
        if len(stdout.encode("utf-8", errors="replace")) > self.max_output_bytes:
            stdout = stdout[: self.max_output_bytes]
            truncated = True

        return CommandResult(
            command=command,
            stdout=stdout,
            stderr="",
            exit_code=0,
            duration_seconds=duration,
            truncated=truncated,
        )

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------
    def _print_command_panel(self, command: str) -> None:
        """Show the proposed command for copy-paste.

        We deliberately do NOT use a Rich ``Panel`` here. Panels use
        Unicode box-drawing characters (``│`` ``─`` ``╭``) as borders,
        and when a long command wraps inside the panel those characters
        end up in the user's clipboard if they triple-click or
        click-drag to select. That has bitten us for real: a bq query
        broke with ``Illegal input character "\342"`` because the user
        pasted ``│`` straight into BigQuery.

        The command now prints as a standalone line with plain ASCII
        separators, so copying it picks up the command and nothing else.
        Terminal soft-wrap handles long lines without inserting any
        visible markers.
        """
        # Remember this command so ``/copy`` can re-push it to the
        # clipboard later if OSC 52 didn't land.
        self._last_proposed_command = command

        self.console.print()
        # Use Rich markup + print so the command itself is printed
        # verbatim (markup=False on the command line — no accidental
        # interpretation of [bold] if it happens to be in a SQL filter).
        self.console.print(
            "[bold yellow]-- Run this command in your own terminal --"
            "[/bold yellow]"
        )
        # ``markup=False`` is load-bearing: a real command could contain
        # ``[`` / ``]`` (e.g. inside a jq filter or JSON payload) and we
        # don't want Rich trying to parse those as style tags.
        # ``highlight=False`` disables Rich's auto-highlighter, which
        # otherwise recolors numbers/URLs and can inject reset codes.
        # ``soft_wrap=True`` is the copy-paste bit: with hard wrapping,
        # Rich inserts a newline at the terminal width and the user's
        # clipboard gets a broken command; with soft wrap, Rich emits
        # the string as-is and lets the terminal handle visual wrap so
        # triple-click / click-drag selects the original contiguous
        # command.
        self.console.print(
            command, markup=False, highlight=False, soft_wrap=True
        )
        self.console.print(
            "[dim]read-only · validated by 4 security layers[/dim]"
        )

        # OSC 52 auto-copy: silently push the command onto the user's
        # clipboard if their terminal supports it (iTerm2, Kitty,
        # WezTerm, tmux with set-clipboard on, …). Gated by
        # GHOSTHUNTER_NO_CLIPBOARD for users who don't want this.
        # We write directly to the console's underlying file rather
        # than via ``console.print`` because the OSC 52 sequence must
        # not be rewrapped, styled, or logged.
        osc52_attempted = False
        try:
            from ghosthunter.clipboard import write_osc52
            osc52_attempted = write_osc52(
                command, stream=getattr(self.console, "file", None)
            )
        except Exception:
            osc52_attempted = False

        # Prompt hint. Includes ``/copy`` so users whose terminal
        # didn't honour OSC 52 still have a documented one-keystroke
        # way to put the command on their clipboard.
        if osc52_attempted:
            hint_prefix = (
                "[dim italic]command placed on your clipboard (OSC 52). "
            )
        else:
            hint_prefix = "[dim italic]"
        self.console.print(
            hint_prefix
            + f"type [bold]{SLASH_COPY}[/bold] to copy this command."
            + "[/dim italic]"
        )
        self.console.print(
            "[dim]Paste the output (multi-line paste works), drop a "
            "[bold]file path[/bold], or [bold]ask a question[/bold]. "
            "[bold]Enter[/bold] sends · "
            "[bold]Esc then Enter[/bold] = newline · "
            f"[bold]{SLASH_HELP}[/bold] for all commands.[/dim]"
        )

    def _collect_output(self) -> str:
        """Collect command output / note / slash command from the user.

        A single call to `_prompt` (which wraps prompt_toolkit) returns
        the entire message, possibly multi-line from bracketed paste or
        Shift+Enter. We classify the result and dispatch accordingly:

          starts with '/' (single line)   → slash command
          single-line path that exists    → read file as output
          looks like command output       → return as output
          short single-line prose         → AdvisorNote (to Opus)
        """
        while True:
            try:
                raw = self._prompt("> ")
            except EOFError:
                raise AdvisorAborted("EOF on stdin")
            except KeyboardInterrupt:
                self.console.print("[dim](use /quit to end the investigation)[/dim]")
                continue

            if not raw.strip():
                continue

            is_multiline = "\n" in raw
            first_line = raw.split("\n", 1)[0].strip()

            # ---- Legacy /paste → dedicated paste loop with ### ----
            if not is_multiline and first_line.lower() == SLASH_PASTE:
                self.console.print(
                    f"[dim]paste output below; end with [bold]{EOF_MARKER}[/bold] "
                    "on its own line[/dim]"
                )
                return self._read_paste_block_legacy()

            # ---- File path branch (single line pointing at an existing file) ----
            # Check file-path FIRST so absolute paths like "/tmp/foo.json"
            # don't get misrouted to the slash-command handler just because
            # they start with "/".
            if not is_multiline:
                candidate = Path(first_line).expanduser()
                if candidate.exists() and candidate.is_file():
                    try:
                        return candidate.read_text(errors="replace")
                    except OSError as exc:
                        self.console.print(
                            f"[red]Could not read {candidate}: {exc}[/red]"
                        )
                        continue

            # ---- Slash commands (only on single-line input) ----
            if not is_multiline and first_line.startswith("/"):
                self._handle_slash(first_line)
                continue

            # ---- Classify: command output vs. short prose / question ----
            if self._looks_like_command_output(raw):
                return raw

            # Short single-line prose → note to Opus
            raise AdvisorNote(raw)

    @staticmethod
    def _looks_like_command_output(text: str) -> bool:
        """Heuristic: is this pasted command output, or prose?

        Command output typically:
          - Spans multiple lines (from paste), OR
          - Is long (> 300 chars), OR
          - Is a JSON / array literal, OR
          - Has table/log structure (pipes, tabs)

        Short single-line prose is treated as a note/question to Opus.
        """
        if "\n" in text:
            return True
        if len(text) > 300:
            return True
        stripped = text.strip()
        if stripped.startswith(("{", "[")) and stripped.endswith(("}", "]")):
            return True
        if text.count("|") >= 3 or text.count("\t") >= 2:
            return True
        return False

    def _read_paste_block_legacy(self) -> str:
        """Legacy fallback: read lines until a line containing only ###.

        Only reached when the user explicitly types /paste. The normal
        path now relies on bracketed paste handled by prompt_toolkit.
        """
        lines: list[str] = []
        while True:
            try:
                line = self._prompt("")
            except (EOFError, KeyboardInterrupt):
                break
            if line.strip() == EOF_MARKER:
                break
            lines.append(line)
        return "\n".join(lines)

    def _handle_slash(self, line: str) -> None:
        """Dispatch a slash command typed at any in-investigation prompt.

        Raises one of: AdvisorSkipped, AdvisorAborted, AdvisorNote,
        AdvisorSpikeSwitch. For passive commands (/help, /hypotheses,
        /list) it prints and returns.
        """
        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == SLASH_HELP:
            self._print_help_during_investigation()
            return

        if cmd == SLASH_SKIP:
            raise AdvisorSkipped("user typed /skip")

        if cmd == SLASH_QUIT:
            raise AdvisorAborted("user typed /quit")

        if cmd == SLASH_NOTE:
            if not arg:
                self.console.print(
                    "[yellow]Usage: /note <text to send to Opus>[/yellow]"
                )
                return
            raise AdvisorNote(arg)

        if cmd == SLASH_HYPOTHESES:
            if self.on_show_hypotheses is not None:
                self.on_show_hypotheses()
            else:
                self.console.print(
                    "[dim](hypothesis snapshot not wired up)[/dim]"
                )
            return

        if cmd == SLASH_SPIKE:
            if not arg:
                self.console.print("[yellow]Usage: /spike N[/yellow]")
                return
            try:
                n = int(arg.split()[0])
            except ValueError:
                self.console.print(
                    f"[yellow]Not a number: {arg}[/yellow]"
                )
                return
            raise AdvisorSpikeSwitch(n)

        if cmd == SLASH_LIST:
            if self.on_list_spikes is not None:
                self.on_list_spikes()
            else:
                self.console.print(
                    "[dim](spike list not wired up)[/dim]"
                )
            return

        if cmd == SLASH_COPY:
            # Puts the most-recent proposed command on the user's
            # clipboard via the best available mechanism: OS-native
            # tool (pbcopy / wl-copy / xclip / clip.exe) then OSC 52
            # as a fallback for SSH / tmux / unusual shells.
            if not self._last_proposed_command:
                self.console.print(
                    "[yellow]Nothing to copy yet — Opus hasn't proposed "
                    "a command this turn.[/yellow]"
                )
                return
            from ghosthunter.clipboard import copy_to_clipboard
            ok, mech = copy_to_clipboard(
                self._last_proposed_command,
                stream=getattr(self.console, "file", None),
            )
            if ok:
                self.console.print(
                    f"[green]✓ copied to clipboard[/green] "
                    f"[dim]({mech})[/dim]"
                )
            elif mech == "skipped":
                self.console.print(
                    "[yellow]Clipboard disabled via "
                    "GHOSTHUNTER_NO_CLIPBOARD — unset to re-enable."
                    "[/yellow]"
                )
            else:
                self.console.print(
                    "[yellow]Couldn't reach your clipboard. "
                    "Install pbcopy (macOS), xclip / wl-copy (Linux), "
                    "or use a terminal with OSC 52 support (iTerm2, "
                    "Kitty, WezTerm, tmux).[/yellow]"
                )
            return

        # Unknown slash command
        self.console.print(
            f"[yellow]Unknown command '{cmd}'. Type /help for options.[/yellow]"
        )

    def _print_help_during_investigation(self) -> None:
        self.console.print(
            Panel(
                (
                    "[bold]Inside an investigation[/bold] — chat naturally\n\n"
                    "[bold]Keyboard:[/bold]\n"
                    "  [cyan]Enter[/cyan]                   send your message\n"
                    "  [cyan]Esc then Enter[/cyan]          insert a newline (compose multi-line)\n"
                    "  [cyan]Ctrl+J[/cyan]                  insert a newline (alternative)\n"
                    "  [cyan]Paste[/cyan]                   multi-line paste captured automatically\n"
                    "  [cyan]↑ / ↓[/cyan]                   navigate input history\n"
                    "  [cyan]Ctrl+R[/cyan]                  reverse history search\n"
                    "  [cyan]Ctrl+C[/cyan]                  cancel input (use /quit to end investigation)\n\n"
                    "[bold]What you can type:[/bold]\n"
                    "  [cyan]<question / comment>[/cyan]    Opus answers next turn\n"
                    "  [cyan]<paste of command output>[/cyan]  Ghosthunter compresses it as evidence\n"
                    "  [cyan]<file path>[/cyan]             read a file as command output\n\n"
                    "[bold]Slash commands:[/bold]\n"
                    f"  [cyan]{SLASH_NOTE} <text>[/cyan]              explicit form of a note\n"
                    f"  [cyan]{SLASH_HYPOTHESES}[/cyan]             show current hypothesis state\n"
                    f"  [cyan]{SLASH_LIST}[/cyan]                   reshow the spike table\n"
                    f"  [cyan]{SLASH_SPIKE} N[/cyan]               abandon this and investigate spike N instead\n"
                    f"  [cyan]{SLASH_COPY}[/cyan]                   put the last proposed command on your clipboard\n"
                    f"  [cyan]{SLASH_SKIP}[/cyan]                   skip this command, let Opus try a different angle\n"
                    f"  [cyan]{SLASH_PASTE}[/cyan]                  legacy paste mode (ends with ###)\n"
                    f"  [cyan]{SLASH_QUIT}[/cyan]                   end investigation, back to spike picker\n"
                    f"  [cyan]{SLASH_HELP}[/cyan]                   show this help"
                ),
                title="Help",
                border_style="cyan",
            )
        )

