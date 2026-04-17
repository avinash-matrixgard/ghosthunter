"""FOCUS Sandbox real-data smoke test.

Validates the parser on the FinOps Foundation's anonymized real-world
FOCUS 1.0 sample data (CC BY 4.0 — https://github.com/FinOps-Open-Cost-
and-Usage-Spec/FOCUS-Sample-Data).

This exists because our synthetic fixtures (benchmarks/spikes/) prove
the parser works on data we shaped ourselves. The FOCUS sandbox proves
it also works on a schema we *didn't* shape — specifically, anonymized
cross-cloud data produced by real billing systems.

## Skipping behavior

This test is skipped if the sample CSV is not present on disk. CI is
expected to skip unless the file has been downloaded. To run locally:

    cd <repo>/benchmarks/real_world
    curl -LO https://raw.githubusercontent.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS-Sample-Data/main/FOCUS-1.0/focus_sample.csv

## What we assert

1. Parser runs without raising.
2. >= 1 spike detected (the 1K-row sample has ~28 in practice).
3. Top spike has a non-empty service name.
4. All detected costs are finite numbers.
5. Multi-cloud service names appear (AWS + Azure at minimum) — the
   sample is explicitly cross-provider, so a parser that only
   recognized one provider would be a red flag.
6. Date-split mode engaged (previous + current periods computed).

## What we do NOT assert

- Specific dollar amounts — the sample has been heavily anonymized /
  scaled, so absolute figures aren't meaningful ground truth.
- Specific services — the anonymization process may change what shows
  up in a future revision of the sample.

These are intentionally loose smoke checks, not a benchmark. The
synthetic benchmark (benchmarks/run_benchmark.py) is where we score
correctness.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from ghosthunter.providers.billing_file import load_spikes_from_file


REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = REPO_ROOT / "benchmarks" / "real_world"
SAMPLE_SMALL = SAMPLE_DIR / "focus_sample.csv"
SAMPLE_10K = SAMPLE_DIR / "focus_sample_10000.csv"
SAMPLE_100K = SAMPLE_DIR / "focus_sample_100000.csv"


# ----------------------------------------------------------------------
# Small sample (1000 rows) — asserted in detail
# ----------------------------------------------------------------------
@pytest.mark.skipif(
    not SAMPLE_SMALL.exists(),
    reason=(
        "FOCUS sample not downloaded. To run: curl -LO "
        "https://raw.githubusercontent.com/FinOps-Open-Cost-and-Usage-Spec/"
        "FOCUS-Sample-Data/main/FOCUS-1.0/focus_sample.csv (into "
        "benchmarks/real_world/)"
    ),
)
def test_focus_small_sample_parses():
    spikes = load_spikes_from_file(SAMPLE_SMALL)
    assert spikes, "parser returned zero spikes on the FOCUS sample"

    top = spikes[0]
    assert top.service and top.service.strip(), (
        f"top spike has empty service name: {top!r}"
    )

    # All costs must be finite (no NaN). change_percent may be inf for
    # services that didn't exist in the previous period — that's valid.
    for s in spikes:
        assert math.isfinite(s.current_cost), (
            f"non-finite current_cost on {s.service!r}: {s.current_cost}"
        )
        assert math.isfinite(s.previous_cost), (
            f"non-finite previous_cost on {s.service!r}: {s.previous_cost}"
        )


@pytest.mark.skipif(
    not SAMPLE_SMALL.exists(),
    reason="FOCUS sample not downloaded",
)
def test_focus_small_sample_is_multi_cloud():
    """The sample mixes AWS + Azure rows. A parser that only recognized
    one provider's service names would surface an obvious skew here.
    """
    spikes = load_spikes_from_file(SAMPLE_SMALL)
    service_names = " ".join(s.service for s in spikes).lower()
    saw_aws = any(
        tag in service_names
        for tag in ("amazon", "aws ", "cloudwatch", "ec2")
    )
    saw_azure = any(
        tag in service_names
        for tag in ("azure", "microsoft", "virtual machines")
    )
    assert saw_aws, f"no AWS-looking services in top spikes: {[s.service for s in spikes[:10]]}"
    assert saw_azure, f"no Azure-looking services in top spikes: {[s.service for s in spikes[:10]]}"


@pytest.mark.skipif(
    not SAMPLE_SMALL.exists(),
    reason="FOCUS sample not downloaded",
)
def test_focus_small_sample_uses_date_split():
    """The sample is a full month of ChargePeriodStart dates, so the
    parser should pick the date-split path (not the total-only
    fallback) — meaning most spikes have a non-zero previous_cost.
    """
    spikes = load_spikes_from_file(SAMPLE_SMALL)
    with_prev = sum(1 for s in spikes if s.previous_cost > 0)
    assert with_prev >= len(spikes) // 2, (
        f"only {with_prev}/{len(spikes)} spikes had a previous_cost — "
        f"parser may have fallen back to total-only mode"
    )


# ----------------------------------------------------------------------
# Larger samples — loose assertions, gated separately so they can be
# skipped independently if only the small file is present.
# ----------------------------------------------------------------------
@pytest.mark.skipif(
    not SAMPLE_10K.exists(),
    reason="focus_sample_10000.csv not downloaded",
)
def test_focus_10k_sample_parses():
    spikes = load_spikes_from_file(SAMPLE_10K)
    assert len(spikes) >= 10, (
        f"expected >= 10 spikes on 10K-row sample, got {len(spikes)}"
    )


@pytest.mark.skipif(
    not SAMPLE_100K.exists(),
    reason="focus_sample_100000.csv not downloaded",
)
def test_focus_100k_sample_parses_under_budget():
    """Regression guard: parsing 100K real-schema rows should stay under
    a few seconds on commodity hardware.
    """
    import time

    t0 = time.monotonic()
    spikes = load_spikes_from_file(SAMPLE_100K)
    elapsed = time.monotonic() - t0

    assert len(spikes) >= 20, f"expected >= 20 spikes, got {len(spikes)}"
    assert elapsed < 10.0, (
        f"parsing 100K FOCUS rows took {elapsed:.1f}s — parser may have regressed"
    )
