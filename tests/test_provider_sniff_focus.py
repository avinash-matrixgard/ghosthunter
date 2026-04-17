"""Regression test for FOCUS 1.0 provider auto-detection.

Before v1.0.2 the advisor-mode banner said ``Advisor mode (gcp)`` on
any billing export whose columns didn't match the AWS CUR or GCP
Console aliases — including every FOCUS 1.0 export, which uses a
cloud-agnostic schema. We now peek at ``ProviderName`` values (or
``ServiceName`` prefixes as a fallback) and route accordingly.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from ghosthunter.cli import _sniff_provider_from_file, _sniff_focus_rows


REPO_ROOT = Path(__file__).resolve().parent.parent
SYNTH = REPO_ROOT / "benchmarks" / "spikes"
FOCUS_SAMPLE_SMALL = REPO_ROOT / "benchmarks" / "real_world" / "focus_sample.csv"


# ---------------------------------------------------------------------------
# _sniff_focus_rows — pure-function unit tests
# ---------------------------------------------------------------------------
class TestSniffFocusRows:
    def test_empty_list_is_unknown(self):
        assert _sniff_focus_rows([]) is None

    def test_all_aws_provider_name(self):
        rows = [{"ProviderName": "AWS", "ServiceName": "Amazon EC2"}] * 10
        assert _sniff_focus_rows(rows) == "aws"

    def test_all_google_provider_name(self):
        rows = [
            {"ProviderName": "Google Cloud", "ServiceName": "Compute Engine"}
        ] * 10
        assert _sniff_focus_rows(rows) == "gcp"

    def test_provider_name_case_insensitive(self):
        rows = [
            {"ProviderName": "aws", "ServiceName": "X"},
            {"ProviderName": "AWS", "ServiceName": "Y"},
            {"ProviderName": "Aws", "ServiceName": "Z"},
        ]
        assert _sniff_focus_rows(rows) == "aws"

    def test_azure_majority_returns_none(self):
        """Ghosthunter doesn't have reasoner rules for Azure — returning
        None lets the caller fall through to a helpful warning rather
        than silently mis-routing to GCP or AWS rules."""
        rows = [
            {"ProviderName": "Microsoft", "ServiceName": "Azure SQL"}
        ] * 20 + [
            {"ProviderName": "AWS", "ServiceName": "Amazon S3"}
        ] * 3
        assert _sniff_focus_rows(rows) is None

    def test_fifty_fifty_aws_azure_returns_none(self):
        rows = [{"ProviderName": "AWS"}] * 5 + [{"ProviderName": "Microsoft"}] * 5
        assert _sniff_focus_rows(rows) is None

    def test_servicename_fallback_when_no_providername(self):
        rows = [
            {"ServiceName": "Amazon Elastic Compute Cloud"},
            {"ServiceName": "AWS Lambda"},
            {"ServiceName": "Amazon Simple Storage Service"},
        ]
        assert _sniff_focus_rows(rows) == "aws"

        gcp_rows = [
            {"ServiceName": "BigQuery"},
            {"ServiceName": "Cloud Run"},
            {"ServiceName": "Kubernetes Engine"},
        ]
        assert _sniff_focus_rows(gcp_rows) == "gcp"

    def test_servicename_prefix_amazoncloudwatch(self):
        """The CE export variant "AmazonCloudWatch" (no space) is real and
        shows up in FOCUS exports. The scorer in billing_file.py already
        treats it as AWS; the sniffer should agree."""
        rows = [{"ServiceName": "AmazonCloudWatch"}] * 10
        assert _sniff_focus_rows(rows) == "aws"


# ---------------------------------------------------------------------------
# _sniff_provider_from_file on synthetic + real fixtures
# ---------------------------------------------------------------------------
class TestSniffFromFile:
    @pytest.mark.parametrize("fixture_id,expected", [
        ("aws_01_nat_gateway_runaway", "aws"),
        ("aws_02_s3_lifecycle_miss", "aws"),
        ("aws_05_bedrock_runaway", "aws"),
        ("gcp_01_bigquery_slot_runaway", "gcp"),
        ("gcp_03_cloudrun_min_instances", "gcp"),
        ("gcp_05_gke_autoscaler_flapping", "gcp"),
    ])
    def test_synthetic_focus_fixtures_sniff_correctly(
        self, fixture_id: str, expected: str
    ):
        csv_path = SYNTH / f"{fixture_id}.csv"
        assert csv_path.exists(), f"missing benchmark fixture: {csv_path}"
        assert _sniff_provider_from_file(csv_path) == expected

    @pytest.mark.skipif(
        not FOCUS_SAMPLE_SMALL.exists(),
        reason="FOCUS sample data not downloaded (benchmarks/real_world/)",
    )
    def test_focus_sandbox_small_sample_sniffed_as_aws(self):
        # The FinOps Foundation's anonymized sample is majority-AWS by
        # row count. If this ever flips (new sample revision), we'll see
        # it here rather than in a broken banner at runtime.
        result = _sniff_provider_from_file(FOCUS_SAMPLE_SMALL)
        assert result == "aws", (
            f"FOCUS sample sniff returned {result!r}, expected 'aws'. "
            "If the upstream sample data changed, update this assertion."
        )


class TestSniffEdgeCases:
    def test_unreadable_file_returns_none(self, tmp_path: Path):
        p = tmp_path / "bad.csv"
        p.write_bytes(b"\x00\x01\x02 not utf-8 \x89\x90")
        # Must not raise — errors swallowed, caller falls back to default.
        result = _sniff_provider_from_file(p)
        assert result is None

    def test_empty_csv_returns_none(self, tmp_path: Path):
        p = tmp_path / "empty.csv"
        p.write_text("")
        assert _sniff_provider_from_file(p) is None

    def test_focus_csv_with_only_azure_returns_none(self, tmp_path: Path):
        p = tmp_path / "azure_only.csv"
        with p.open("w", newline="") as f:
            w = csv.DictWriter(
                f, fieldnames=["ChargePeriodStart", "ServiceName", "ProviderName", "BilledCost"]
            )
            w.writeheader()
            for _ in range(20):
                w.writerow({
                    "ChargePeriodStart": "2026-03-01",
                    "ServiceName": "Azure SQL Database",
                    "ProviderName": "Microsoft",
                    "BilledCost": "1.00",
                })
        # Azure isn't a supported provider → None (caller falls through).
        assert _sniff_provider_from_file(p) is None
