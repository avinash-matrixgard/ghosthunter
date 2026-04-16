"""Phase 6: memory palace filename → wing parsing.

Covers every filename pattern Ghosthunter recognizes plus negative cases.
The AWS patterns landed in Phase 2 alongside the advisor-mode scenario
work; this file locks down the full catalog now that AWS support is
complete.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ghosthunter.memory.palace import (
    default_wing_for_files,
    parse_wing_from_filename,
)


# ---------------------------------------------------------------------------
# GCP Console export format
# ---------------------------------------------------------------------------
class TestGCPConsolePattern:
    @pytest.mark.parametrize(
        "name,expected",
        [
            (
                "Billing Account for example.com_Reports, 2026-01-01.csv",
                "example.com",
            ),
            (
                "Billing Account for Acme Corp_Reports, 2026-03-01 - 2026-04-01.csv",
                "Acme Corp",
            ),
            (
                "Billing Account for billing-account-01234_Reports.csv",
                "billing-account-01234",
            ),
        ],
    )
    def test_gcp_console_export(self, name, expected):
        assert parse_wing_from_filename(name) == expected


# ---------------------------------------------------------------------------
# AWS filename conventions
# ---------------------------------------------------------------------------
class TestAWSPatterns:
    @pytest.mark.parametrize(
        "name,expected",
        [
            # 12-digit account-id prefix
            ("111122223333-aws-billing-detailed-2026-03.csv", "111122223333"),
            ("999988887777-aws-cost-report-2026.csv", "999988887777"),
            # Standard CUR naming: <report>-YYYYMMDD-YYYYMMDD.csv
            ("my-cur-report-20260301-20260401.csv", "my-cur-report"),
            ("prod-cost-export-20260101-20260201.csv", "prod-cost-export"),
            ("finops_cur.01-20260101-20260201.csv", "finops_cur.01"),
        ],
    )
    def test_aws_filenames(self, name, expected):
        assert parse_wing_from_filename(name) == expected


# ---------------------------------------------------------------------------
# Negative / edge cases
# ---------------------------------------------------------------------------
class TestFallthrough:
    @pytest.mark.parametrize(
        "name",
        [
            "random.csv",
            "report.json",
            "billing.csv",
            "",
            # 11-digit prefix — NOT an account id
            ("11112222333-aws-report.csv"),
            # CUR-like but wrong date shape (7 vs 8 digits)
            "report-2026030-20260401.csv",
            # GCP prefix but no "_Reports" suffix
            "Billing Account for foo.csv",
        ],
    )
    def test_fallback_to_default(self, name):
        assert parse_wing_from_filename(name) == "default"

    def test_accepts_path_object(self, tmp_path):
        p = tmp_path / "Billing Account for foo_Reports.csv"
        p.touch()
        assert parse_wing_from_filename(p) == "foo"


# ---------------------------------------------------------------------------
# default_wing_for_files — multi-file bundle resolution
# ---------------------------------------------------------------------------
class TestDefaultWingForFiles:
    def test_single_file(self):
        files = [Path("Billing Account for example.com_Reports.csv")]
        assert default_wing_for_files(files) == "example.com"

    def test_multiple_matching_files(self):
        files = [
            Path("111122223333-aws-billing-detailed-2026-01.csv"),
            Path("111122223333-aws-billing-detailed-2026-02.csv"),
        ]
        assert default_wing_for_files(files) == "111122223333"

    def test_mixed_wings_fall_back_to_default(self):
        # One GCP file, one AWS file — no single wing name applies.
        files = [
            Path("Billing Account for example.com_Reports.csv"),
            Path("111122223333-aws-billing-detailed-2026-01.csv"),
        ]
        assert default_wing_for_files(files) == "default"

    def test_ignores_unparseable_file_when_mixed_with_parseable(self):
        # One file matches; the other falls through. Since fallthrough
        # contributes "default", which the resolver discards, the unique
        # known wing wins.
        files = [
            Path("Billing Account for example.com_Reports.csv"),
            Path("random.csv"),
        ]
        assert default_wing_for_files(files) == "example.com"

    def test_all_unparseable_returns_default(self):
        files = [Path("random.csv"), Path("other.json")]
        assert default_wing_for_files(files) == "default"
