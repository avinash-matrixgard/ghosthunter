"""Phase 6: Config load/save/migration behavior.

Covers:
  - A legacy config (missing `provider` key) loads cleanly with
    provider defaulting to "gcp" and `aws` to None.
  - `migrate_config_in_place` rewrites such a legacy file to the new
    schema, adds `provider = "gcp"`, and is idempotent.
  - A new config round-trips through save/load without mutation.
  - A config with `provider = "aws"` + AWSConfig nested block survives
    save/load.
  - migrate_config_in_place is a no-op on missing files.
  - Audit-log entries carry the provider column and ce_api_calls field
    when written via _append_audit_log (as of Phase 4).
"""

from __future__ import annotations

import json
from pathlib import Path

import tomli

from ghosthunter.config import (
    AWSConfig,
    BudgetConfig,
    Config,
    migrate_config_in_place,
)

# ---------------------------------------------------------------------------
# Legacy (pre-provider) configs
# ---------------------------------------------------------------------------
LEGACY_TOML = """\
project_id = "my-proj"
billing_dataset = "my-proj.billing_export"
lookback_days = 30

[budget]
max_commands = 15
max_cost_usd = 1.0
max_seconds = 600.0
"""


def _write(path: Path, body: str) -> None:
    path.write_text(body)


class TestLegacyLoad:
    def test_legacy_loads_with_gcp_default(self, tmp_path):
        cfg_path = tmp_path / "config.toml"
        _write(cfg_path, LEGACY_TOML)
        cfg = Config.load(cfg_path)
        assert cfg.provider == "gcp"
        assert cfg.project_id == "my-proj"
        assert cfg.billing_dataset == "my-proj.billing_export"
        assert cfg.aws is None
        assert cfg.lookback_days == 30

    def test_legacy_save_writes_provider_field(self, tmp_path):
        cfg_path = tmp_path / "config.toml"
        _write(cfg_path, LEGACY_TOML)
        cfg = Config.load(cfg_path)
        cfg.save(cfg_path)
        data = tomli.loads(cfg_path.read_text())
        assert data["provider"] == "gcp"
        assert data["project_id"] == "my-proj"
        # `aws` key is dropped when None — save() strips it to keep the
        # file clean for users who never touched AWS mode.
        assert "aws" not in data


# ---------------------------------------------------------------------------
# Migration helper
# ---------------------------------------------------------------------------
class TestMigrateConfigInPlace:
    def test_rewrites_legacy_file(self, tmp_path):
        cfg_path = tmp_path / "config.toml"
        _write(cfg_path, LEGACY_TOML)
        assert migrate_config_in_place(cfg_path) is True
        data = tomli.loads(cfg_path.read_text())
        assert data["provider"] == "gcp"
        assert data["project_id"] == "my-proj"

    def test_noop_when_provider_already_present(self, tmp_path):
        cfg_path = tmp_path / "config.toml"
        # Save a new-shape config and capture its mtime.
        cfg = Config(provider="gcp", project_id="x", billing_dataset="x.y")
        cfg.save(cfg_path)
        before_bytes = cfg_path.read_bytes()
        # Migration is a no-op — file content is byte-identical afterward.
        assert migrate_config_in_place(cfg_path) is False
        assert cfg_path.read_bytes() == before_bytes

    def test_noop_when_file_missing(self, tmp_path):
        cfg_path = tmp_path / "nonexistent.toml"
        assert migrate_config_in_place(cfg_path) is False
        assert not cfg_path.exists()

    def test_idempotent_when_called_twice(self, tmp_path):
        cfg_path = tmp_path / "config.toml"
        _write(cfg_path, LEGACY_TOML)
        first = migrate_config_in_place(cfg_path)
        second = migrate_config_in_place(cfg_path)
        assert first is True
        assert second is False  # no second migration needed


# ---------------------------------------------------------------------------
# Round-trip — gcp + aws
# ---------------------------------------------------------------------------
class TestRoundTrip:
    def test_gcp_roundtrip(self, tmp_path):
        cfg_path = tmp_path / "config.toml"
        original = Config(
            provider="gcp",
            project_id="proj-xyz",
            billing_dataset="proj-xyz.billing_export",
            lookback_days=60,
            budget=BudgetConfig(max_commands=20, max_cost_usd=2.0, max_seconds=900.0),
        )
        original.save(cfg_path)
        loaded = Config.load(cfg_path)
        assert loaded.provider == "gcp"
        assert loaded.project_id == "proj-xyz"
        assert loaded.billing_dataset == "proj-xyz.billing_export"
        assert loaded.lookback_days == 60
        assert loaded.budget.max_commands == 20
        assert loaded.budget.max_cost_usd == 2.0
        assert loaded.budget.max_seconds == 900.0
        assert loaded.aws is None

    def test_aws_roundtrip_with_ack(self, tmp_path):
        cfg_path = tmp_path / "config.toml"
        original = Config(
            provider="aws",
            aws=AWSConfig(
                profile="dev",
                region="eu-west-1",
                account_id="111122223333",
                ce_api_cost_ack=True,
            ),
            lookback_days=30,
        )
        original.save(cfg_path)
        loaded = Config.load(cfg_path)
        assert loaded.provider == "aws"
        assert loaded.aws is not None
        assert loaded.aws.profile == "dev"
        assert loaded.aws.region == "eu-west-1"
        assert loaded.aws.account_id == "111122223333"
        assert loaded.aws.ce_api_cost_ack is True
        # GCP-specific fields default to empty strings, not None — both
        # shapes need to coexist without tripping anything.
        assert loaded.project_id == ""
        assert loaded.billing_dataset == ""

    def test_aws_ack_persists_through_save_cycle(self, tmp_path):
        """Regression: Phase 4 relies on ce_api_cost_ack surviving a save
        so scripted repeat-runs don't re-prompt.
        """
        cfg_path = tmp_path / "config.toml"
        cfg = Config(
            provider="aws",
            aws=AWSConfig(profile="prod", region="us-east-1", ce_api_cost_ack=False),
        )
        cfg.save(cfg_path)
        cfg = Config.load(cfg_path)
        assert cfg.aws.ce_api_cost_ack is False  # initial
        cfg.aws.ce_api_cost_ack = True
        cfg.save(cfg_path)
        cfg2 = Config.load(cfg_path)
        assert cfg2.aws.ce_api_cost_ack is True


# ---------------------------------------------------------------------------
# Audit log shape (Phase 4 + Phase 6)
# ---------------------------------------------------------------------------
class TestAuditLogProviderField:
    """Exercise the _append_audit_log helper with `extra={"provider": ...}`."""

    def test_audit_entry_carries_provider(self, tmp_path, monkeypatch):
        from ghosthunter import cli

        audit_path = tmp_path / "audit.log"
        monkeypatch.setattr(cli, "AUDIT_LOG_PATH", audit_path)

        class _FakeResult:
            class _Spike:
                service = "Amazon EC2"

            class _Budget:
                commands_used = 3

            spike = _Spike()
            succeeded = True
            budget = _Budget()
            conclusion = {"root_cause": "NAT gateway data transfer"}
            aborted_reason = None

        cli._append_audit_log(_FakeResult(), extra={"provider": "aws", "ce_api_calls": 4})
        lines = audit_path.read_text().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["provider"] == "aws"
        assert entry["ce_api_calls"] == 4
        assert entry["service"] == "Amazon EC2"
        assert entry["commands_used"] == 3
        assert entry["succeeded"] is True

    def test_audit_entry_without_extra_has_no_provider(self, tmp_path, monkeypatch):
        from ghosthunter import cli

        audit_path = tmp_path / "audit.log"
        monkeypatch.setattr(cli, "AUDIT_LOG_PATH", audit_path)

        class _R:
            class _S:
                service = "x"

            class _B:
                commands_used = 0

            spike = _S()
            succeeded = False
            budget = _B()
            conclusion = None
            aborted_reason = "user quit"

        cli._append_audit_log(_R())
        entry = json.loads(audit_path.read_text().splitlines()[0])
        # Provider is only present when the caller supplied it via extra.
        assert "provider" not in entry
        assert "ce_api_calls" not in entry
