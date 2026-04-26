"""Tests for ``providers/gcp.GCPProvider``.

``AWSProvider`` had a test file (`tests/test_aws_provider.py`) from
day one; the GCP equivalent didn't. This file fills that gap.

Coverage:

- ``execute_command``
    * Validator rejects a bad command → ``CommandRejectedError`` and
      NO subprocess is spawned (defense in depth).
    * Normal path: subprocess returns stdout, `CommandResult` is
      populated correctly.
    * Subprocess exit != 0 surfaces in `CommandResult.exit_code`.
    * Large stdout is truncated to ``max_output_bytes`` and
      ``truncated=True``.
    * Timeout raises ``CommandTimeoutError`` and the proc is killed.
- ``_sandbox_env``
    * Keeps ``PATH``, ``HOME``, ``USER``, ``LANG``, ``LC_ALL`` plus
      the GCP credential vars (``CLOUDSDK_CONFIG``,
      ``GOOGLE_APPLICATION_CREDENTIALS``, etc.).
    * Strips unrelated environment variables.
    * Pins ``CLOUDSDK_CORE_PROJECT`` to the configured project if the
      env doesn't already set it.
- ``fetch_billing_spikes``
    * Raises ``GCPProviderError`` with an actionable message when the
      ``google-cloud-bigquery`` import failed (``_BQ_AVAILABLE=False``).
    * Raises when ``billing_dataset`` is not configured.
- ``_rows_to_spikes``
    * Pivots (service, window) rows into ``CostSpike`` objects.
    * Applies both percent and absolute thresholds.
    * Sorts by absolute change (largest first).
- ``BaseProvider`` conformance
    * ``provider_key == "gcp"``.
    * ``cli_tools() == ("gcloud", "bq", "gsutil")``.
    * ``env_keep_list()`` contains the GCP creds + shared PATH set.
    * ``provider_hint_for_reasoner()`` mentions gcloud/bq/gsutil.
- ``quote_for_shell`` — escapes shell metacharacters.
"""

from __future__ import annotations

import asyncio

import pytest

from ghosthunter.providers import gcp as gcp_mod
from ghosthunter.providers.base import BaseProvider
from ghosthunter.providers.gcp import (
    CommandRejectedError,
    CommandResult,
    CommandTimeoutError,
    GCPProvider,
    GCPProviderError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _FakeProc:
    """Minimal stand-in for asyncio subprocess.Process."""

    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        communicate_delay: float = 0.0,
        hang: bool = False,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._communicate_delay = communicate_delay
        self._hang = hang
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            # Block forever so the caller times out.
            await asyncio.Event().wait()
        if self._communicate_delay:
            await asyncio.sleep(self._communicate_delay)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def _patch_subprocess(monkeypatch, proc: _FakeProc) -> list[dict]:
    """Replace asyncio.create_subprocess_shell with a stub that records
    calls and returns ``proc``. Returns the call-log list."""
    call_log: list[dict] = []

    async def _fake_create(cmd, **kwargs):
        call_log.append({"cmd": cmd, "kwargs": kwargs})
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_create)
    return call_log


# ---------------------------------------------------------------------------
# execute_command — pre-validation
# ---------------------------------------------------------------------------
class TestExecuteCommandPreValidation:
    def test_blocked_command_raises_without_spawning_subprocess(self, monkeypatch):
        prov = GCPProvider(project_id="demo-proj")
        # Script subprocess_shell to record any (unexpected) invocation.
        log = _patch_subprocess(monkeypatch, _FakeProc())

        with pytest.raises(CommandRejectedError) as excinfo:
            asyncio.run(prov.execute_command("rm -rf /"))
        # Message includes the blocking layer.
        assert "L1" in str(excinfo.value)
        # Crucial: the shell was NEVER invoked.
        assert log == []

    def test_non_allowlisted_gcloud_verb_rejected(self, monkeypatch):
        prov = GCPProvider(project_id="demo-proj")
        log = _patch_subprocess(monkeypatch, _FakeProc())
        with pytest.raises(CommandRejectedError):
            asyncio.run(prov.execute_command("gcloud compute instances delete vm1"))
        assert log == []


# ---------------------------------------------------------------------------
# execute_command — happy path
# ---------------------------------------------------------------------------
class TestExecuteCommandHappyPath:
    def test_successful_run_returns_command_result(self, monkeypatch):
        prov = GCPProvider(project_id="demo-proj")
        proc = _FakeProc(stdout=b"line1\nline2\n", returncode=0)
        log = _patch_subprocess(monkeypatch, proc)

        result = asyncio.run(prov.execute_command("gcloud compute instances list --format=json"))

        assert isinstance(result, CommandResult)
        assert result.command == "gcloud compute instances list --format=json"
        assert result.stdout == "line1\nline2\n"
        assert result.stderr == ""
        assert result.exit_code == 0
        assert result.truncated is False
        assert result.duration_seconds >= 0
        # Shell actually got invoked with the full command.
        assert len(log) == 1
        assert log[0]["cmd"] == "gcloud compute instances list --format=json"

    def test_nonzero_exit_code_surfaced(self, monkeypatch):
        prov = GCPProvider(project_id="demo-proj")
        _patch_subprocess(
            monkeypatch,
            _FakeProc(stdout=b"", stderr=b"ERROR: not found", returncode=2),
        )
        result = asyncio.run(prov.execute_command("gcloud compute instances list"))
        assert result.exit_code == 2
        assert "ERROR" in result.stderr
        assert not result.succeeded

    def test_output_truncation(self, monkeypatch):
        prov = GCPProvider(project_id="demo-proj", max_output_bytes=50)
        oversized = b"X" * 200
        _patch_subprocess(monkeypatch, _FakeProc(stdout=oversized))
        result = asyncio.run(prov.execute_command("gcloud compute instances list"))
        assert result.truncated is True
        assert len(result.stdout) == 50

    def test_stdout_utf8_decoding_is_lenient(self, monkeypatch):
        """Invalid UTF-8 shouldn't crash — should be replaced."""
        prov = GCPProvider(project_id="demo-proj")
        _patch_subprocess(
            monkeypatch,
            _FakeProc(stdout=b"\xff\xfe not utf-8", returncode=0),
        )
        result = asyncio.run(prov.execute_command("gcloud compute instances list"))
        # Should not raise; replacement chars in output.
        assert "not utf-8" in result.stdout


# ---------------------------------------------------------------------------
# execute_command — timeout
# ---------------------------------------------------------------------------
class TestExecuteCommandTimeout:
    def test_timeout_raises_and_kills_proc(self, monkeypatch):
        prov = GCPProvider(project_id="demo-proj", command_timeout=1)
        proc = _FakeProc(hang=True)  # never returns from communicate()
        _patch_subprocess(monkeypatch, proc)

        with pytest.raises(CommandTimeoutError) as excinfo:
            asyncio.run(prov.execute_command("gcloud compute instances list"))
        assert "1s" in str(excinfo.value)
        assert proc.killed, "proc.kill() must be called on timeout"


# ---------------------------------------------------------------------------
# _sandbox_env
# ---------------------------------------------------------------------------
class TestSandboxEnv:
    def test_keeps_gcp_and_shared_vars(self, monkeypatch):
        # Set up a representative env.
        monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
        monkeypatch.setenv("HOME", "/home/test")
        monkeypatch.setenv("USER", "test")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
        monkeypatch.setenv("CLOUDSDK_CONFIG", "/tmp/cloudsdk")
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa-key.json")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "env-proj")
        # Pollutants that should be stripped.
        monkeypatch.setenv("NPM_TOKEN", "don't leak me")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "stale aws key")

        prov = GCPProvider(project_id="demo-proj")
        env = prov._sandbox_env()

        # Kept.
        assert env.get("PATH") == "/usr/local/bin:/usr/bin"
        assert env.get("HOME") == "/home/test"
        assert env.get("USER") == "test"
        assert env.get("LANG") == "en_US.UTF-8"
        assert env.get("LC_ALL") == "en_US.UTF-8"
        assert env.get("CLOUDSDK_CONFIG") == "/tmp/cloudsdk"
        assert env.get("GOOGLE_APPLICATION_CREDENTIALS") == "/tmp/sa-key.json"
        assert env.get("GOOGLE_CLOUD_PROJECT") == "env-proj"

        # Stripped.
        assert "NPM_TOKEN" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env

    def test_pins_cloudsdk_core_project_default(self, monkeypatch):
        """If CLOUDSDK_CORE_PROJECT isn't set in the env, the provider
        fills it in with its ``project_id`` so gcloud commands without
        explicit --project still target the right project."""
        monkeypatch.delenv("CLOUDSDK_CORE_PROJECT", raising=False)
        prov = GCPProvider(project_id="demo-proj")
        env = prov._sandbox_env()
        assert env.get("CLOUDSDK_CORE_PROJECT") == "demo-proj"

    def test_user_env_overrides_pinned_default(self, monkeypatch):
        """If the caller's env already has CLOUDSDK_CORE_PROJECT, the
        provider should NOT overwrite it with its own project_id."""
        monkeypatch.setenv("CLOUDSDK_CORE_PROJECT", "user-project")
        prov = GCPProvider(project_id="demo-proj")
        env = prov._sandbox_env()
        assert env.get("CLOUDSDK_CORE_PROJECT") == "user-project"


# ---------------------------------------------------------------------------
# fetch_billing_spikes — guard paths
# ---------------------------------------------------------------------------
class TestFetchBillingSpikesGuards:
    def test_raises_when_bigquery_unavailable(self, monkeypatch):
        """Simulate the optional dependency being missing. Caller
        should see a clear error, not an ImportError."""
        monkeypatch.setattr(gcp_mod, "_BQ_AVAILABLE", False)
        prov = GCPProvider(
            project_id="demo-proj",
            billing_dataset="demo-proj.billing",
        )
        with pytest.raises(GCPProviderError, match="google-cloud-bigquery"):
            prov.fetch_billing_spikes()

    def test_raises_when_billing_dataset_missing(self):
        """Even if BigQuery is installed, we can't fetch without knowing
        the dataset. Error must be actionable."""
        prov = GCPProvider(project_id="demo-proj", billing_dataset=None)
        with pytest.raises(GCPProviderError, match="billing_dataset"):
            prov.fetch_billing_spikes()


# ---------------------------------------------------------------------------
# _rows_to_spikes — the pivot logic
# ---------------------------------------------------------------------------
class TestRowsToSpikes:
    def _row(self, service, window, cost, daily=None):
        return {
            "service": service,
            "window": window,
            "total_cost": cost,
            "daily": daily or [],
        }

    def test_surfaces_services_over_percent_threshold(self):
        rows = [
            self._row("Cloud DNS", "current", 1000.0),
            self._row("Cloud DNS", "previous", 100.0),  # +900% — big
            self._row("Compute Engine", "current", 105.0),
            self._row("Compute Engine", "previous", 100.0),  # +5% — below 20%
        ]
        spikes = GCPProvider._rows_to_spikes(rows, min_change_percent=20, min_absolute_change=1000)
        names = [s.service for s in spikes]
        assert "Cloud DNS" in names
        assert "Compute Engine" not in names

    def test_surfaces_services_over_absolute_threshold(self):
        rows = [
            self._row("Cloud DNS", "current", 1200.0),
            self._row("Cloud DNS", "previous", 1000.0),  # +$200, +20% — absolute passes
        ]
        spikes = GCPProvider._rows_to_spikes(
            rows, min_change_percent=1_000_000, min_absolute_change=100
        )
        assert len(spikes) == 1
        assert spikes[0].service == "Cloud DNS"

    def test_sorted_by_absolute_change_descending(self):
        rows = [
            # Cloud DNS: $100 -> $300  (+$200, +200%)
            self._row("Cloud DNS", "current", 300.0),
            self._row("Cloud DNS", "previous", 100.0),
            # Compute Engine: $500 -> $1500  (+$1000, +200%)
            self._row("Compute Engine", "current", 1500.0),
            self._row("Compute Engine", "previous", 500.0),
        ]
        spikes = GCPProvider._rows_to_spikes(rows, min_change_percent=20, min_absolute_change=0)
        # Larger absolute delta first.
        assert [s.service for s in spikes] == ["Compute Engine", "Cloud DNS"]

    def test_new_service_infinite_percent(self):
        """A service that's entirely new (no previous) shows as +inf%."""
        rows = [
            self._row("Cloud Run", "current", 500.0),
            # no previous row for Cloud Run
        ]
        spikes = GCPProvider._rows_to_spikes(rows, min_change_percent=20, min_absolute_change=0)
        assert len(spikes) == 1
        assert spikes[0].previous_cost == 0.0
        assert spikes[0].change_percent == float("inf")

    def test_flat_service_skipped(self):
        rows = [
            self._row("Cloud DNS", "current", 100.0),
            self._row("Cloud DNS", "previous", 100.0),
        ]
        spikes = GCPProvider._rows_to_spikes(rows, min_change_percent=20, min_absolute_change=100)
        assert spikes == []


# ---------------------------------------------------------------------------
# BaseProvider conformance
# ---------------------------------------------------------------------------
class TestBaseProviderConformance:
    def test_provider_key(self):
        assert GCPProvider.provider_key == "gcp"
        assert GCPProvider(project_id="x").provider_key == "gcp"

    def test_is_base_provider(self):
        assert isinstance(GCPProvider(project_id="x"), BaseProvider)

    def test_cli_tools(self):
        assert GCPProvider(project_id="x").cli_tools() == (
            "gcloud",
            "bq",
            "gsutil",
        )

    def test_env_keep_list_contains_gcp_and_shared(self):
        env = GCPProvider(project_id="x").env_keep_list()
        # Shared shell env vars.
        for key in ("PATH", "HOME", "USER", "LANG", "LC_ALL"):
            assert key in env
        # GCP credential env vars.
        for key in (
            "CLOUDSDK_CONFIG",
            "CLOUDSDK_CORE_PROJECT",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
        ):
            assert key in env
        # No AWS vars.
        assert not any(k.startswith("AWS_") for k in env)

    def test_provider_hint_for_reasoner_mentions_gcp_tooling(self):
        hint = GCPProvider(project_id="x").provider_hint_for_reasoner()
        lower = hint.lower()
        assert "gcloud" in lower
        assert "bq query" in lower
        # Must not leak AWS wording.
        assert "aws ec2" not in lower


# ---------------------------------------------------------------------------
# quote_for_shell
# ---------------------------------------------------------------------------
class TestQuoteForShell:
    @pytest.mark.parametrize(
        "value,should_quote",
        [
            ("simple", False),
            ("has space", True),
            ("dangerous;inject", True),
            ("with'quote", True),
        ],
    )
    def test_quote_for_shell(self, value, should_quote):
        out = GCPProvider.quote_for_shell(value)
        if should_quote:
            assert out != value  # got wrapped
        else:
            assert out == value  # safe as-is
