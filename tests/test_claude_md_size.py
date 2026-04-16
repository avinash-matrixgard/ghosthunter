"""Enforce the CLAUDE.md size budget.

Rule: **CLAUDE.md MUST NEVER EXCEED 500 LINES.**

Why: it's reloaded on every Claude turn. A big CLAUDE.md eats tokens,
which costs both money and daily budget. After hitting ~30% of the
daily limit per turn with the original 1351-line file, we set a hard
ceiling of 500 lines and split overflow into `docs/internal/*.md`.

If this test fails, you have two options:
  1. Trim CLAUDE.md — move the least-essential section to a new file
     under `docs/internal/<topic>.md` and add a one-line pointer under
     the "Docs" section.
  2. If the 500 cap is genuinely wrong for your project (e.g. the
     project grew and needs more per-turn context), raise the constant
     deliberately with a commit message that documents why, and
     commit the bigger file in the same change. Do NOT bump the
     constant silently to make the test pass.

A warning fires at 80% (400 lines) so there's time to trim before
the cap is hit.
"""
from __future__ import annotations

import warnings
from pathlib import Path

import pytest


MAX_LINES = 500
WARN_AT = int(MAX_LINES * 0.8)  # 400

CLAUDE_MD = Path(__file__).resolve().parent.parent / "CLAUDE.md"


def _line_count(path: Path) -> int:
    with path.open("rb") as f:
        return sum(1 for _ in f)


class TestClaudeMdSize:
    def test_claude_md_exists(self):
        assert CLAUDE_MD.exists(), f"{CLAUDE_MD} must exist"

    def test_claude_md_under_hard_cap(self):
        n = _line_count(CLAUDE_MD)
        assert n <= MAX_LINES, (
            f"CLAUDE.md is {n} lines; hard cap is {MAX_LINES}. "
            "Move a section into docs/internal/<topic>.md and reference "
            "it under the Docs section of CLAUDE.md. See the module "
            "docstring of this file for guidance."
        )

    def test_claude_md_not_near_cap(self):
        """Soft warning so overruns don't surprise us at commit time."""
        n = _line_count(CLAUDE_MD)
        if n > WARN_AT:
            warnings.warn(
                UserWarning(
                    f"CLAUDE.md is {n} lines, above the {WARN_AT}-line "
                    f"soft ceiling ({int(100 * n / MAX_LINES)}% of the "
                    f"{MAX_LINES} hard cap). Consider splitting a section "
                    "into docs/internal/ before the next addition."
                )
            )


class TestDocsInternalExists:
    """Keep the overflow-target directory as a stable contract."""

    def test_docs_internal_dir_exists(self):
        docs = CLAUDE_MD.parent / "docs" / "internal"
        assert docs.is_dir(), (
            f"{docs} must exist — it's where overflow from CLAUDE.md "
            "lands per the 500-line rule."
        )

    def test_claude_md_references_docs_internal(self):
        """Rule is only useful if CLAUDE.md itself advertises the escape
        hatch. Catch accidental removal of the Docs section."""
        body = CLAUDE_MD.read_text()
        assert "docs/internal" in body, (
            "CLAUDE.md must mention docs/internal/ somewhere — it's "
            "the documented place for overflow. If you rename the "
            "directory, update this test and the HARD RULE banner."
        )
