"""Regenerate benchmark fixtures (CSVs + ground-truth JSONs) from scenarios.py.

Run as:
    python benchmarks/generate_fixtures.py

Outputs (committed to git):
    benchmarks/spikes/<scenario_id>.csv    — 60-day FOCUS 1.0 billing export
    benchmarks/spikes/<scenario_id>.json   — ground-truth expected answers

Determinism: a per-scenario seed (hash of scenario_id) keeps daily noise
stable, so re-running the generator produces bit-identical files unless a
scenario config changed. This lets CSVs live in git as stable artifacts.

CSV schema (FOCUS 1.0 — cross-cloud):
    ChargePeriodStart, ServiceName, SkuId, SubAccountId, RegionId,
    ProviderName, BilledCost
"""
from __future__ import annotations

import csv
import json
import random
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from scenarios import SCENARIOS, Scenario


FIXTURES_DIR = Path(__file__).parent / "spikes"
BASELINE_DAYS = 30
SPIKE_DAYS = 30
START_DATE = date(2026, 2, 1)  # previous period starts here
NOISE_BASELINE = 0.10  # ±10% gaussian jitter on baseline days
NOISE_SPIKE = 0.05  # ±5% on the amplified target service

PROVIDER_NAMES = {
    "gcp": "Google Cloud",
    "aws": "AWS",
}

# SKUs to sprinkle on the non-target "background" services so the CSV has
# variety. Deterministic via the seeded RNG.
_GCP_BACKGROUND_SKUS = (
    "E2 Instance Core", "N1 Standard Core", "SSD Capacity",
    "Storage Standard Class A Operations", "Network Egress", "Log Volume",
    "Query Analysis", "Shared VPC",
)
_AWS_BACKGROUND_SKUS = (
    "BoxUsage:t3.medium", "BoxUsage:m5.large", "TimedStorage-ByteHrs",
    "DataTransfer-Out-Bytes", "InvokeRequest", "DataProcessing-Bytes",
    "RunInstances:0002", "Requests-Tier1",
)
_REGIONS = {
    "gcp": ("us-central1", "us-east1", "europe-west1", "asia-southeast1"),
    "aws": ("us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1"),
}


def _seed_for(scenario_id: str) -> int:
    # Stable per-scenario integer seed — Python's hash() is salted, so use
    # a simple deterministic fold instead.
    h = 0
    for ch in scenario_id:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return h


def _noisy(rng: random.Random, base: float, sigma: float) -> float:
    return max(0.01, base * (1.0 + rng.gauss(0, sigma)))


def _rows_for(scenario: Scenario) -> list[dict[str, str]]:
    rng = random.Random(_seed_for(scenario.id))
    provider = PROVIDER_NAMES[scenario.provider]
    region_pool = _REGIONS[scenario.provider]
    bg_skus = (
        _GCP_BACKGROUND_SKUS if scenario.provider == "gcp"
        else _AWS_BACKGROUND_SKUS
    )

    target_sku = scenario.sku or "Usage"
    target_region = scenario.region or region_pool[0]
    target_account = scenario.sub_account or f"{scenario.provider}-default"

    # Background services: filter out any that collide with the target
    # service name so we don't double-book.
    background = [
        (name, cost) for (name, cost) in scenario.other_services
        if name != scenario.spike.service
    ]

    rows: list[dict[str, str]] = []
    for day_idx in range(BASELINE_DAYS + SPIKE_DAYS):
        day = START_DATE + timedelta(days=day_idx)
        is_spike_period = day_idx >= BASELINE_DAYS

        # -- target service row(s) for this day
        if scenario.spike.direction == "up":
            target_multiplier = scenario.spike_factor if is_spike_period else 1.0
        else:
            # "down" means the previous period was the big one; current drops.
            target_multiplier = 1.0 if is_spike_period else scenario.spike_factor

        target_cost = _noisy(
            rng,
            scenario.baseline_daily_cost * target_multiplier,
            NOISE_SPIKE,
        )
        rows.append({
            "ChargePeriodStart": day.isoformat(),
            "ServiceName": scenario.spike.service,
            "SkuId": target_sku,
            "SubAccountId": target_account,
            "RegionId": target_region,
            "ProviderName": provider,
            "BilledCost": f"{target_cost:.2f}",
        })

        # -- background services (flat ±10%)
        for (bg_name, bg_base) in background:
            bg_cost = _noisy(rng, bg_base, NOISE_BASELINE)
            rows.append({
                "ChargePeriodStart": day.isoformat(),
                "ServiceName": bg_name,
                "SkuId": rng.choice(bg_skus),
                "SubAccountId": f"{scenario.provider}-bg-{rng.randint(1, 3)}",
                "RegionId": rng.choice(region_pool),
                "ProviderName": provider,
                "BilledCost": f"{bg_cost:.2f}",
            })

    return rows


def _ground_truth(scenario: Scenario) -> dict:
    # Compute target current/previous totals exactly as the CSV will have
    # them (minus noise — we use expected values for human-readable
    # documentation only). The scorer itself uses what Ghosthunter extracts
    # from the CSV, not these predicted figures.
    if scenario.spike.direction == "up":
        expected_previous = scenario.baseline_daily_cost * BASELINE_DAYS
        expected_current = (
            scenario.baseline_daily_cost * scenario.spike_factor * SPIKE_DAYS
        )
    else:
        expected_previous = (
            scenario.baseline_daily_cost * scenario.spike_factor * BASELINE_DAYS
        )
        expected_current = scenario.baseline_daily_cost * SPIKE_DAYS
    return {
        "id": scenario.id,
        "provider": scenario.provider,
        "description": scenario.description,
        "difficulty": scenario.difficulty,
        "tags": list(scenario.tags),
        "spike": {
            "service": scenario.spike.service,
            "direction": scenario.spike.direction,
            "min_change_percent": scenario.spike.min_change_percent,
            "current_cost_range": list(scenario.spike.current_cost_range),
            # Informational — not used by the scorer.
            "expected_previous_cost_approx": round(expected_previous, 2),
            "expected_current_cost_approx": round(expected_current, 2),
        },
        "root_cause": asdict(scenario.root_cause) | {
            "evidence_keywords": list(scenario.root_cause.evidence_keywords),
        },
    }


def generate() -> list[tuple[str, int]]:
    """Write all fixtures. Returns [(scenario_id, row_count), ...]."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    summary: list[tuple[str, int]] = []
    for scenario in SCENARIOS:
        rows = _rows_for(scenario)
        csv_path = FIXTURES_DIR / f"{scenario.id}.csv"
        with csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

        gt_path = FIXTURES_DIR / f"{scenario.id}.json"
        with gt_path.open("w") as f:
            json.dump(_ground_truth(scenario), f, indent=2)
            f.write("\n")

        summary.append((scenario.id, len(rows)))
    return summary


if __name__ == "__main__":
    results = generate()
    print(f"Wrote {len(results)} fixtures to {FIXTURES_DIR}")
    for scenario_id, n_rows in results:
        print(f"  • {scenario_id}: {n_rows} rows")
