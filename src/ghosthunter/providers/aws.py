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
from typing import ClassVar

from ghosthunter.providers.base import (
    BaseProvider,
    CommandRejectedError,
    CommandResult,
    CommandTimeoutError,
    CostSpike,
    ProviderError,
)
from ghosthunter.security.validator import SecurityValidator


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
    ) -> None:
        self.profile = profile
        self.region = region
        self.account_id = account_id
        self.validator = validator or SecurityValidator(provider="aws")
        self.command_timeout = command_timeout
        self.max_output_bytes = max_output_bytes

    # ------------------------------------------------------------------
    # Billing fetch — Phase 4 populates this with boto3 Cost Explorer.
    # ------------------------------------------------------------------
    def fetch_billing_spikes(
        self,
        lookback_days: int = 30,
        min_change_percent: float = 20.0,
        min_absolute_change: float = 100.0,
    ) -> list[CostSpike]:
        raise AWSProviderError(
            "AWS active-mode billing fetch lands in Phase 4. "
            "For now, export a Cost Explorer CSV (or run "
            "`aws ce get-cost-and-usage`) and pass it with -f. "
            "See `ghosthunter billing-template --provider=aws`."
        )

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
   `awk`, `tr`, `jq`. Anything else gets blocked.
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
