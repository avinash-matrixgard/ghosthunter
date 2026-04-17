"""Smoke tests for the ``ghosthunter`` CLI surface.

``cli.py`` is 1000+ SLOC with no direct tests prior to this file. We
don't exercise the full Opus/Sonnet loop (that's covered by
``test_investigator.py``). Instead we probe every CLI command's
non-LLM paths to make sure:

- ``--help`` works for every subcommand.
- ``init`` writes a valid config.toml for both GCP and AWS.
- ``investigate --list`` short-circuits before any API call and
  prints a spike table.
- ``investigate`` correctly dispatches `--provider` and errors
  cleanly on missing files / wrong args.
- ``billing-template`` renders both GCP and AWS recipes.
- ``audit`` renders past investigations (mixed GCP/AWS rows) and
  handles a missing audit log gracefully.

Uses Typer's ``CliRunner`` to invoke in-process. ``CONFIG_PATH`` and
``AUDIT_LOG_PATH`` get monkeypatched to ``tmp_path`` so tests never
touch ``~/.ghosthunter/``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ghosthunter import cli
from ghosthunter.cli import app
from ghosthunter.config import AWSConfig, BudgetConfig, Config


runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """Redirect CONFIG_PATH + AUDIT_LOG_PATH away from ``~/.ghosthunter``."""
    cfg = tmp_path / "config.toml"
    audit = tmp_path / "audit.log"
    monkeypatch.setattr(cli, "CONFIG_PATH", cfg)
    monkeypatch.setattr(cli, "AUDIT_LOG_PATH", audit)
    # Config module has its own module-level constants used by Config.load.
    from ghosthunter import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", cfg)
    monkeypatch.setattr(cfg_mod, "AUDIT_LOG_PATH", audit)
    return cfg, audit


@pytest.fixture
def fake_audit_log(isolated_config):
    """Write a small mixed-provider audit log so ``audit`` has something
    to render."""
    _, audit_path = isolated_config
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "timestamp": "2026-04-01T10:00:00",
            "service": "Cloud DNS",
            "succeeded": True,
            "commands_used": 5,
            "conclusion": {"root_cause": "DNS cache bypass"},
            "aborted_reason": None,
            "provider": "gcp",
        },
        {
            "timestamp": "2026-04-10T14:30:00",
            "service": "Amazon EC2",
            "succeeded": True,
            "commands_used": 4,
            "conclusion": {"root_cause": "NAT data transfer"},
            "aborted_reason": None,
            "provider": "aws",
            "ce_api_calls": 3,
        },
        {
            "timestamp": "2026-04-15T09:20:00",
            "service": "AWS Lambda",
            "succeeded": False,
            "commands_used": 2,
            "conclusion": None,
            "aborted_reason": "user quit",
            "provider": "aws",
            "ce_api_calls": 1,
        },
    ]
    audit_path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n"
    )
    return audit_path


# ---------------------------------------------------------------------------
# --help for every command
# ---------------------------------------------------------------------------
class TestHelpSurfaces:
    @pytest.mark.parametrize("command", [
        "--help",
        "init --help",
        "investigate --help",
        "chat --help",
        "billing-template --help",
        "demo --help",
        "audit --help",
        "palace --help",
    ])
    def test_help_exits_zero(self, command):
        result = runner.invoke(app, command.split())
        assert result.exit_code == 0, (
            f"`ghosthunter {command}` failed:\n{result.output}"
        )
        # Top-level --help should list all commands we claim to ship.
        if command == "--help":
            for sub in ["init", "investigate", "chat", "billing-template",
                        "demo", "audit", "palace"]:
                assert sub in result.output


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------
class TestInit:
    def test_init_gcp_writes_config(self, isolated_config, monkeypatch):
        """init picks gcp → prompts for project/dataset/lookback → writes TOML."""
        cfg_path, _ = isolated_config

        # Script the interactive prompts in order.
        responses = iter([
            "gcp",                         # provider
            "30",                          # lookback_days
            "my-proj",                     # GCP project ID
            "my-proj.billing_export",      # billing dataset
        ])
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            lambda *a, **k: next(responses),
        )

        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0, result.output

        # Config file exists and has the expected fields.
        assert cfg_path.exists()
        cfg = Config.load(cfg_path)
        assert cfg.provider == "gcp"
        assert cfg.project_id == "my-proj"
        assert cfg.billing_dataset == "my-proj.billing_export"
        assert cfg.lookback_days == 30
        assert cfg.aws is None

    def test_init_aws_writes_config(self, isolated_config, monkeypatch):
        cfg_path, _ = isolated_config
        responses = iter([
            "aws",                   # provider
            "30",                    # lookback_days
            "dev-sandbox",           # AWS profile
            "us-west-2",             # AWS region
            "111122223333",          # account id
        ])
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask",
            lambda *a, **k: next(responses),
        )

        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0, result.output

        cfg = Config.load(cfg_path)
        assert cfg.provider == "aws"
        assert cfg.aws is not None
        assert cfg.aws.profile == "dev-sandbox"
        assert cfg.aws.region == "us-west-2"
        assert cfg.aws.account_id == "111122223333"
        # GCP fields default to empty string.
        assert cfg.project_id == ""

    def test_init_refuses_to_overwrite_without_confirm(
        self, isolated_config, monkeypatch
    ):
        """Existing config + user declines overwrite → early exit, file untouched."""
        cfg_path, _ = isolated_config
        # Pre-existing config.
        original = Config(
            provider="gcp",
            project_id="keep-me",
            billing_dataset="keep-me.billing_export",
        )
        original.save(cfg_path)
        before = cfg_path.read_text()

        monkeypatch.setattr(
            "rich.prompt.Confirm.ask",
            lambda *a, **k: False,  # decline overwrite
        )

        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        assert "Cancelled" in result.output
        assert cfg_path.read_text() == before


# ---------------------------------------------------------------------------
# investigate --list (short-circuits before any API call)
# ---------------------------------------------------------------------------
class TestInvestigateList:
    def test_list_renders_spike_table_for_aws_ce_csv(self, isolated_config):
        fixture = (
            Path(__file__).parent / "fixtures" / "aws" / "ce_by_service.csv"
        )
        assert fixture.exists(), "AWS CE fixture missing"

        result = runner.invoke(
            app, ["investigate", "--list", str(fixture)]
        )
        assert result.exit_code == 0, result.output
        # Provider auto-sniffed as AWS.
        assert "aws" in result.output.lower()
        # The spike table should show EC2 - Other or similar.
        assert "EC2" in result.output or "Lambda" in result.output

    def test_list_renders_spike_table_for_focus_csv(self, isolated_config):
        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "aws"
            / "focus_sample_small.csv"
        )
        assert fixture.exists(), "FOCUS fixture missing"
        result = runner.invoke(
            app, ["investigate", "--list", str(fixture)]
        )
        assert result.exit_code == 0, result.output

    def test_missing_file_errors_cleanly(self, isolated_config):
        result = runner.invoke(
            app, ["investigate", "--list", "/tmp/does-not-exist-12345.csv"]
        )
        # Non-zero exit; message should mention the file.
        assert result.exit_code != 0 or "not found" in result.output.lower()

    def test_no_file_and_no_active_errors(self, isolated_config, monkeypatch):
        """Advisor mode without files should error, not crash."""
        # Don't hit the API-key gate.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-gate")
        result = runner.invoke(app, ["investigate"])
        assert result.exit_code != 0
        assert (
            "billing file" in result.output.lower()
            or "at least one" in result.output.lower()
        )

    def test_active_and_files_are_mutually_exclusive(self, isolated_config, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-gate")
        fixture = (
            Path(__file__).parent / "fixtures" / "aws" / "ce_by_service.csv"
        )
        result = runner.invoke(
            app, ["investigate", "--active", "-f", str(fixture)]
        )
        assert result.exit_code != 0
        assert "not both" in result.output.lower() or "either" in result.output.lower()


# ---------------------------------------------------------------------------
# billing-template
# ---------------------------------------------------------------------------
class TestBillingTemplate:
    def test_gcp_template_renders(self):
        result = runner.invoke(app, ["billing-template"])
        assert result.exit_code == 0, result.output
        # Should mention BigQuery + Console paths.
        lower = result.output.lower()
        assert "bigquery" in lower or "bq query" in lower
        assert "console" in lower or "reports" in lower

    def test_aws_template_renders(self):
        result = runner.invoke(
            app, ["billing-template", "--provider=aws"]
        )
        assert result.exit_code == 0, result.output
        lower = result.output.lower()
        # Three AWS paths (CE CSV, CE JSON, CUR) should all be mentioned.
        assert "cost explorer" in lower or " ce " in lower
        assert "cur" in lower


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------
class TestAudit:
    def test_audit_renders_mixed_provider_entries(self, fake_audit_log):
        result = runner.invoke(app, ["audit"])
        assert result.exit_code == 0, result.output
        # All three entries should appear.
        assert "Cloud DNS" in result.output
        assert "Amazon EC2" in result.output
        assert "AWS Lambda" in result.output
        # Provider column renders both gcp and aws.
        assert "gcp" in result.output
        assert "aws" in result.output
        # ce_api_calls should surface as "ce:N" for AWS rows.
        assert "ce:3" in result.output

    def test_audit_handles_missing_log(self, isolated_config):
        """No audit log yet → clean "No audit log yet." message, exit 0."""
        result = runner.invoke(app, ["audit"])
        assert result.exit_code == 0
        assert "no audit log" in result.output.lower()

    def test_audit_limit_flag(self, fake_audit_log):
        result = runner.invoke(app, ["audit", "--limit", "1"])
        assert result.exit_code == 0, result.output
        # Only the last entry should appear.
        assert "AWS Lambda" in result.output
        # The older entry should NOT.
        assert "Cloud DNS" not in result.output


# ---------------------------------------------------------------------------
# palace — status works without MemPalace installed
# ---------------------------------------------------------------------------
class TestPalace:
    def test_palace_status_without_mempalace(self, isolated_config):
        """`ghosthunter palace status` (the default) should always
        succeed, even if MemPalace / mcp isn't installed.
        """
        result = runner.invoke(app, ["palace"])
        assert result.exit_code == 0, result.output
        assert "palace" in result.output.lower() or "available" in result.output.lower()

    def test_palace_install_check(self, isolated_config):
        result = runner.invoke(app, ["palace", "install-check"])
        assert result.exit_code == 0, result.output
        # Whether each dep is installed is environment-dependent; we
        # only assert the section renders cleanly.
        assert "mcp" in result.output.lower() or "mempalace" in result.output.lower()
