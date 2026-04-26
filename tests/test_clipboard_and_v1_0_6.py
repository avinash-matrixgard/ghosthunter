"""Regression tests for v1.0.6 clipboard integration + blocked-command
display improvements.

v1.0.5 shipped a paste-safe command block, but the user reported they
still had to triple-click / drag to copy long commands because
terminals don't have markdown-style "copy buttons". v1.0.6 closes the
gap with two paths:

  1. OSC 52 auto-copy — silently pushes the proposed command onto the
     user's clipboard if their terminal honours the OSC 52 escape
     sequence (iTerm2, Kitty, WezTerm, tmux with set-clipboard on).
  2. ``/copy`` slash command — OS-native fallback (pbcopy / wl-copy /
     xclip / clip.exe) plus OSC 52 if nothing else works.

Also upgraded the blocked-command renderer to show WHAT got blocked
and WHY, not just the layer code.
"""

from __future__ import annotations

import base64
import io
import re
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from ghosthunter.clipboard import (
    _ENV_OPT_OUT,
    copy_to_clipboard,
    write_osc52,
)
from ghosthunter.ui import render_command_blocked


# ---------------------------------------------------------------------------
# OSC 52 emitter
# ---------------------------------------------------------------------------
class _FakeTTY(io.StringIO):
    """StringIO that lies about being a tty so OSC 52 will emit."""

    def isatty(self) -> bool:
        return True


class TestWriteOsc52:
    def test_emits_to_tty_stream(self):
        s = _FakeTTY()
        ok = write_osc52("hello world", stream=s)
        assert ok is True
        output = s.getvalue()
        # The escape sequence format: \x1b]52;c;<base64>\x07
        match = re.search(r"\x1b\]52;c;([A-Za-z0-9+/=]+)\x07", output)
        assert match, f"OSC 52 sequence not found in: {output!r}"
        decoded = base64.b64decode(match.group(1)).decode("utf-8")
        assert decoded == "hello world"

    def test_skips_non_tty_stream(self):
        """Redirected stdout (e.g. `ghosthunter ... > log.txt`) must
        not get OSC 52 garbage written into it."""
        plain_buf = io.StringIO()  # not a tty
        ok = write_osc52("x", stream=plain_buf)
        assert ok is False
        assert plain_buf.getvalue() == ""

    def test_respects_env_opt_out(self, monkeypatch):
        monkeypatch.setenv(_ENV_OPT_OUT, "1")
        s = _FakeTTY()
        ok = write_osc52("anything", stream=s)
        assert ok is False
        assert s.getvalue() == ""

    @pytest.mark.parametrize("value", ["true", "TRUE", "yes", "on", "1"])
    def test_env_opt_out_accepts_common_truthy(self, monkeypatch, value):
        monkeypatch.setenv(_ENV_OPT_OUT, value)
        s = _FakeTTY()
        assert write_osc52("x", stream=s) is False

    def test_empty_text_refused(self):
        s = _FakeTTY()
        assert write_osc52("", stream=s) is False

    def test_oversize_payload_refused(self):
        # 10 KB source → >_OSC52_MAX_BYTES base64 → must refuse rather
        # than splatter a giant escape sequence that some terminals
        # simply echo literally.
        huge = "x" * 10_000
        s = _FakeTTY()
        assert write_osc52(huge, stream=s) is False

    def test_unicode_command_roundtrips(self):
        """BQ SQL with non-ASCII chars — the base64 encoding must
        preserve them on the way through."""
        s = _FakeTTY()
        cmd = "bq query 'SELECT description FROM `p.d.t` WHERE x = \"café\"'"
        assert write_osc52(cmd, stream=s) is True
        match = re.search(r"\x1b\]52;c;([A-Za-z0-9+/=]+)\x07", s.getvalue())
        assert match
        assert base64.b64decode(match.group(1)).decode("utf-8") == cmd


# ---------------------------------------------------------------------------
# copy_to_clipboard — OS-native + OSC 52 fallback
# ---------------------------------------------------------------------------
class TestCopyToClipboard:
    def test_respects_env_opt_out(self, monkeypatch):
        monkeypatch.setenv(_ENV_OPT_OUT, "1")
        ok, mech = copy_to_clipboard("x")
        assert ok is False
        assert mech == "skipped"

    def test_empty_text_unavailable(self, monkeypatch):
        monkeypatch.delenv(_ENV_OPT_OUT, raising=False)
        ok, mech = copy_to_clipboard("")
        assert ok is False
        assert mech == "unavailable"

    def test_native_success_short_circuits(self, monkeypatch):
        """When pbcopy/xclip succeeds (returncode 0) we stop there —
        no need to also emit OSC 52."""
        monkeypatch.delenv(_ENV_OPT_OUT, raising=False)
        fake_proc = MagicMock(returncode=0, stdout="", stderr="")
        with patch("ghosthunter.clipboard.subprocess.run", return_value=fake_proc) as mock_run:
            ok, mech = copy_to_clipboard("hello")
        assert ok is True
        assert mech in {"pbcopy", "wl-copy", "xclip", "xsel", "clip"}
        mock_run.assert_called_once()
        # First positional arg is the argv; the input kwarg is the
        # text we want copied.
        kwargs = mock_run.call_args.kwargs
        assert kwargs["input"] == "hello"

    def test_native_missing_falls_back_to_osc52(self, monkeypatch):
        """If the OS tool isn't installed (FileNotFoundError), we
        try OSC 52 instead."""
        monkeypatch.delenv(_ENV_OPT_OUT, raising=False)
        fake_stream = _FakeTTY()

        with patch(
            "ghosthunter.clipboard.subprocess.run",
            side_effect=FileNotFoundError("pbcopy not found"),
        ):
            ok, mech = copy_to_clipboard("test cmd", stream=fake_stream)
        assert ok is True
        assert mech == "osc52"
        assert "\x1b]52;c;" in fake_stream.getvalue()

    def test_all_paths_fail_returns_unavailable(self, monkeypatch):
        monkeypatch.delenv(_ENV_OPT_OUT, raising=False)
        plain_buf = io.StringIO()  # not a tty → OSC 52 refused
        with patch(
            "ghosthunter.clipboard.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            ok, mech = copy_to_clipboard("x", stream=plain_buf)
        assert ok is False
        assert mech == "unavailable"


# ---------------------------------------------------------------------------
# /copy slash command integration
# ---------------------------------------------------------------------------
class TestCopySlashCommand:
    def _make_advisor(self) -> tuple:
        """Build an AdvisorProvider wired to a string-buffer console."""
        from ghosthunter.providers.advisor import AdvisorProvider

        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, width=120)
        advisor = AdvisorProvider(console=console)
        return advisor, buf

    def test_copy_with_no_prior_command_tells_user(self):
        advisor, buf = self._make_advisor()
        advisor._handle_slash("/copy")
        assert "Nothing to copy" in buf.getvalue()

    def test_copy_success_prints_confirmation(self, monkeypatch):
        """After Opus proposed a command and /copy was typed, we
        print ✓ copied and the mechanism name."""
        monkeypatch.delenv(_ENV_OPT_OUT, raising=False)
        advisor, buf = self._make_advisor()
        advisor._last_proposed_command = "gcloud config list"

        fake_proc = MagicMock(returncode=0)
        with patch(
            "ghosthunter.clipboard.subprocess.run",
            return_value=fake_proc,
        ):
            advisor._handle_slash("/copy")

        assert "copied to clipboard" in buf.getvalue()

    def test_copy_env_opt_out_explains_why(self, monkeypatch):
        monkeypatch.setenv(_ENV_OPT_OUT, "1")
        advisor, buf = self._make_advisor()
        advisor._last_proposed_command = "gcloud config list"
        advisor._handle_slash("/copy")
        assert "GHOSTHUNTER_NO_CLIPBOARD" in buf.getvalue()

    def test_print_command_panel_tracks_last_command(self, monkeypatch):
        """The OSC 52 + /copy feature needs the provider to record
        each command Opus proposes. Regression guard: if a later
        refactor drops the assignment, both features silently break."""
        monkeypatch.setenv(_ENV_OPT_OUT, "1")  # suppress OSC 52 noise
        advisor, _buf = self._make_advisor()
        advisor._print_command_panel("gcloud dns managed-zones list")
        assert advisor._last_proposed_command == "gcloud dns managed-zones list"

        advisor._print_command_panel("bq query 'SELECT 1'")
        assert advisor._last_proposed_command == "bq query 'SELECT 1'"


# ---------------------------------------------------------------------------
# Blocked-command renderer
# ---------------------------------------------------------------------------
class TestBlockedCommandRender:
    def _render(self, *, command: str | None, layer: str, reason: str) -> str:
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, width=120)
        render_command_blocked(console, command=command, layer=layer, reason=reason)
        return buf.getvalue()

    def test_shows_the_blocked_command(self):
        out = self._render(
            command="gcloud monitoring time-series list --project=foo",
            layer="L2",
            reason="command not in allowlist",
        )
        assert "gcloud monitoring time-series list --project=foo" in out

    def test_shows_the_blocking_reason(self):
        out = self._render(
            command="echo `whoami`",
            layer="L1",
            reason="unquoted shell substitution: `",
        )
        assert "L1" in out
        assert "unquoted shell substitution" in out

    def test_shows_layer_explanation(self):
        """L2 users shouldn't need to read the source to learn what
        L2 means."""
        out = self._render(command="gcloud foo", layer="L2", reason="not in allowlist")
        assert "allowlist" in out.lower()

    def test_handles_missing_command_gracefully(self):
        """Some code paths emit command_blocked without a command
        field (legacy / malformed payload). We should still render
        something usable rather than crash."""
        out = self._render(command=None, layer="L5", reason="budget hit")
        assert "L5" in out
        assert "budget hit" in out

    def test_long_command_prints_verbatim(self):
        """The blocked command display uses the same paste-safe
        rendering as proposed commands — no newlines injected."""
        long_cmd = (
            "bq query --use_legacy_sql=false --format=prettyjson "
            "--project_id=test "
            "'SELECT DATE(usage_start_time) AS d, SUM(cost) AS c "
            'FROM `p.d.t` WHERE s = "X" GROUP BY d ORDER BY d\''
        )
        out = self._render(command=long_cmd, layer="L1", reason="test")
        assert long_cmd in out, (
            "long blocked command was mangled in rendering — copy-paste would break"
        )


# ---------------------------------------------------------------------------
# Authenticity guard for clipboard module (same bar as rest of tool)
# ---------------------------------------------------------------------------
def test_clipboard_module_has_no_scenario_specific_content():
    """No pattern-shortcut strings accumulate in the clipboard module."""
    import inspect

    import ghosthunter.clipboard as clip_mod

    source = inspect.getsource(clip_mod).lower()
    forbidden = [
        "bigquery",
        "example",
        "nat gateway",
        "acme",
        "scenario",
    ]
    offenders = [f for f in forbidden if f in source]
    assert not offenders, f"clipboard module contains scenario-specific strings: {offenders}"
