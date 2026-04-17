"""Regression tests for the v1.0.4 paste-safety fixes.

Two distinct bugs caught running advisor mode against the a real customer
Cloud DNS bill:

1. ``bq query`` SQL with backticks around fully-qualified table
   references (the ONLY way BigQuery accepts ``project.dataset.table``
   refs in Standard SQL) was rejected by Layer 1 because the blocklist
   flagged backticks as shell command-substitution regardless of quote
   context. Opus worked around it by producing backtick-less SQL that
   BigQuery then refused to parse.

2. The "Run this command in your own terminal" Rich Panel used Unicode
   box-drawing characters (``│`` ``─`` ``╭``). When a long command
   wrapped across multiple lines inside the panel and the user
   triple-clicked or click-dragged to copy it, the ``│`` border chars
   came with. Pasting into a shell produced ``unrecognized arguments:
   │`` and ``zsh: command not found: │``; pasting into ``bq query``
   produced ``Illegal input character "\\342"`` (``\\342`` = first byte
   of ``│`` in UTF-8).
"""
from __future__ import annotations

import re

import pytest

from ghosthunter.security.blocklist import (
    find_fast_reject,
    has_unquoted_command_substitution,
)
from ghosthunter.security.validator import SecurityValidator


# ---------------------------------------------------------------------------
# Fix 2: quote-aware command substitution
# ---------------------------------------------------------------------------
class TestQuoteAwareSubstitution:
    """``has_unquoted_command_substitution`` must match bash semantics:
    safe ONLY inside single quotes (where bash does no expansion)."""

    def test_backtick_outside_quotes_blocked(self):
        assert has_unquoted_command_substitution("echo `whoami`") == "`"

    def test_backtick_inside_single_quotes_allowed(self):
        """The whole reason for this fix — bq query SQL with table refs."""
        cmd = "bq query 'SELECT * FROM `proj.dataset.table`'"
        assert has_unquoted_command_substitution(cmd) is None

    def test_backtick_inside_double_quotes_still_blocked(self):
        """Bash expands backticks inside double quotes, so they stay dangerous."""
        assert has_unquoted_command_substitution('echo "`whoami`"') == "`"

    def test_dollar_paren_outside_quotes_blocked(self):
        assert has_unquoted_command_substitution("echo $(whoami)") == "$("

    def test_dollar_paren_in_single_quotes_allowed(self):
        assert has_unquoted_command_substitution("echo '$(whoami)'") is None

    def test_dollar_paren_in_double_quotes_still_blocked(self):
        assert has_unquoted_command_substitution('echo "$(whoami)"') == "$("

    def test_dollar_brace_outside_quotes_blocked(self):
        assert has_unquoted_command_substitution("echo ${USER}") == "${"

    def test_dollar_brace_in_single_quotes_allowed(self):
        assert has_unquoted_command_substitution("echo '${USER}'") is None

    def test_escaped_backtick_allowed(self):
        r"""Backslash-escaped backticks (``\`foo\```) are literal in bash."""
        assert has_unquoted_command_substitution(r"echo \`safe\`") is None

    def test_bare_dollar_allowed(self):
        """A lone ``$`` without paren/brace isn't command substitution."""
        assert has_unquoted_command_substitution("echo $foo") is None

    def test_clean_command_unchanged(self):
        cmd = "gcloud dns managed-zones list --project=foo --format=json"
        assert has_unquoted_command_substitution(cmd) is None

    def test_nested_quotes_tracked_correctly(self):
        """Single quote inside double quotes doesn't toggle single-quote state."""
        # Here the single quote is inside double quotes, so we're never
        # really in single-quote mode, so the backtick is unquoted.
        cmd = 'echo "it\'s `whoami`"'
        assert has_unquoted_command_substitution(cmd) == "`"


# ---------------------------------------------------------------------------
# find_fast_reject no longer trips on sql backticks
# ---------------------------------------------------------------------------
class TestFastRejectNoLongerFlagsBackticks:
    @pytest.mark.parametrize("cmd", [
        "bq query 'SELECT 1 FROM `p.d.t`'",
        "bq query --use_legacy_sql=false 'SELECT * FROM `a.b.c`'",
        "echo `x`",  # unquoted — still dangerous, but caught later, not here
    ])
    def test_backticks_pass_fast_reject(self, cmd: str):
        """Backticks were removed from ``FAST_REJECT_PATTERNS`` in v1.0.4 —
        the quote-aware helper catches the unquoted cases instead."""
        hit = find_fast_reject(cmd)
        assert hit is None or "`" not in hit, (
            f"find_fast_reject still flags backticks: {hit!r} in {cmd!r}"
        )


# ---------------------------------------------------------------------------
# End-to-end through SecurityValidator — the real caller
# ---------------------------------------------------------------------------
class TestValidatorEndToEnd:
    def setup_method(self):
        self.v = SecurityValidator("gcp")

    def test_real_acme_bq_query_now_accepted(self):
        """This is the exact query Opus produced on the customer investigation
        that previously got rejected by Layer 1 for containing a backtick."""
        cmd = (
            "bq query --use_legacy_sql=false --project_id=prj-acme-dns "
            "'SELECT DATE(usage_start_time) AS d, SUM(cost) AS c "
            "FROM `prj-billing.billing_export.gcp_billing_export_v1_X` "
            "WHERE service.description = \"Cloud DNS\" "
            "GROUP BY d ORDER BY d'"
        )
        result = self.v.is_allowed(cmd)
        assert result.allowed, (
            f"bq query with backticks still blocked at {result.layer}: "
            f"{result.reason}"
        )

    def test_backtick_command_substitution_still_blocked(self):
        """Ensure the fix didn't widen the security gate."""
        result = self.v.is_allowed("gcloud `echo config` list")
        assert not result.allowed
        assert result.layer == "L1"

    def test_dollar_paren_command_substitution_still_blocked(self):
        result = self.v.is_allowed("gcloud $(echo config) list")
        assert not result.allowed
        assert result.layer == "L1"

    def test_chaining_still_blocked(self):
        result = self.v.is_allowed("gcloud config list; rm -rf /")
        assert not result.allowed
        assert result.layer == "L1"

    def test_rm_still_blocked(self):
        result = self.v.is_allowed("rm -rf /")
        assert not result.allowed
        assert result.layer == "L1"


# ---------------------------------------------------------------------------
# Fix 1: command panel renders pure ASCII
# ---------------------------------------------------------------------------
# Box-drawing Unicode codepoints commonly used by Rich's default panel
# box. If any of these end up in the printed command panel, copy-paste
# WILL destroy whatever command sits inside.
_BOX_DRAWING_CHARS = "│─╭╮╰╯┃━┏┓┗┛┌┐└┘"

# "Smart quotes" that can leak in if a renderer applies prose
# typography. None of these are legal in a shell command.
_SMART_QUOTES = "\u2018\u2019\u201c\u201d"


class TestCommandPanelIsPlainAscii:
    """If copy-paste picks up a ``│`` from the panel, the user's shell
    breaks. v1.0.4 replaces the Panel with a plain print so there are
    no borders to pick up in the first place."""

    def _render_panel(self, command: str) -> str:
        from rich.console import Console
        import io

        from ghosthunter.providers.advisor import AdvisorProvider

        buf = io.StringIO()
        console = Console(
            file=buf,
            force_terminal=False,  # no ANSI color codes in the string
            width=120,
            legacy_windows=False,
        )
        # _print_command_panel only needs self.console — build a minimal
        # shim rather than wiring up the whole advisor provider.
        shim = AdvisorProvider.__new__(AdvisorProvider)
        shim.console = console
        AdvisorProvider._print_command_panel(shim, command)
        return buf.getvalue()

    def test_no_box_drawing_characters_around_command(self):
        cmd = "gcloud dns managed-zones list --project=prj-acme-dns"
        out = self._render_panel(cmd)
        # The command itself must appear unmangled.
        assert cmd in out, f"command not found verbatim in panel output:\n{out!r}"
        # And no Unicode borders anywhere.
        offenders = [c for c in _BOX_DRAWING_CHARS if c in out]
        assert not offenders, (
            f"panel rendered these Unicode box-drawing characters: {offenders!r}. "
            f"They will end up in clipboard when users select the command."
        )

    def test_no_smart_quotes_in_panel_output(self):
        cmd = "gcloud logging read 'resource.type=\"dns_managed_zone\"' --limit=20"
        out = self._render_panel(cmd)
        offenders = [c for c in _SMART_QUOTES if c in out]
        assert not offenders, (
            f"panel leaked smart quotes: {offenders!r}. "
            "The user's shell will reject them with an encoding error."
        )

    def test_long_command_not_broken_by_wrapping(self):
        """A long command renders as one logical line from the command's
        perspective — it can terminal-soft-wrap visually, but the text
        the user selects and pastes must be the exact original command,
        with no border chars or continuation markers injected.
        """
        long_cmd = (
            "bq query --use_legacy_sql=false --format=prettyjson "
            "--project_id=prj-acme-dns "
            "'SELECT DATE(usage_start_time) AS d, SUM(cost) AS c "
            "FROM `prj-billing.billing_export.gcp_billing_export_v1_X` "
            "WHERE service.description = \"Cloud DNS\" "
            "GROUP BY d ORDER BY d'"
        )
        out = self._render_panel(long_cmd)
        assert long_cmd in out, (
            "long command was broken or mangled in rendering. "
            "Expected to find the whole string verbatim.\n"
            f"Got:\n{out!r}"
        )
        offenders = [c for c in _BOX_DRAWING_CHARS if c in out]
        assert not offenders, (
            f"long command rendering introduced border chars: {offenders!r}"
        )

    def test_no_dangerous_unicode_anywhere_in_panel(self):
        """Defence in depth: the output must not contain ANY of the
        specific Unicode characters that cause shell / BigQuery /
        general paste failures. We allow decorative header chars (``─``
        ``·``) because they sit on distinct lines from the command and
        aren't clipboard hazards when users select the command line."""
        cmd = "gcloud config list --format=json"
        out = self._render_panel(cmd)
        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        clean = ansi_re.sub("", out)

        # The known-dangerous set — box-drawing borders + smart quotes.
        dangerous = set(_BOX_DRAWING_CHARS + _SMART_QUOTES)
        offenders = sorted({ch for ch in clean if ch in dangerous})
        assert not offenders, (
            f"panel contains clipboard-hostile chars: "
            f"{[f'U+{ord(c):04X}' for c in offenders]}"
        )
