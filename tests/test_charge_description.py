"""Regression test: ChargeDescription / LineItemDescription must flow
from the billing CSV through the parser into the spike's
`contributor_descriptions` dict and onward into the investigator's
initial prompt.

This was the v1.0.2→v1.0.3 UX gap: FOCUS 1.0 CSVs contain a
``ChargeDescription`` column ("$1.624 per On Demand Linux g5.4xlarge
Instance Hour"), but the parser only surfaced opaque SKU IDs. Advisor
mode then spent multiple API rounds asking the user to look up SKU
codes that the CSV already contained. The fix: thread descriptions
through the data model and render them alongside SKU IDs everywhere
they're displayed (CLI + Opus prompt).
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from ghosthunter.providers.billing_file import (
    DESCRIPTION_KEYS,
    load_spikes_from_file,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
FOCUS_100K = REPO_ROOT / "benchmarks" / "real_world" / "focus_sample_100000.csv"


def _write_focus_csv(path: Path, rows: list[dict]) -> None:
    """Helper: build a tiny FOCUS 1.0 CSV fixture with description column."""
    fieldnames = [
        "ChargePeriodStart", "ServiceName", "SkuId", "ChargeDescription",
        "SubAccountId", "RegionId", "ProviderName", "BilledCost",
    ]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Description column detection
# ---------------------------------------------------------------------------
class TestDescriptionKeyAliases:
    def test_focus_canonical_name_detected(self):
        """`ChargeDescription` is the canonical FOCUS 1.0 column name."""
        assert "ChargeDescription" in DESCRIPTION_KEYS

    def test_aws_cur_alias_detected(self):
        """AWS CUR uses a different name for the same thing."""
        assert "lineItem/LineItemDescription" in DESCRIPTION_KEYS

    def test_lowercase_variant_detected(self):
        assert "charge_description" in DESCRIPTION_KEYS


# ---------------------------------------------------------------------------
# End-to-end: CSV → spike.contributor_descriptions
# ---------------------------------------------------------------------------
class TestDescriptionThroughParser:
    def test_descriptions_attached_to_sku_contributors(self, tmp_path: Path):
        """60 days of data with two SKUs, the target service amplified in
        the second half. Descriptions must surface in spike output."""
        rows: list[dict] = []
        # Previous period (low cost)
        for day in range(1, 16):
            rows.append({
                "ChargePeriodStart": f"2026-03-{day:02d}",
                "ServiceName": "Amazon Elastic Compute Cloud",
                "SkuId": "SKUAAA1",
                "ChargeDescription": "$1.624 per On Demand Linux g5.4xlarge Instance Hour",
                "SubAccountId": "acct-prod",
                "RegionId": "us-east-1",
                "ProviderName": "AWS",
                "BilledCost": "10.00",
            })
        # Current period (high cost — the spike)
        for day in range(16, 31):
            rows.append({
                "ChargePeriodStart": f"2026-03-{day:02d}",
                "ServiceName": "Amazon Elastic Compute Cloud",
                "SkuId": "SKUAAA1",
                "ChargeDescription": "$1.624 per On Demand Linux g5.4xlarge Instance Hour",
                "SubAccountId": "acct-prod",
                "RegionId": "us-east-1",
                "ProviderName": "AWS",
                "BilledCost": "50.00",
            })
            rows.append({
                "ChargePeriodStart": f"2026-03-{day:02d}",
                "ServiceName": "Amazon Elastic Compute Cloud",
                "SkuId": "SKUBBB2",
                "ChargeDescription": "$0.10 per GB-month gp3 SSD storage",
                "SubAccountId": "acct-prod",
                "RegionId": "us-east-1",
                "ProviderName": "AWS",
                "BilledCost": "5.00",
            })

        fixture = tmp_path / "focus_fixture.csv"
        _write_focus_csv(fixture, rows)

        spikes = load_spikes_from_file(fixture)
        assert spikes, "parser returned no spikes"

        ec2 = next(
            (s for s in spikes if "Elastic Compute" in s.service), None
        )
        assert ec2 is not None, "EC2 spike not present"

        # The descriptions must have been attached to the SKU contributors.
        assert ec2.contributor_descriptions, (
            "contributor_descriptions is empty — descriptions lost in parsing"
        )
        assert "sku:SKUAAA1" in ec2.contributor_descriptions
        assert "g5.4xlarge" in ec2.contributor_descriptions["sku:SKUAAA1"]

    def test_no_description_column_doesnt_break_parsing(self, tmp_path: Path):
        """Older GCP Console exports have no description column. Parser
        must still work — just without descriptions."""
        fieldnames = [
            "Usage start date", "Service description", "SKU description",
            "Project ID", "Cost ($)",
        ]
        p = tmp_path / "gcp_no_desc.csv"
        with p.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for day in range(1, 31):
                w.writerow({
                    "Usage start date": f"2026-03-{day:02d}",
                    "Service description": "BigQuery",
                    "SKU description": "Analysis",
                    "Project ID": "proj-a",
                    "Cost ($)": "10.0" if day < 16 else "100.0",
                })

        spikes = load_spikes_from_file(p)
        assert spikes
        assert spikes[0].contributor_descriptions == {}

    def test_descriptions_only_for_sku_and_usage_type_dims(self, tmp_path: Path):
        """Descriptions get attached to opaque-ID dimensions (sku,
        usage_type), not to region/project/account which already have
        human names. Prevents noise in the prompt."""
        rows = []
        for day in range(1, 31):
            rows.append({
                "ChargePeriodStart": f"2026-03-{day:02d}",
                "ServiceName": "Amazon S3",
                "SkuId": "STORAGE",
                "ChargeDescription": "$0.023 per GB-month standard storage",
                "SubAccountId": "acct-a",
                "RegionId": "us-east-1",
                "ProviderName": "AWS",
                "BilledCost": "20.0" if day < 16 else "100.0",
            })
        p = tmp_path / "s3.csv"
        _write_focus_csv(p, rows)

        spikes = load_spikes_from_file(p)
        spike = spikes[0]
        # Region and account keys must NOT show up in
        # contributor_descriptions — only sku.
        keys = list(spike.contributor_descriptions.keys())
        assert all(k.startswith(("sku:", "usage_type:")) for k in keys), (
            f"unexpected dim in contributor_descriptions: {keys}"
        )


# ---------------------------------------------------------------------------
# Real-world FOCUS sandbox: the exact bug we caught
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not FOCUS_100K.exists(),
    reason="FOCUS 100K sample not downloaded",
)
def test_focus_100k_surfaces_g5_description():
    """The UX regression that motivated v1.0.3: without this, Opus never
    learned that SKU ``4GQWNPC9K2PZAY97`` means a g5.4xlarge GPU hour
    and kept asking the user to decode it."""
    spikes = load_spikes_from_file(FOCUS_100K)
    ec2 = next(
        (s for s in spikes if "Elastic Compute" in s.service), None
    )
    assert ec2 is not None, (
        "No EC2 spike in FOCUS 100K sample — the fixture data may have changed"
    )
    top_sku = next(iter(ec2.top_contributors.get("sku", [])), None)
    assert top_sku is not None, "no SKU contributors in EC2 spike"
    sku_id = top_sku[0]
    desc = ec2.contributor_descriptions.get(f"sku:{sku_id}")
    assert desc, (
        f"top SKU {sku_id} has no description attached "
        f"(known keys: {list(ec2.contributor_descriptions)[:5]}…)"
    )
    # The g5.4xlarge description contains the instance family — that's
    # the one word that would let Opus conclude without asking.
    assert "Instance Hour" in desc or "GB-month" in desc, (
        f"description doesn't look like an AWS rate string: {desc!r}"
    )


# ---------------------------------------------------------------------------
# Initial prompt renders descriptions inline
# ---------------------------------------------------------------------------
class TestPromptRendering:
    def test_prompt_includes_description_when_present(self):
        from ghosthunter.investigator import _build_initial_prompt
        from ghosthunter.providers.base import CostSpike

        spike = CostSpike(
            service="Amazon EC2",
            current_cost=500.0,
            previous_cost=100.0,
            change_percent=400.0,
            top_contributors={
                "sku": [("SKUAAA1", 300.0), ("SKUBBB2", 50.0)],
            },
            contributor_descriptions={
                "sku:SKUAAA1": "$1.624 per On Demand Linux g5.4xlarge Instance Hour",
            },
        )
        prompt = _build_initial_prompt(spike)
        assert "SKUAAA1" in prompt
        assert "g5.4xlarge" in prompt, (
            "description didn't make it into the prompt — Opus can't see it"
        )
        # SKUBBB2 doesn't have a description — it should still render with
        # just cost, no dangling dash or empty description.
        assert "SKUBBB2" in prompt
        assert "SKUBBB2: $50.00" in prompt  # no trailing "—"

    def test_prompt_works_without_any_descriptions(self):
        from ghosthunter.investigator import _build_initial_prompt
        from ghosthunter.providers.base import CostSpike

        spike = CostSpike(
            service="BigQuery",
            current_cost=500.0,
            previous_cost=100.0,
            change_percent=400.0,
            top_contributors={"sku": [("Analysis", 300.0)]},
        )
        prompt = _build_initial_prompt(spike)
        assert "Analysis" in prompt
        # No descriptions attached, so no em-dash garbage in the prompt.
        assert "Analysis: $300.00" in prompt
