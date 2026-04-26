"""Pytest smoke of the Ghosthunter detection benchmark.

Runs the harness in-process on every pytest invocation so that a parser
regression that drops a scenario below the pass threshold fails CI, not
only ``python benchmarks/run_benchmark.py`` runs by hand.

The benchmark itself is parser-only (no network, ~200 ms) so the cost
of this test is negligible.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = REPO_ROOT / "benchmarks"


@pytest.fixture(scope="module")
def benchmark_runner():
    # benchmarks/ isn't a package — add it to sys.path just for this test.
    if str(BENCH_DIR) not in sys.path:
        sys.path.insert(0, str(BENCH_DIR))
    import run_benchmark  # type: ignore[import-not-found]

    return run_benchmark


def test_all_scenarios_pass(benchmark_runner):
    """Every bundled scenario must score >= PASS_THRESHOLD (80).

    If a scenario regresses, we print a per-scenario breakdown so it's
    obvious from the CI log which one broke without having to rerun by
    hand.
    """
    ids = benchmark_runner._discover_scenario_ids()
    assert ids, "No benchmark scenarios discovered — generate_fixtures.py may not have been run"

    results = [benchmark_runner._run_one(sid) for sid in ids]
    failed = [r for r in results if not r.passed]

    if failed:
        lines = ["Failed scenarios:"]
        for r in failed:
            lines.append(f"  • {r.id}: score={r.score}, detected={r.detected_service!r}")
            for reason in r.reasons:
                lines.append(f"      {reason}")
        pytest.fail("\n".join(lines))


def test_benchmark_mean_score_is_100(benchmark_runner):
    """Detection quality should be perfect on synthetic fixtures.

    If this drops without an intentional scenario change, the parser
    lost precision somewhere — investigate before merging.
    """
    ids = benchmark_runner._discover_scenario_ids()
    results = [benchmark_runner._run_one(sid) for sid in ids]
    mean = sum(r.score for r in results) / max(len(results), 1)
    assert mean == pytest.approx(100.0), (
        f"Mean benchmark score dropped to {mean:.1f}/100 — a scenario is losing points"
    )


def test_benchmark_covers_both_providers(benchmark_runner):
    """Regression guard: we want both GCP and AWS represented."""
    ids = benchmark_runner._discover_scenario_ids()
    assert any(i.startswith("gcp_") for i in ids), "No GCP scenarios"
    assert any(i.startswith("aws_") for i in ids), "No AWS scenarios"
