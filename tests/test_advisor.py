"""Tests for ``providers/advisor.AdvisorProvider``.

AdvisorProvider is the user-facing shim that replaces direct shell
execution: it prints the proposed command, waits for the user to paste
the output (or invoke a slash command), and wraps the result in a
``CommandResult`` the investigator can consume.

We don't touch prompt_toolkit in tests. Instead we monkeypatch
``AdvisorProvider._prompt`` (the single seam into user input) to
return scripted strings. All Rich console output is directed to an
``io.StringIO`` so the test output stays clean.

Coverage:

- ``execute_command`` path
    * Pre-validates and raises ``CommandRejectedError`` for blocked
      commands, without ever prompting.
    * Returns a ``CommandResult`` with the pasted output when the user
      pastes a multi-line blob.
    * Reads a file path when the user types one; returns the file's
      contents.
    * Propagates AdvisorSkipped / AdvisorAborted / AdvisorNote /
      AdvisorSpikeSwitch raised from inside ``_collect_output``.
- ``_looks_like_command_output`` heuristic (8 cases).
- ``_handle_slash`` dispatch:
    * ``/quit``  → AdvisorAborted
    * ``/skip``  → AdvisorSkipped
    * ``/note X`` → AdvisorNote("X")
    * ``/note`` without arg → usage hint, no exception
    * ``/spike 3`` → AdvisorSpikeSwitch(3)
    * ``/spike abc`` → error hint, no exception
    * ``/hypotheses`` / ``/list`` → call the configured hook
    * ``/help`` → prints panel, no exception
    * unknown slash → prints hint, no exception
- ``ask_user`` flow:
    * Plain text returns the answer.
    * ``/skip`` returns the "decline" sentinel.
    * Other slash commands propagate their exceptions.
- Output truncation at ``max_output_bytes``.
- Legacy ``/paste`` block terminated by ``###``.
"""

from __future__ import annotations

import asyncio
import io
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from ghosthunter.providers.advisor import (
    EOF_MARKER,
    AdvisorAborted,
    AdvisorNote,
    AdvisorProvider,
    AdvisorSkipped,
    AdvisorSpikeSwitch,
)
from ghosthunter.providers.base import (
    CommandRejectedError,
    CommandResult,
)
from ghosthunter.security.validator import SecurityValidator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _silent_console() -> Console:
    """Console that writes to an in-memory buffer instead of the terminal."""
    return Console(
        file=io.StringIO(),
        force_terminal=False,
        width=120,
        record=True,
    )


def _script_prompt(monkeypatch, lines: list[str]) -> list[str]:
    """Replace ``AdvisorProvider._prompt`` with a scripted sequence.

    Each call pops one line from ``lines``. Returns the same list so
    the test can observe how many prompts happened (via len()).
    """
    it = iter(lines)

    def _fake_prompt(prefix: str = "") -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError("script exhausted")

    monkeypatch.setattr(AdvisorProvider, "_prompt", staticmethod(_fake_prompt))
    return lines


def _make_advisor(**kwargs) -> AdvisorProvider:
    kwargs.setdefault("validator", SecurityValidator(provider="aws"))
    kwargs.setdefault("console", _silent_console())
    return AdvisorProvider(**kwargs)


# ---------------------------------------------------------------------------
# execute_command: pre-validation
# ---------------------------------------------------------------------------
class TestExecuteCommandPreValidation:
    def test_blocks_before_prompting(self, monkeypatch):
        """A command that fails Layer 1-4 must NOT prompt the user."""
        advisor = _make_advisor()
        # Scripted prompt — if we're blocked correctly, it's never called.
        _script_prompt(monkeypatch, [])

        with pytest.raises(CommandRejectedError):
            asyncio.run(
                advisor.execute_command("rm -rf /")  # Layer 1 fast-reject
            )

    def test_blocked_reason_mentions_layer(self, monkeypatch):
        advisor = _make_advisor()
        _script_prompt(monkeypatch, [])
        try:
            asyncio.run(advisor.execute_command("curl http://evil"))
        except CommandRejectedError as exc:
            assert "L1" in str(exc)
        else:
            pytest.fail("expected CommandRejectedError")


# ---------------------------------------------------------------------------
# execute_command: happy paths (paste + file)
# ---------------------------------------------------------------------------
class TestExecuteCommandHappyPaths:
    def test_returns_pasted_multiline_output(self, monkeypatch):
        advisor = _make_advisor()
        pasted = "line 1\nline 2\nline 3"
        _script_prompt(monkeypatch, [pasted])

        result = asyncio.run(advisor.execute_command("aws ec2 describe-instances"))
        assert isinstance(result, CommandResult)
        assert result.stdout == pasted
        assert result.exit_code == 0
        assert result.command == "aws ec2 describe-instances"
        assert result.truncated is False

    def test_returns_json_single_line(self, monkeypatch):
        """A single-line JSON blob is recognised as output, not prose."""
        advisor = _make_advisor()
        body = '{"a": 1, "b": 2}'
        _script_prompt(monkeypatch, [body])
        result = asyncio.run(advisor.execute_command("aws sts get-caller-identity"))
        assert result.stdout == body

    def test_reads_file_path(self, tmp_path, monkeypatch):
        advisor = _make_advisor()
        payload = tmp_path / "out.json"
        payload.write_text('{"count": 42}\n')
        _script_prompt(monkeypatch, [str(payload)])

        result = asyncio.run(advisor.execute_command("aws ec2 describe-instances"))
        assert "count" in result.stdout
        assert "42" in result.stdout

    def test_truncates_oversized_output(self, monkeypatch):
        advisor = _make_advisor(max_output_bytes=100)
        pasted = "X" * 200 + "\nY" * 200  # 400-ish bytes, multiline → treated as output
        _script_prompt(monkeypatch, [pasted])
        result = asyncio.run(advisor.execute_command("aws ec2 describe-instances"))
        assert result.truncated is True
        assert len(result.stdout) <= 100


# ---------------------------------------------------------------------------
# execute_command: propagating slash-command exceptions
# ---------------------------------------------------------------------------
class TestExecuteCommandSlashExceptions:
    def test_quit_propagates_advisor_aborted(self, monkeypatch):
        advisor = _make_advisor()
        _script_prompt(monkeypatch, ["/quit"])
        with pytest.raises(AdvisorAborted):
            asyncio.run(advisor.execute_command("aws ec2 describe-instances"))

    def test_skip_propagates_advisor_skipped(self, monkeypatch):
        advisor = _make_advisor()
        _script_prompt(monkeypatch, ["/skip"])
        with pytest.raises(AdvisorSkipped):
            asyncio.run(advisor.execute_command("aws ec2 describe-instances"))

    def test_note_with_text_propagates_advisor_note(self, monkeypatch):
        advisor = _make_advisor()
        _script_prompt(monkeypatch, ["/note try a different angle"])
        with pytest.raises(AdvisorNote) as excinfo:
            asyncio.run(advisor.execute_command("aws ec2 describe-instances"))
        assert excinfo.value.note == "try a different angle"

    def test_spike_switch_propagates(self, monkeypatch):
        advisor = _make_advisor()
        _script_prompt(monkeypatch, ["/spike 3"])
        with pytest.raises(AdvisorSpikeSwitch) as excinfo:
            asyncio.run(advisor.execute_command("aws ec2 describe-instances"))
        assert excinfo.value.target_index == 3

    def test_short_prose_becomes_advisor_note(self, monkeypatch):
        """Short free-text input is classified as a note, not output."""
        advisor = _make_advisor()
        _script_prompt(monkeypatch, ["what's the region for this workload?"])
        with pytest.raises(AdvisorNote) as excinfo:
            asyncio.run(advisor.execute_command("aws ec2 describe-instances"))
        assert "region" in excinfo.value.note


# ---------------------------------------------------------------------------
# _looks_like_command_output heuristic
# ---------------------------------------------------------------------------
class TestLooksLikeCommandOutput:
    @pytest.mark.parametrize(
        "text,expected",
        [
            # Positives
            ("line1\nline2", True),  # multi-line
            ("x" * 301, True),  # long
            ('{ "key": "val" }', True),  # JSON object
            ("[1, 2, 3]", True),  # JSON array
            ("a|b|c|d|e", True),  # 4 pipes
            ("foo\tbar\tbaz", True),  # 2 tabs
            # Negatives
            ("short prose", False),
            ("what's the region?", False),
            ("it's broken", False),
            ("a|b", False),  # only 1 pipe
            ("", False),
        ],
    )
    def test_heuristic(self, text, expected):
        assert AdvisorProvider._looks_like_command_output(text) is expected


# ---------------------------------------------------------------------------
# _handle_slash dispatch
# ---------------------------------------------------------------------------
class TestHandleSlash:
    def test_quit_raises_aborted(self):
        advisor = _make_advisor()
        with pytest.raises(AdvisorAborted):
            advisor._handle_slash("/quit")

    def test_skip_raises_skipped(self):
        advisor = _make_advisor()
        with pytest.raises(AdvisorSkipped):
            advisor._handle_slash("/skip")

    def test_note_with_arg_raises_note(self):
        advisor = _make_advisor()
        with pytest.raises(AdvisorNote) as excinfo:
            advisor._handle_slash("/note this is a note")
        assert excinfo.value.note == "this is a note"

    def test_note_without_arg_is_passive(self):
        """Missing /note argument should print usage, not raise."""
        advisor = _make_advisor()
        advisor._handle_slash("/note")  # does not raise
        output = advisor.console.export_text()
        assert "Usage" in output or "usage" in output.lower()

    def test_spike_with_int(self):
        advisor = _make_advisor()
        with pytest.raises(AdvisorSpikeSwitch) as excinfo:
            advisor._handle_slash("/spike 7")
        assert excinfo.value.target_index == 7

    def test_spike_without_int_is_passive(self):
        advisor = _make_advisor()
        advisor._handle_slash("/spike notanumber")  # does not raise
        out = advisor.console.export_text().lower()
        assert "number" in out or "not a number" in out

    def test_spike_without_arg_is_passive(self):
        advisor = _make_advisor()
        advisor._handle_slash("/spike")  # does not raise
        assert "Usage" in advisor.console.export_text()

    def test_hypotheses_calls_hook(self):
        hook = MagicMock()
        advisor = _make_advisor(on_show_hypotheses=hook)
        advisor._handle_slash("/hypotheses")
        hook.assert_called_once_with()

    def test_hypotheses_no_hook_is_passive(self):
        advisor = _make_advisor()
        advisor._handle_slash("/hypotheses")  # does not raise

    def test_list_calls_hook(self):
        hook = MagicMock()
        advisor = _make_advisor(on_list_spikes=hook)
        advisor._handle_slash("/list")
        hook.assert_called_once_with()

    def test_help_is_passive(self):
        advisor = _make_advisor()
        advisor._handle_slash("/help")  # does not raise
        assert advisor.console.export_text()  # printed something

    def test_unknown_slash_is_passive(self):
        advisor = _make_advisor()
        advisor._handle_slash("/nonsense")  # does not raise
        assert "Unknown" in advisor.console.export_text()


# ---------------------------------------------------------------------------
# ask_user flow
# ---------------------------------------------------------------------------
class TestAskUser:
    def test_plain_text_returned(self, monkeypatch):
        advisor = _make_advisor()
        _script_prompt(monkeypatch, ["my-project-id"])
        answer = asyncio.run(advisor.ask_user("Which project?"))
        assert answer == "my-project-id"

    def test_skip_returns_decline_sentinel(self, monkeypatch):
        advisor = _make_advisor()
        _script_prompt(monkeypatch, ["/skip"])
        answer = asyncio.run(advisor.ask_user("Which project?"))
        assert "declined" in answer.lower()

    def test_quit_still_propagates(self, monkeypatch):
        advisor = _make_advisor()
        _script_prompt(monkeypatch, ["/quit"])
        with pytest.raises(AdvisorAborted):
            asyncio.run(advisor.ask_user("Which project?"))

    def test_note_propagates(self, monkeypatch):
        advisor = _make_advisor()
        _script_prompt(monkeypatch, ["/note try asking differently"])
        with pytest.raises(AdvisorNote):
            asyncio.run(advisor.ask_user("Which project?"))

    def test_empty_line_then_answer(self, monkeypatch):
        """Empty input should re-prompt rather than return an empty string."""
        advisor = _make_advisor()
        _script_prompt(monkeypatch, ["", "   ", "my-answer"])
        answer = asyncio.run(advisor.ask_user("Q"))
        assert answer == "my-answer"

    def test_help_re_prompts(self, monkeypatch):
        """Passive slash commands like /help should loop back to prompt,
        not return."""
        advisor = _make_advisor()
        _script_prompt(monkeypatch, ["/help", "my-answer"])
        answer = asyncio.run(advisor.ask_user("Q"))
        assert answer == "my-answer"


# ---------------------------------------------------------------------------
# Legacy /paste block
# ---------------------------------------------------------------------------
class TestLegacyPasteBlock:
    def test_paste_block_terminated_by_marker(self, monkeypatch):
        advisor = _make_advisor()
        _script_prompt(
            monkeypatch,
            [
                "/paste",
                "line a",
                "line b",
                "line c",
                EOF_MARKER,
            ],
        )
        result = asyncio.run(advisor.execute_command("aws ec2 describe-instances"))
        assert "line a" in result.stdout
        assert "line b" in result.stdout
        assert "line c" in result.stdout
        # The EOF marker itself should NOT be in the captured stdout.
        assert EOF_MARKER not in result.stdout


# ---------------------------------------------------------------------------
# EOF / KeyboardInterrupt handling
# ---------------------------------------------------------------------------
class TestPromptInterrupts:
    def test_eof_raises_advisor_aborted(self, monkeypatch):
        """EOF on stdin (Ctrl+D) ends the investigation cleanly."""
        advisor = _make_advisor()
        # Empty scripted list → _prompt raises EOFError immediately.
        _script_prompt(monkeypatch, [])
        with pytest.raises(AdvisorAborted):
            asyncio.run(advisor.execute_command("aws ec2 describe-instances"))
