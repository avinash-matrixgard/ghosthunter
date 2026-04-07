"""GCP provider: billing fetch + sandboxed command execution.

Two distinct paths:

1. `fetch_billing_spikes()` — uses the BigQuery Python client directly to
   pull billing export data. This is the investigator's *setup* phase and
   does NOT go through the security validator (it's structured Python, not
   a shell command).

2. `execute_command()` — runs gcloud/bq/gsutil shell commands that Opus
   proposes during investigation. Every command MUST already be approved
   by `SecurityValidator.is_allowed()` before reaching this function. The
   provider re-validates as defense in depth.
"""
from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ghosthunter.security.validator import SecurityValidator

# Optional import: BigQuery client is heavy and not needed for demo mode.
try:
    from google.cloud import bigquery  # type: ignore
    _BQ_AVAILABLE = True
except ImportError:
    _BQ_AVAILABLE = False


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class CostSpike:
    """A service whose cost changed materially over the lookback window."""
    service: str
    current_cost: float
    previous_cost: float
    change_percent: float
    daily_breakdown: list[dict[str, Any]] = field(default_factory=list)

    @property
    def absolute_change(self) -> float:
        return self.current_cost - self.previous_cost


@dataclass
class CommandResult:
    """Result of executing a shell command in the sandbox."""
    command: str
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    truncated: bool = False

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class GCPProviderError(Exception):
    """Base error for the GCP provider."""


class CommandRejectedError(GCPProviderError):
    """Raised when a command fails security validation at execution time."""


class CommandTimeoutError(GCPProviderError):
    """Raised when a command exceeds its timeout."""


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class GCPProvider:
    """Read-only GCP access for the investigator.

    Parameters
    ----------
    project_id:
        The GCP project to investigate.
    billing_dataset:
        Fully qualified billing export dataset, e.g. ``my-proj.billing_export``.
        Required for `fetch_billing_spikes` but unused by `execute_command`.
    validator:
        SecurityValidator instance. Defaults to a fresh one.
    command_timeout:
        Per-command wall-clock cap in seconds. Default 120.
    max_output_bytes:
        Per-command stdout cap. Output beyond this is truncated and the
        ``truncated`` flag is set.
    """

    def __init__(
        self,
        project_id: str,
        billing_dataset: str | None = None,
        validator: SecurityValidator | None = None,
        command_timeout: int = 120,
        max_output_bytes: int = 1_000_000,  # 1 MB
    ) -> None:
        self.project_id = project_id
        self.billing_dataset = billing_dataset
        self.validator = validator or SecurityValidator()
        self.command_timeout = command_timeout
        self.max_output_bytes = max_output_bytes

    # ------------------------------------------------------------------
    # Billing fetch (BigQuery client — NOT a shell command)
    # ------------------------------------------------------------------
    def fetch_billing_spikes(
        self,
        lookback_days: int = 30,
        min_change_percent: float = 20.0,
        min_absolute_change: float = 100.0,
    ) -> list[CostSpike]:
        """Fetch billing data and return services with material cost changes.

        Compares the most recent ``lookback_days`` against the prior window
        of equal length. A spike is reported if EITHER the percent change
        OR the absolute change exceeds its threshold.
        """
        if not _BQ_AVAILABLE:
            raise GCPProviderError(
                "google-cloud-bigquery is not installed; cannot fetch billing"
            )
        if not self.billing_dataset:
            raise GCPProviderError(
                "billing_dataset is required for fetch_billing_spikes"
            )

        client = bigquery.Client(project=self.project_id)

        today = date.today()
        current_start = today - timedelta(days=lookback_days)
        previous_start = current_start - timedelta(days=lookback_days)

        query = f"""
            WITH windowed AS (
              SELECT
                service.description AS service,
                CASE
                  WHEN DATE(usage_start_time) >= @current_start
                    THEN 'current'
                  WHEN DATE(usage_start_time) >= @previous_start
                    THEN 'previous'
                END AS window,
                cost,
                DATE(usage_start_time) AS day
              FROM `{self.billing_dataset}.gcp_billing_export_v1_*`
              WHERE DATE(usage_start_time) >= @previous_start
            )
            SELECT
              service,
              window,
              SUM(cost) AS total_cost,
              ARRAY_AGG(STRUCT(day, cost) ORDER BY day) AS daily
            FROM windowed
            WHERE window IS NOT NULL
            GROUP BY service, window
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(
                    "current_start", "DATE", current_start
                ),
                bigquery.ScalarQueryParameter(
                    "previous_start", "DATE", previous_start
                ),
            ]
        )

        rows = list(client.query(query, job_config=job_config).result())
        return self._rows_to_spikes(
            rows, min_change_percent, min_absolute_change
        )

    @staticmethod
    def _rows_to_spikes(
        rows: list[Any],
        min_change_percent: float,
        min_absolute_change: float,
    ) -> list[CostSpike]:
        """Pivot the (service, window) rows into CostSpike objects."""
        by_service: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = by_service.setdefault(
                row["service"], {"current": 0.0, "previous": 0.0, "daily": []}
            )
            entry[row["window"]] = float(row["total_cost"])
            if row["window"] == "current":
                entry["daily"] = [
                    {"day": str(d["day"]), "cost": float(d["cost"])}
                    for d in row["daily"]
                ]

        spikes: list[CostSpike] = []
        for service, data in by_service.items():
            current = data["current"]
            previous = data["previous"]
            if previous > 0:
                pct = ((current - previous) / previous) * 100.0
            else:
                pct = float("inf") if current > 0 else 0.0
            absolute = current - previous

            material = (
                abs(pct) >= min_change_percent
                or abs(absolute) >= min_absolute_change
            )
            if not material:
                continue

            spikes.append(
                CostSpike(
                    service=service,
                    current_cost=current,
                    previous_cost=previous,
                    change_percent=pct,
                    daily_breakdown=data["daily"],
                )
            )

        spikes.sort(key=lambda s: abs(s.absolute_change), reverse=True)
        return spikes

    # ------------------------------------------------------------------
    # Command execution (shell — every command goes through validator)
    # ------------------------------------------------------------------
    async def execute_command(self, command: str) -> CommandResult:
        """Execute a single shell command in the sandbox.

        Re-validates the command before execution as defense in depth.
        Wraps subprocess execution with a timeout and output cap.
        """
        result = self.validator.is_allowed(command)
        if not result.allowed:
            raise CommandRejectedError(
                f"command rejected at execution ({result.layer}): {result.reason}"
            )

        loop = asyncio.get_event_loop()
        start = loop.time()

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._sandbox_env(),
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self.command_timeout
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise CommandTimeoutError(
                f"command exceeded {self.command_timeout}s: {command}"
            ) from exc

        duration = loop.time() - start

        truncated = False
        if len(stdout_b) > self.max_output_bytes:
            stdout_b = stdout_b[: self.max_output_bytes]
            truncated = True

        return CommandResult(
            command=command,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            exit_code=proc.returncode if proc.returncode is not None else -1,
            duration_seconds=duration,
            truncated=truncated,
        )

    def _sandbox_env(self) -> dict[str, str]:
        """Minimal env for subprocess. Inherits PATH and gcloud credentials
        but strips anything else that could leak data or alter behavior.
        """
        import os

        keep = {
            "PATH",
            "HOME",
            "USER",
            "LANG",
            "LC_ALL",
            # gcloud / GCP auth
            "CLOUDSDK_CONFIG",
            "CLOUDSDK_CORE_PROJECT",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
        }
        env = {k: v for k, v in os.environ.items() if k in keep}
        # Pin the project so commands without --project still target the right one
        env.setdefault("CLOUDSDK_CORE_PROJECT", self.project_id)
        return env

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def quote_for_shell(value: str) -> str:
        """Convenience for callers building command strings safely."""
        return shlex.quote(value)
