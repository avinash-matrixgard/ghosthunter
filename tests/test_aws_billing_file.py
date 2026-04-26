"""Phase 2 tests: the billing-file parser handles all three AWS shapes.

Fixtures under tests/fixtures/aws/ mirror real AWS exports:
  - ce_by_service.csv          Cost Explorer UI download (grouped by Service)
  - ce_get_cost_and_usage.json `aws ce get-cost-and-usage` raw output
  - cur_line_items.csv         CUR CSV with lineItem/* columns
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ghosthunter.memory.palace import parse_wing_from_filename
from ghosthunter.providers.billing_file import (
    ACCOUNT_KEYS,
    GROUPING_PRIORITY,
    SERVICE_KEYWORDS_AWS,
    SERVICE_KEYWORDS_GCP,
    USAGE_TYPE_KEYS,
    _flatten_ce_json,
    load_spikes_from_file,
)

FIXTURES = Path(__file__).parent / "fixtures" / "aws"


# ---------------------------------------------------------------------------
# Column aliases picked up correctly
# ---------------------------------------------------------------------------
class TestColumnAliases:
    def test_account_keys_include_cur_columns(self):
        assert "lineItem/UsageAccountId" in ACCOUNT_KEYS
        assert "Linked Account" in ACCOUNT_KEYS

    def test_usage_type_keys_include_cur_columns(self):
        assert "lineItem/UsageType" in USAGE_TYPE_KEYS
        assert "UsageType" in USAGE_TYPE_KEYS

    def test_grouping_priority_includes_account_and_usage_type(self):
        assert "account" in GROUPING_PRIORITY
        assert "usage_type" in GROUPING_PRIORITY
        # Project still takes precedence over account when both present in a
        # single file; GCP files rarely have both.
        assert GROUPING_PRIORITY.index("project") < GROUPING_PRIORITY.index("account")


# ---------------------------------------------------------------------------
# CE UI CSV — by Service
# ---------------------------------------------------------------------------
class TestCostExplorerCSV:
    def test_parses_and_detects_spikes(self):
        spikes = load_spikes_from_file(
            FIXTURES / "ce_by_service.csv",
            min_change_percent=20,
            min_absolute_change=50,
        )
        assert spikes, "Expected at least one spike"

    def test_top_spike_is_ec2_other(self):
        spikes = load_spikes_from_file(
            FIXTURES / "ce_by_service.csv",
            min_change_percent=20,
            min_absolute_change=50,
        )
        names = [s.service for s in spikes[:3]]
        assert "EC2 - Other" in names, f"Expected EC2 - Other in top 3 spikes; got {names}"

    def test_grouping_is_service(self):
        spikes = load_spikes_from_file(FIXTURES / "ce_by_service.csv")
        for s in spikes:
            assert s.grouping == "service"


# ---------------------------------------------------------------------------
# CE JSON — aws ce get-cost-and-usage raw output
# ---------------------------------------------------------------------------
class TestCostExplorerJSON:
    def test_parses_raw_ce_response(self):
        spikes = load_spikes_from_file(
            FIXTURES / "ce_get_cost_and_usage.json",
            min_change_percent=20,
            min_absolute_change=50,
        )
        assert spikes
        # Same scenario as the CSV → same top spike.
        assert spikes[0].service == "EC2 - Other"

    def test_flatten_ce_json_maps_group_keys_to_columns(self):
        sample = {
            "GroupDefinitions": [
                {"Type": "DIMENSION", "Key": "SERVICE"},
            ],
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-03-01", "End": "2026-03-02"},
                    "Total": {},
                    "Groups": [
                        {
                            "Keys": ["Amazon EC2"],
                            "Metrics": {
                                "UnblendedCost": {
                                    "Amount": "12.34",
                                    "Unit": "USD",
                                },
                            },
                        }
                    ],
                }
            ],
        }
        rows = _flatten_ce_json(sample)
        assert len(rows) == 1
        row = rows[0]
        assert row["Service"] == "Amazon EC2"
        assert row["Start"] == "2026-03-01"
        assert row["UnblendedCost"] == pytest.approx(12.34)

    def test_flatten_ce_json_maps_usage_type_dimension(self):
        sample = {
            "GroupDefinitions": [
                {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
            ],
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-03-01", "End": "2026-03-02"},
                    "Groups": [
                        {
                            "Keys": ["BoxUsage:m5.xlarge"],
                            "Metrics": {"UnblendedCost": {"Amount": "50.00"}},
                        }
                    ],
                }
            ],
        }
        rows = _flatten_ce_json(sample)
        # USAGE_TYPE → column "UsageType"
        assert rows[0]["UsageType"] == "BoxUsage:m5.xlarge"

    def test_flatten_ce_json_handles_no_group_by(self):
        sample = {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-03-01", "End": "2026-03-02"},
                    "Total": {"UnblendedCost": {"Amount": "100.00"}},
                    "Groups": [],
                }
            ],
        }
        rows = _flatten_ce_json(sample)
        assert len(rows) == 1
        assert rows[0]["Service"] == "Total"
        assert rows[0]["UnblendedCost"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# CUR — lineItem/* columns
# ---------------------------------------------------------------------------
class TestCURLineItems:
    def test_parses_cur_with_lineitem_columns(self):
        spikes = load_spikes_from_file(
            FIXTURES / "cur_line_items.csv",
            min_change_percent=20,
            min_absolute_change=50,
        )
        assert spikes

    def test_top_contributors_surface_usage_type_and_account(self):
        spikes = load_spikes_from_file(
            FIXTURES / "cur_line_items.csv",
            min_change_percent=20,
            min_absolute_change=50,
        )
        ec2 = next((s for s in spikes if s.service == "AmazonEC2"), None)
        assert ec2 is not None, f"expected AmazonEC2 spike; got {[s.service for s in spikes]}"
        assert "usage_type" in ec2.top_contributors
        assert "account" in ec2.top_contributors
        usage_names = [name for name, _ in ec2.top_contributors["usage_type"]]
        assert "NatGateway-Bytes" in usage_names
        assert "BoxUsage:m5.xlarge" in usage_names

    def test_parquet_rejected_with_clear_message(self, tmp_path):
        p = tmp_path / "cur.parquet"
        p.write_bytes(b"parquet-magic")
        from ghosthunter.providers.billing_file import BillingFileError

        with pytest.raises(BillingFileError, match="Parquet"):
            load_spikes_from_file(p)


# ---------------------------------------------------------------------------
# Service keyword dicts
# ---------------------------------------------------------------------------
class TestServiceKeywords:
    def test_aws_dict_has_core_services(self):
        for svc in (
            "AWS Lambda",
            "Amazon Simple Storage Service",
            "Amazon Elastic Compute Cloud - Compute",
            "Amazon Relational Database Service",
            "AWS Key Management Service",
        ):
            assert svc in SERVICE_KEYWORDS_AWS, f"{svc} missing from AWS keywords"

    def test_gcp_dict_still_has_original_entries(self):
        assert "Cloud DNS" in SERVICE_KEYWORDS_GCP
        assert "BigQuery" in SERVICE_KEYWORDS_GCP


# ---------------------------------------------------------------------------
# Memory palace wing parser — AWS filename patterns
# ---------------------------------------------------------------------------
class TestWingParser:
    @pytest.mark.parametrize(
        "name,expected",
        [
            (
                "Billing Account for example.com_Reports, 2026-01-01.csv",
                "example.com",
            ),
            ("111122223333-aws-billing-detailed-2026-03.csv", "111122223333"),
            ("my-cur-report-20260301-20260401.csv", "my-cur-report"),
            ("random.csv", "default"),
        ],
    )
    def test_wing_patterns(self, name, expected):
        assert parse_wing_from_filename(name) == expected
