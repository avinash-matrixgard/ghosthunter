"""AWS provider: billing fetch (Phase 4) + sandboxed command execution.

Mirrors the shape of ``providers.gcp.GCPProvider`` so the investigator
doesn't care which cloud it's running against.

Phase 2 ships:
    - ``execute_command()``  — subprocess exec of allowlisted ``aws`` CLI
                               commands, with an AWS-scoped sandbox env
                               (profile, region, access keys, session token).
    - Metadata methods        — ``env_keep_list``, ``cli_tools``,
                               ``billing_template_help``,
                               ``provider_hint_for_reasoner``.

Phase 4 will fill ``fetch_billing_spikes()`` with a boto3 Cost Explorer
implementation. Until then the method raises a clear NotImplementedError
pointing at advisor mode.
"""

from __future__ import annotations

import asyncio
import shlex
from datetime import date, timedelta
from typing import Any, ClassVar

from ghosthunter.providers.base import (
    BaseProvider,
    CommandRejectedError,
    CommandResult,
    CommandTimeoutError,
    CostSpike,
    ProviderError,
)
from ghosthunter.security.validator import SecurityValidator

# Optional import: boto3 is only needed for active mode. Advisor-mode
# AWS users should be able to run Ghosthunter without boto3 installed.
try:
    import boto3  # type: ignore

    _BOTO3_AVAILABLE = True
except ImportError:
    _BOTO3_AVAILABLE = False


# ---------------------------------------------------------------------------
# Back-compat alias pattern mirroring GCPProviderError.
# ---------------------------------------------------------------------------
AWSProviderError = ProviderError


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class AWSProvider(BaseProvider):
    """Read-only AWS access for the investigator.

    Parameters
    ----------
    profile:
        AWS named profile to use (``--profile`` flag / ``AWS_PROFILE`` env).
        Empty string = use default credential chain.
    region:
        AWS region to pin when the command doesn't specify one.
        Default ``us-east-1`` (Cost Explorer's home region).
    account_id:
        Optional 12-digit AWS account id. Informational only in Phase 2;
        used by Phase 4 to scope Cost Explorer queries.
    validator:
        SecurityValidator instance. Defaults to a fresh AWS-scoped one.
    command_timeout:
        Per-command wall-clock cap in seconds. Default 120.
    max_output_bytes:
        Per-command stdout cap. Output beyond this is truncated.
    """

    provider_key: ClassVar[str] = "aws"

    def __init__(
        self,
        profile: str = "",
        region: str = "us-east-1",
        account_id: str = "",
        validator: SecurityValidator | None = None,
        command_timeout: int = 120,
        max_output_bytes: int = 1_000_000,  # 1 MB
        ce_client: Any | None = None,
        on_ce_call: Any | None = None,
    ) -> None:
        self.profile = profile
        self.region = region
        self.account_id = account_id
        self.validator = validator or SecurityValidator(provider="aws")
        self.command_timeout = command_timeout
        self.max_output_bytes = max_output_bytes
        # Injected boto3 client for tests. Production path lazily constructs
        # one inside fetch_billing_spikes so boto3 import stays optional.
        self._ce_client = ce_client
        # Optional callback fired before each CE API call. Used by the CLI
        # to render the cost banner and bump an audit counter. Callback
        # signature: (operation_name: str, params: dict) -> None
        self.on_ce_call = on_ce_call

    # ------------------------------------------------------------------
    # Billing fetch — boto3 Cost Explorer.
    # ------------------------------------------------------------------
    def fetch_billing_spikes(
        self,
        lookback_days: int = 30,
        min_change_percent: float = 20.0,
        min_absolute_change: float = 100.0,
        followup_usage_type: bool = True,
    ) -> list[CostSpike]:
        """Query Cost Explorer for SERVICE-level spikes over a window.

        Compares the most recent ``lookback_days`` against the prior
        equal-length window and returns services whose cost moved
        materially by either percent or absolute-dollar thresholds.

        When ``followup_usage_type`` is True, for each spike whose
        ``previous_cost`` exceeds ``min_absolute_change`` we make a
        second CE call scoped to that service grouped by USAGE_TYPE,
        and attach the results as ``top_contributors["usage_type"]``.

        Cost note: each CE call is billed at ~$0.01. A typical run
        makes 2 + (small-N) calls. The CLI prints a one-line banner
        on first use and persists the acknowledgment in config so the
        prompt doesn't show again.
        """
        ce = self._get_ce_client()
        today = date.today()
        current_end = today
        current_start = today - timedelta(days=lookback_days)
        previous_end = current_start
        previous_start = previous_end - timedelta(days=lookback_days)

        # CE expects YYYY-MM-DD strings, and End is *exclusive*.
        current = self._ce_service_totals(ce, current_start, current_end)
        previous = self._ce_service_totals(ce, previous_start, previous_end)

        spikes: list[CostSpike] = []
        for service in sorted(set(current) | set(previous)):
            cur = current.get(service, 0.0)
            prev = previous.get(service, 0.0)
            if prev > 0:
                pct = ((cur - prev) / prev) * 100.0
            else:
                pct = float("inf") if cur > 0 else 0.0
            absolute = cur - prev
            material = abs(pct) >= min_change_percent or abs(absolute) >= min_absolute_change
            if not material:
                continue
            spikes.append(
                CostSpike(
                    service=service,
                    current_cost=cur,
                    previous_cost=prev,
                    change_percent=pct,
                    grouping="service",
                    daily_breakdown=[],
                )
            )

        spikes.sort(key=lambda s: abs(s.absolute_change), reverse=True)

        # Optional: for each material spike, break down by USAGE_TYPE for
        # the CURRENT window. Skipped for tiny spikes so we don't burn CE
        # calls ($0.01 each) on noise.
        if followup_usage_type:
            for spike in spikes:
                if (
                    spike.previous_cost < min_absolute_change
                    and spike.current_cost < min_absolute_change * 2
                ):
                    continue
                try:
                    breakdown = self._ce_usage_type_for_service(
                        ce, spike.service, current_start, current_end
                    )
                except Exception:
                    # Follow-up failures are non-fatal — we already have
                    # the primary spike data. Keep the primary; drop the
                    # contributor detail.
                    continue
                if breakdown:
                    spike.top_contributors["usage_type"] = breakdown

        return spikes

    # ------------------------------------------------------------------
    def _get_ce_client(self) -> Any:
        """Return the boto3 CE client, constructing one lazily if needed."""
        if self._ce_client is not None:
            return self._ce_client
        if not _BOTO3_AVAILABLE:
            raise AWSProviderError(
                "boto3 is not installed; install it to use AWS active mode:\n"
                "  pip install 'ghosthunter[aws]'   # or: pip install boto3\n"
                "Advisor mode (a billing-file CSV / JSON) works without boto3 —\n"
                "see `ghosthunter billing-template --provider=aws`."
            )
        session_kwargs: dict[str, Any] = {}
        if self.profile:
            session_kwargs["profile_name"] = self.profile
        if self.region:
            session_kwargs["region_name"] = self.region
        session = boto3.Session(**session_kwargs)
        # Cost Explorer lives in us-east-1 regardless of where the
        # account's resources are. Pin it so callers can pass a different
        # default region (us-west-2 etc.) without confusing CE.
        return session.client("ce", region_name="us-east-1")

    def _ce_service_totals(self, ce: Any, start: date, end: date) -> dict[str, float]:
        """Return {service_name: unblended_cost} aggregated over the window."""
        self._notify_ce_call(
            "get_cost_and_usage_by_service",
            {"start": start.isoformat(), "end": end.isoformat()},
        )
        totals: dict[str, float] = {}
        params: dict[str, Any] = {
            "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
            "Granularity": "MONTHLY",
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
        }
        response = ce.get_cost_and_usage(**params)
        while True:
            for bucket in response.get("ResultsByTime") or []:
                for group in bucket.get("Groups") or []:
                    keys = group.get("Keys") or []
                    if not keys:
                        continue
                    service = keys[0]
                    metrics = group.get("Metrics") or {}
                    amt_str = metrics.get("UnblendedCost", {}).get("Amount") or "0"
                    try:
                        amt = float(amt_str)
                    except (TypeError, ValueError):
                        amt = 0.0
                    totals[service] = totals.get(service, 0.0) + amt
            next_token = response.get("NextPageToken")
            if not next_token:
                break
            response = ce.get_cost_and_usage(**{**params, "NextPageToken": next_token})
        return totals

    def _ce_usage_type_for_service(
        self, ce: Any, service: str, start: date, end: date, limit: int = 8
    ) -> list[tuple[str, float]]:
        """Return top-N (UsageType, cost) pairs for a service over a window."""
        self._notify_ce_call(
            "get_cost_and_usage_usage_type",
            {
                "service": service,
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        params: dict[str, Any] = {
            "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
            "Granularity": "MONTHLY",
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            "Filter": {
                "Dimensions": {"Key": "SERVICE", "Values": [service]},
            },
        }
        response = ce.get_cost_and_usage(**params)
        totals: dict[str, float] = {}
        while True:
            for bucket in response.get("ResultsByTime") or []:
                for group in bucket.get("Groups") or []:
                    keys = group.get("Keys") or []
                    if not keys:
                        continue
                    usage = keys[0]
                    metrics = group.get("Metrics") or {}
                    amt_str = metrics.get("UnblendedCost", {}).get("Amount") or "0"
                    try:
                        amt = float(amt_str)
                    except (TypeError, ValueError):
                        amt = 0.0
                    totals[usage] = totals.get(usage, 0.0) + amt
            next_token = response.get("NextPageToken")
            if not next_token:
                break
            response = ce.get_cost_and_usage(**{**params, "NextPageToken": next_token})
        ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        return ranked[:limit]

    def _notify_ce_call(self, operation: str, params: dict[str, Any]) -> None:
        """Fire the on_ce_call hook if one is registered."""
        if self.on_ce_call is not None:
            try:
                self.on_ce_call(operation, params)
            except Exception:
                # Hook errors must never mask billing-fetch errors.
                pass

    # ------------------------------------------------------------------
    # Command execution — mirrors GCPProvider.execute_command exactly,
    # just with an AWS-scoped sandbox env.
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

    # ------------------------------------------------------------------
    # BaseProvider metadata
    # ------------------------------------------------------------------
    def env_keep_list(self) -> set[str]:
        return {
            "PATH",
            "HOME",
            "USER",
            "LANG",
            "LC_ALL",
            # AWS auth — support every supported credential model:
            "AWS_PROFILE",
            "AWS_DEFAULT_PROFILE",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_CONFIG_FILE",
            "AWS_SHARED_CREDENTIALS_FILE",
            "AWS_ENDPOINT_URL",
            "AWS_CA_BUNDLE",
            # SSO
            "AWS_SSO_SESSION",
        }

    def cli_tools(self) -> tuple[str, ...]:
        return ("aws",)

    def billing_template_help(self) -> str:
        return AWS_BILLING_TEMPLATE

    def provider_hint_for_reasoner(self) -> str:
        return AWS_REASONER_RULES

    # ------------------------------------------------------------------
    def _sandbox_env(self) -> dict[str, str]:
        """Minimal env for subprocess. Inherits PATH and AWS credentials
        but strips anything else that could leak data or alter behavior.
        """
        import os

        keep = self.env_keep_list()
        env = {k: v for k, v in os.environ.items() if k in keep}
        # Pin profile and region as defaults for commands that don't
        # specify them. Explicit --profile/--region on the command wins.
        if self.profile:
            env.setdefault("AWS_PROFILE", self.profile)
        if self.region:
            env.setdefault("AWS_REGION", self.region)
            env.setdefault("AWS_DEFAULT_REGION", self.region)
        return env

    # ------------------------------------------------------------------
    @staticmethod
    def quote_for_shell(value: str) -> str:
        return shlex.quote(value)


# ---------------------------------------------------------------------------
# Billing-template text shown by `ghosthunter billing-template`.
# ---------------------------------------------------------------------------
AWS_BILLING_TEMPLATE = """[bold cyan]Option A — Cost Explorer CSV download (simplest)[/bold cyan]
Fastest path. From the Cost Explorer UI, export a CSV grouped by Service
(and optionally UsageType) over the window you want to investigate.

  1. Go to [cyan]https://console.aws.amazon.com/cost-management/home#/cost-explorer[/cyan]
  2. Pick your time range (covering the spike)
  3. Group by [bold]Service[/bold] → Download CSV → save as by-service.csv
  4. Change Group by → [bold]Usage Type[/bold] → Download → save as by-usage-type.csv
  5. (optional) Change Group by → [bold]Linked Account[/bold] → Download → save as by-account.csv

Then: [bold]ghosthunter investigate --provider=aws \\
         -f by-service.csv -f by-usage-type.csv -f by-account.csv[/bold]

[bold cyan]Option B — `aws ce get-cost-and-usage` JSON (scriptable)[/bold cyan]
If you have AWS CLI configured with Cost-Explorer read access:

  [dim]aws ce get-cost-and-usage \\
    --time-period Start=2026-01-01,End=2026-04-01 \\
    --granularity DAILY \\
    --metrics UnblendedCost \\
    --group-by Type=DIMENSION,Key=SERVICE \\
    > by-service.json

  aws ce get-cost-and-usage \\
    --time-period Start=2026-01-01,End=2026-04-01 \\
    --granularity DAILY \\
    --metrics UnblendedCost \\
    --group-by Type=DIMENSION,Key=USAGE_TYPE \\
    > by-usage-type.json[/dim]

Then: [bold]ghosthunter investigate --provider=aws \\
         -f by-service.json -f by-usage-type.json[/bold]

[bold cyan]Option C — CUR (Cost and Usage Report) CSV from S3 (richest)[/bold cyan]
If you have a CUR configured and exporting to S3:

  1. Download the latest CUR CSV from your S3 bucket
     (filename typically `<report-name>-<YYYYMMDD>-<YYYYMMDD>.csv`)
  2. Pass it directly — Ghosthunter recognizes `lineItem/*` columns

Then: [bold]ghosthunter investigate --provider=aws -f cur-export.csv[/bold]

[dim]CUR Parquet files aren't supported in v1 — ask AWS to also export CSV,
or convert with a local tool.[/dim]

[bold cyan]Option D — FOCUS (cross-cloud, FinOps Foundation spec)[/bold cyan]
If your exports are in the [cyan]FinOps Open Cost & Usage Specification[/cyan]
(FOCUS 1.0+) format — the emerging cross-cloud standard from the FinOps
Foundation — Ghosthunter recognizes those columns directly
([cyan]ServiceName[/cyan], [cyan]BilledCost[/cyan], [cyan]ChargePeriodStart[/cyan],
[cyan]SubAccountId[/cyan], [cyan]RegionName[/cyan], ...). Works with exports from
AWS, Azure, GCP, and other FOCUS-compliant vendors.

Public sample data you can try today:
  [cyan]https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS-Sample-Data[/cyan]

[dim]Recognized AWS columns (case-insensitive, multiple aliases per field):[/dim]
  • [bold]service[/bold]     (required) — Service / lineItem/ProductCode /
                                           product/ProductName
  • [bold]cost[/bold]        (required) — UnblendedCost / BlendedCost /
                                           lineItem/UnblendedCost / Amount
  • [bold]date[/bold]        (optional) — Start / TimePeriodStart /
                                           lineItem/UsageStartDate
  • [bold]usage_type[/bold]  (optional) — UsageType / lineItem/UsageType
  • [bold]account[/bold]     (optional) — Linked Account / lineItem/UsageAccountId
  • [bold]location[/bold]    (optional) — Region / product/region

Extra dimensions don't change spike detection — they sharpen the
hypotheses Opus forms by showing WHERE inside a service the cost moved.
"""


# ---------------------------------------------------------------------------
# Reasoner prompt fragment (Phase 5 expands this into the full AWS block).
# For Phase 2 we ship a minimal but honest set of rules so Opus doesn't
# hallucinate gcloud syntax when running against AWS.
# ---------------------------------------------------------------------------
AWS_REASONER_RULES = """## COMMAND RULES (AWS — NON-NEGOTIABLE — security layers will block violations)

1. ONE command per turn. NEVER chain with `&&`, `;`, or `||`.
2. Only safe pipes: `head`, `tail`, `wc`, `sort`, `uniq`, `grep`, `cut`,
   `tr`, `jq`. Anything else gets blocked.
3. CLI is `aws` only. Read-only verbs: `describe-*`, `list-*`, `get-*`.
   FORBIDDEN verbs (blocked by security layer):
     create-, delete-, update-, modify-, put-, start-, run-, terminate-,
     stop-, attach-, detach-, associate-, disassociate-, invoke,
     publish, send-message, decrypt, encrypt.
4. Never `aws lambda invoke`, `aws sns publish`, `aws sqs send-message`,
   `aws athena start-query-execution`, `aws stepfunctions start-execution`,
   `aws ssm send-command|start-session`, `aws secretsmanager get-secret-value`,
   `aws ssm get-parameter --with-decryption`, `aws kms decrypt|encrypt`.
   These look like reads but cause writes / leak secrets.
5. Cost Explorer (`aws ce ...`) commands cost ~$0.01 each. Prefer the
   billing data already provided in the initial prompt. Only call `aws ce`
   when you need a dimension the billing file doesn't have.
6. NO REDIRECTS. Do NOT use `>`, `>>`, `<`, or `2>&1`. Errors surface
   in the command result anyway.
7. Prefer `--output json` + `jq` over `--query` when you need structured
   extraction. `--query` is fine for simple projections.
8. Region scoping: AWS commands are region-scoped. If the billing data
   shows multi-region spend, pin `--region` explicitly. Use
   `aws ec2 describe-regions` first if you don't know the regions in use.
9. CE dimension keys are UPPER_SNAKE_CASE: `SERVICE`, `USAGE_TYPE`,
   `LINKED_ACCOUNT`, `REGION`, `INSTANCE_TYPE`.
10. `aws athena start-query-execution` is BLOCKED. Tell the user to run
    Athena queries in the Athena console themselves and paste results back.
"""


__all__ = [
    "AWSProvider",
    "AWSProviderError",
    "AWS_BILLING_TEMPLATE",
    "AWS_REASONER_RULES",
]
