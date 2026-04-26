"""Enforce the CLAUDE.md size budget — LOCAL DEV ONLY.

CLAUDE.md is now `.gitignore`-d and lives only on contributors'
machines (it holds personal context, not a public spec). This test
exists so Avinash's local workflow continues to benefit from the
500-line cap; it **skips gracefully** when the file isn't present
(fresh public clone, CI, etc.).

Rule: when CLAUDE.md exists, it must stay ≤ 500 lines. A big
CLAUDE.md reloads on every Claude turn and eats daily-budget tokens.
Overflow lands in `docs/internal/*.md` (also gitignored; use
`@docs/internal/<name>.md` to pull one on-demand).
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

MAX_LINES = 500
WARN_AT = int(MAX_LINES * 0.8)  # 400

CLAUDE_MD = Path(__file__).resolve().parent.parent / "CLAUDE.md"

# Skip the whole module if CLAUDE.md isn't there — it's not part of
# the public repo anymore. Contributors who DO have it locally still
# get the enforcement.
pytestmark = pytest.mark.skipif(
    not CLAUDE_MD.exists(),
    reason="CLAUDE.md not present (gitignored dev notes); size cap only enforced locally.",
)


def _line_count(path: Path) -> int:
    with path.open("rb") as f:
        return sum(1 for _ in f)


class TestClaudeMdSize:
    def test_claude_md_under_hard_cap(self):
        n = _line_count(CLAUDE_MD)
        assert n <= MAX_LINES, (
            f"CLAUDE.md is {n} lines; hard cap is {MAX_LINES}. "
            "Move a section into docs/internal/<topic>.md — see the "
            "module docstring of this file for guidance."
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
