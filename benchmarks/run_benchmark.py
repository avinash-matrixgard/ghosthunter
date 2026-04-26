"""Run the Ghosthunter detection benchmark.

Layer 1 — pure parser + ranker validation, NO Anthropic API calls. For
each scenario in benchmarks/spikes/, load the CSV via the same code path
the CLI uses (``load_spikes_from_file``) and score the top spike against
the ground-truth JSON.

Scoring (0-100 per scenario):
    +50  top-ranked spike's service name matches expected (exact or
         substring, case-insensitive)
    +20  spike.current_cost falls inside expected range
    +15  spike direction (up/down) matches expected
    +15  |change_percent| >= min_change_percent

Aggregate report written to benchmarks/results/latest.md with a table,
pass rate (score >= 80), mean score, and per-provider breakdown.

Run as:
    python benchmarks/run_benchmark.py
    python benchmarks/run_benchmark.py --filter gcp
    python benchmarks/run_benchmark.py --json     # machine-readable to stdout
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Make src/ importable without installing the package — convenient for CI
# and for running the benchmark from a bare clone.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ghosthunter.providers.billing_file import (  # noqa: E402
    BillingFileError,
    load_spikes_from_file,
)

FIXTURES_DIR = Path(__file__).parent / "spikes"
RESULTS_DIR = Path(__file__).parent / "results"
PASS_THRESHOLD = 80  # score >= this counts as a pass


# Point scoring weights — keep in sync with the rubric in the docstring.
POINTS_SERVICE_EXACT = 50
POINTS_SERVICE_SUBSTRING = 25
POINTS_COST_IN_RANGE = 20
POINTS_DIRECTION = 15
POINTS_MAGNITUDE = 15


@dataclass
class ScenarioResult:
    id: str
    provider: str
    difficulty: str
    description: str
    score: int
    passed: bool
    detected_service: str | None
    detected_current: float | None
    detected_previous: float | None
    detected_change_pct: float | None
    detected_grouping: str | None
    n_spikes_returned: int
    checks: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    error: str | None = None


def _score_service(expected: str, actual: str | None) -> tuple[int, str]:
    if actual is None:
        return 0, "no spike returned"
    exp = expected.strip().lower()
    act = actual.strip().lower()
    if act == exp:
        return POINTS_SERVICE_EXACT, f"service name matches exactly ({actual!r})"
    if exp in act or act in exp:
        return POINTS_SERVICE_SUBSTRING, (
            f"service name substring match ({actual!r} vs expected {expected!r})"
        )
    return 0, f"service name mismatch ({actual!r} vs expected {expected!r})"


def _score_scenario(ground_truth: dict, spikes: list) -> ScenarioResult:
    spike_gt = ground_truth["spike"]
    expected_service: str = spike_gt["service"]
    expected_direction: str = spike_gt["direction"]
    min_pct: float = spike_gt["min_change_percent"]
    cost_lo, cost_hi = spike_gt["current_cost_range"]

    result = ScenarioResult(
        id=ground_truth["id"],
        provider=ground_truth["provider"],
        difficulty=ground_truth["difficulty"],
        description=ground_truth["description"],
        score=0,
        passed=False,
        detected_service=None,
        detected_current=None,
        detected_previous=None,
        detected_change_pct=None,
        detected_grouping=None,
        n_spikes_returned=len(spikes),
    )

    if not spikes:
        result.reasons.append("parser returned zero spikes")
        result.checks = {
            "service": False,
            "direction": False,
            "magnitude": False,
            "cost_in_range": False,
        }
        return result

    top = spikes[0]
    result.detected_service = top.service
    result.detected_current = top.current_cost
    result.detected_previous = top.previous_cost
    result.detected_change_pct = top.change_percent
    result.detected_grouping = top.grouping

    # --- service match ---
    pts, reason = _score_service(expected_service, top.service)
    result.score += pts
    result.reasons.append(f"[{pts:+d}] {reason}")
    result.checks["service"] = pts > 0

    # --- direction ---
    actual_direction = "up" if top.change_percent >= 0 else "down"
    if actual_direction == expected_direction:
        result.score += POINTS_DIRECTION
        result.reasons.append(f"[+{POINTS_DIRECTION}] direction matches ({actual_direction})")
        result.checks["direction"] = True
    else:
        result.reasons.append(
            f"[+0] direction wrong (got {actual_direction}, expected {expected_direction})"
        )
        result.checks["direction"] = False

    # --- magnitude ---
    abs_pct = abs(top.change_percent)
    if abs_pct >= min_pct:
        result.score += POINTS_MAGNITUDE
        result.reasons.append(f"[+{POINTS_MAGNITUDE}] |{abs_pct:.0f}%| >= {min_pct:.0f}%")
        result.checks["magnitude"] = True
    else:
        result.reasons.append(f"[+0] |{abs_pct:.0f}%| < required {min_pct:.0f}%")
        result.checks["magnitude"] = False

    # --- cost in range ---
    if cost_lo <= top.current_cost <= cost_hi:
        result.score += POINTS_COST_IN_RANGE
        result.reasons.append(
            f"[+{POINTS_COST_IN_RANGE}] current_cost "
            f"${top.current_cost:,.0f} in [${cost_lo:,.0f}, ${cost_hi:,.0f}]"
        )
        result.checks["cost_in_range"] = True
    else:
        result.reasons.append(
            f"[+0] current_cost ${top.current_cost:,.0f} outside [${cost_lo:,.0f}, ${cost_hi:,.0f}]"
        )
        result.checks["cost_in_range"] = False

    result.passed = result.score >= PASS_THRESHOLD
    return result


def _run_one(scenario_id: str) -> ScenarioResult:
    csv_path = FIXTURES_DIR / f"{scenario_id}.csv"
    gt_path = FIXTURES_DIR / f"{scenario_id}.json"
    with gt_path.open() as f:
        ground_truth = json.load(f)

    try:
        spikes = load_spikes_from_file(csv_path)
    except BillingFileError as exc:
        result = ScenarioResult(
            id=scenario_id,
            provider=ground_truth["provider"],
            difficulty=ground_truth["difficulty"],
            description=ground_truth["description"],
            score=0,
            passed=False,
            detected_service=None,
            detected_current=None,
            detected_previous=None,
            detected_change_pct=None,
            detected_grouping=None,
            n_spikes_returned=0,
            error=f"BillingFileError: {exc}",
        )
        return result

    return _score_scenario(ground_truth, spikes)


def _discover_scenario_ids() -> list[str]:
    return sorted(p.stem for p in FIXTURES_DIR.glob("*.json"))


def _render_markdown(results: list[ScenarioResult]) -> str:
    n = len(results)
    passed = sum(1 for r in results if r.passed)
    mean = sum(r.score for r in results) / max(n, 1)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines: list[str] = []
    lines.append("# Ghosthunter Benchmark — Latest Run")
    lines.append("")
    lines.append(f"Generated: {ts}")
    lines.append("")
    lines.append(
        f"**Scenarios**: {n}  ·  **Passed** (score ≥ {PASS_THRESHOLD}): "
        f"{passed}/{n} ({100 * passed / max(n, 1):.0f}%)  ·  "
        f"**Mean score**: {mean:.1f}/100"
    )
    lines.append("")

    # Per-provider breakdown
    by_provider: dict[str, list[ScenarioResult]] = {}
    for r in results:
        by_provider.setdefault(r.provider, []).append(r)
    if len(by_provider) > 1:
        lines.append("## By provider")
        lines.append("")
        lines.append("| Provider | Scenarios | Passed | Mean score |")
        lines.append("|----------|-----------|--------|------------|")
        for prov, rs in sorted(by_provider.items()):
            rp = sum(1 for r in rs if r.passed)
            m = sum(r.score for r in rs) / max(len(rs), 1)
            lines.append(f"| {prov} | {len(rs)} | {rp}/{len(rs)} | {m:.1f} |")
        lines.append("")

    # Scenario table
    lines.append("## Scenarios")
    lines.append("")
    lines.append("| Scenario | Provider | Difficulty | Score | Pass | Detected | Change % |")
    lines.append("|----------|----------|------------|-------|------|----------|----------|")
    for r in results:
        tick = "✅" if r.passed else "❌"
        svc = r.detected_service or "(none)"
        pct = (
            f"{r.detected_change_pct:+.0f}%"
            if r.detected_change_pct is not None and r.detected_change_pct != float("inf")
            else "—"
        )
        lines.append(
            f"| `{r.id}` | {r.provider} | {r.difficulty} | {r.score}/100 | {tick} | {svc} | {pct} |"
        )
    lines.append("")

    # Failures detail
    failures = [r for r in results if not r.passed]
    if failures:
        lines.append("## Failures")
        lines.append("")
        for r in failures:
            lines.append(f"### `{r.id}` — score {r.score}/100")
            lines.append("")
            lines.append(f"_{r.description}_")
            lines.append("")
            if r.error:
                lines.append(f"**Error**: `{r.error}`")
                lines.append("")
            for reason in r.reasons:
                lines.append(f"- {reason}")
            lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ghosthunter detection benchmark")
    parser.add_argument(
        "--filter",
        help="Substring to filter scenario ids (e.g. 'gcp', 'bigquery')",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout instead of the summary.",
    )
    parser.add_argument(
        "--write-report",
        action="store_true",
        default=True,
        help="Write benchmarks/results/latest.md (default on)",
    )
    args = parser.parse_args(argv)

    scenario_ids = _discover_scenario_ids()
    if not scenario_ids:
        print(
            f"No scenarios found in {FIXTURES_DIR}. "
            f"Run `python benchmarks/generate_fixtures.py` first.",
            file=sys.stderr,
        )
        return 2

    if args.filter:
        scenario_ids = [s for s in scenario_ids if args.filter in s]
        if not scenario_ids:
            print(f"No scenarios match filter {args.filter!r}", file=sys.stderr)
            return 2

    results = [_run_one(sid) for sid in scenario_ids]

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2, default=str))
        return 0

    # Console summary
    n = len(results)
    passed = sum(1 for r in results if r.passed)
    mean = sum(r.score for r in results) / max(n, 1)
    print(f"\nGhosthunter Benchmark — {n} scenarios")
    print(f"Passed (score >= {PASS_THRESHOLD}): {passed}/{n}  ·  Mean: {mean:.1f}/100\n")
    col_w = max(len(r.id) for r in results)
    for r in results:
        tick = "✅" if r.passed else "❌"
        svc = r.detected_service or "(none)"
        print(f"  {tick}  {r.id:<{col_w}}  {r.score:3d}/100  {svc}")
    print()

    if args.write_report:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = RESULTS_DIR / "latest.md"
        out.write_text(_render_markdown(results))
        print(f"Wrote {out.relative_to(_REPO_ROOT)}\n")

    return 0 if passed == n else 1


if __name__ == "__main__":
    sys.exit(main())
