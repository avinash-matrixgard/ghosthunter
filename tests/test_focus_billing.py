"""FOCUS (FinOps Open Cost & Usage Spec) billing-file parsing.

FOCUS is the cross-cloud billing spec the FinOps Foundation ships at
https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS-Sample-Data.

Its column names (`ServiceName`, `BilledCost`, `ChargePeriodStart`,
`RegionName`, `SubAccountId`, ...) differ from AWS CUR and GCP Console
exports, so Ghosthunter's alias tuples needed to learn them.

These tests lock down that the FOCUS 1.0 CSV shape parses end-to-end
and produces the expected spike breakdown.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ghosthunter.providers.billing_file import (
    ACCOUNT_KEYS,
    COST_KEYS,
    DATE_KEYS,
    LOCATION_KEYS,
    SERVICE_KEYS,
    SKU_KEYS,
    load_spikes_from_file,
)


FIXTURE = (
    Path(__file__).parent / "fixtures" / "aws" / "focus_sample_small.csv"
)


class TestFocusColumnAliases:
    """The column tuples must include the FOCUS 1.0 canonical names."""

    def test_service_aliases_include_focus(self):
        assert "ServiceName" in SERVICE_KEYS

    def test_cost_aliases_include_focus(self):
        # BilledCost is the primary FOCUS cost column; EffectiveCost
        # and ListCost are common fallbacks.
        assert "BilledCost" in COST_KEYS
        assert "EffectiveCost" in COST_KEYS

    def test_date_aliases_include_focus(self):
        assert "ChargePeriodStart" in DATE_KEYS

    def test_account_aliases_include_focus(self):
        # SubAccount is the per-tenant id; BillingAccount is the invoice
        # parent. Both are recognized.
        assert "SubAccountId" in ACCOUNT_KEYS
        assert "BillingAccountId" in ACCOUNT_KEYS

    def test_location_aliases_include_focus(self):
        assert "RegionName" in LOCATION_KEYS

    def test_sku_aliases_include_focus(self):
        assert "SkuId" in SKU_KEYS


class TestFocusSampleParse:
    def test_fixture_exists(self):
        assert FIXTURE.exists(), f"FOCUS fixture missing: {FIXTURE}"

    def test_parses_and_detects_ec2_spike(self):
        spikes = load_spikes_from_file(
            FIXTURE, min_change_percent=20, min_absolute_change=10
        )
        assert spikes, "Expected at least one spike from the FOCUS fixture"

        # The fixture is shaped so EC2 goes from ~$12/day in January to
        # ~$50/day in February — big clear spike. S3 stays flat.
        top = spikes[0]
        assert top.service == "Amazon Elastic Compute Cloud", (
            f"EC2 should be the top spike; got {top.service!r}"
        )
        assert top.change_percent > 100, (
            f"EC2 change_percent should exceed 100%; got {top.change_percent}"
        )

    def test_flat_service_not_in_spike_list(self):
        spikes = load_spikes_from_file(
            FIXTURE, min_change_percent=20, min_absolute_change=10
        )
        # S3 in the fixture goes from $5.20 to $5.30 — no material spike.
        names = [s.service for s in spikes]
        assert "Amazon Simple Storage Service" not in names, (
            "flat S3 cost should not surface as a spike"
        )

    def test_grouping_is_service(self):
        spikes = load_spikes_from_file(FIXTURE)
        for s in spikes:
            assert s.grouping == "service"
