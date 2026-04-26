"""Preflight checks for active-mode investigations.

Turns "crash with a stack trace" into "panel + prompt". Runs BEFORE
any cloud API call, so users with missing deps / bad creds / wrong
profile find out immediately with a clear message, not after Opus
has already burned tokens.

Each provider has its own entry point (``run_preflight_aws``,
``run_preflight_gcp``). They execute a sequence of small ``Check``
functions; the first failure halts the sequence and shows an
actionable panel. Checks fall into three categories:

- **Auto-fixable** — we can run ``pip install`` / similar. Prompt
  "Install now? [Y/n]" and re-run the check on yes.
- **User-fixable** — the user must run a command (``aws sso login``,
  ``export ANTHROPIC_API_KEY=…``). We show the command and wait for
  them to press Enter, then re-run the check.
- **Informational** — something we can't fix (CE rate-limit, Anthropic
  outage). Show context + "Continue anyway? Abort?".

The module has zero hard deps on boto3 / google-cloud-bigquery: every
import of those SDKs is inside a check so advisor-mode installs
without the cloud SDKs still run the core CLI cleanly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

from ghosthunter.config import Config


# ---------------------------------------------------------------------------
# Issue type + categories
# ---------------------------------------------------------------------------
@dataclass
class PreflightIssue:
    """A preflight check failed; carries enough info to offer a fix."""

    label: str
    """Short headline, e.g. 'boto3 not installed'."""

    detail: str
    """One or two sentences explaining what's wrong + why it matters."""

    fix_command: str | None = None
    """Shell command the user would run to fix (shown in the panel,
    even when ``fix_callable`` is present — so the user knows what
    'Install now' means)."""

    fix_callable: Callable[[], None] | None = None
    """Zero-arg callable that performs the fix. If present, the panel
    offers 'Install now? [Y/n]' and invokes this on yes. Raises on
    fix failure so the caller can show the second-chance panel."""

    user_command: str | None = None
    """Command the user must run themselves. Shown verbatim in a dim
    footer. After the user runs it, they press Enter to re-check."""

    docs_url: str | None = None
    """Optional link to more context (troubleshooting section, IAM
    policy snippet, etc.)."""


Check = Callable[[Config], PreflightIssue | None]
"""A preflight check: takes config, returns None on success or an issue."""


# ---------------------------------------------------------------------------
# Public orchestrator
# ---------------------------------------------------------------------------
def run_preflight(
    cfg: Config,
    checks: list[Check],
    console: Console,
    *,
    title: str,
) -> bool:
    """Run each check in order. Return True if all pass.

    On the first failure, show a Rich panel with the issue. If the
    issue has a ``fix_callable``, prompt to run it and retry the check
    (once). If it has a ``user_command``, wait for the user to press
    Enter then retry. If neither, return False.

    The orchestrator prints a one-line success summary after every
    passing check so the user can see progress.
    """
    console.print()
    console.print(Panel(title, border_style="cyan", expand=False))

    for check in checks:
        issue = check(cfg)
        for _attempt in range(2):  # initial run + one retry after fix
            if issue is None:
                break
            _render_issue(console, issue)
            if not _prompt_fix_or_wait(console, issue):
                return False
            # Re-run the check to confirm the fix stuck.
            issue = check(cfg)
        else:
            # Two attempts both failed → surrender.
            console.print(
                "[red]Preflight check still failing after retry. "
                "Run the command above and try again.[/red]"
            )
            return False

        console.print(f"  [green]✓[/green] {_check_name(check)}")

    console.print("\n[green]Preflight OK — proceeding.[/green]\n")
    return True


def _check_name(check: Check) -> str:
    """Pretty label for a check function, used in the success line."""
    name = getattr(check, "__name__", "") or "check"
    return name.removeprefix("_check_").replace("_", " ")


def _render_issue(console: Console, issue: PreflightIssue) -> None:
    body_lines = [f"[bold]{issue.label}[/bold]", "", issue.detail]
    if issue.fix_command:
        body_lines.extend(["", "[dim]Fix:[/dim]", f"  [cyan]{issue.fix_command}[/cyan]"])
    if issue.user_command:
        body_lines.extend(
            [
                "",
                "[dim]Run this in another terminal, then press Enter:[/dim]",
                f"  [cyan]{issue.user_command}[/cyan]",
            ]
        )
    if issue.docs_url:
        body_lines.extend(["", f"[dim]Docs:[/dim] {issue.docs_url}"])

    console.print(
        Panel(
            "\n".join(body_lines),
            title="[yellow]Preflight — issue[/yellow]",
            border_style="yellow",
            expand=False,
        )
    )


def _prompt_fix_or_wait(console: Console, issue: PreflightIssue) -> bool:
    """Handle the issue based on which fix category it's in.

    Returns ``True`` if the caller should retry the check, ``False``
    if the caller should give up (user declined fix, fix raised, etc).
    """
    if issue.fix_callable is not None:
        if not Confirm.ask("[bold]Install now?[/bold]", default=True, console=console):
            console.print("[dim]OK, leaving it to you. Install and re-run.[/dim]")
            return False
        try:
            issue.fix_callable()
        except Exception as exc:  # noqa: BLE001 — we surface any failure
            console.print(f"[red]Fix failed: {exc}[/red]")
            if issue.fix_command:
                console.print(f"[dim]Run manually: [cyan]{issue.fix_command}[/cyan][/dim]")
            return False
        return True

    if issue.user_command is not None:
        console.print("[dim](waiting for you to run it…)[/dim]")
        try:
            input("Press Enter when done, or Ctrl+C to abort: ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return False
        return True

    # Informational / unrecoverable.
    console.print("[red]No automatic fix available for this issue. See panel above.[/red]")
    return False


# ---------------------------------------------------------------------------
# Shared checks
# ---------------------------------------------------------------------------
def _check_anthropic_api_key(_cfg: Config) -> PreflightIssue | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return None
    return PreflightIssue(
        label="ANTHROPIC_API_KEY is not set",
        detail=(
            "Ghosthunter needs an Anthropic API key to call Claude Opus "
            "(reasoning) and Sonnet (compression). Key is read from the "
            "environment at runtime — never persisted to disk."
        ),
        user_command="export ANTHROPIC_API_KEY=sk-ant-...",
        docs_url="https://console.anthropic.com/settings/keys",
    )


# ---------------------------------------------------------------------------
# AWS-specific checks
# ---------------------------------------------------------------------------
def _check_boto3(_cfg: Config) -> PreflightIssue | None:
    try:
        import boto3  # noqa: F401
    except ImportError:
        return PreflightIssue(
            label="boto3 is not installed",
            detail=(
                "AWS active mode uses boto3 to call the Cost Explorer "
                "API for billing-spike detection. Advisor mode (CSV / "
                "JSON paste) works without it."
            ),
            fix_command="pip install 'ghosthunter[aws]'",
            fix_callable=lambda: _pip_install("ghosthunter[aws]"),
        )
    return None


def _check_aws_cli(_cfg: Config) -> PreflightIssue | None:
    if shutil.which("aws") is not None:
        return None
    return PreflightIssue(
        label="`aws` CLI not found on PATH",
        detail=(
            "Ghosthunter shells out to the `aws` CLI to run the "
            "commands Opus proposes during an investigation. Install "
            "it from the AWS CLI v2 installer and confirm with "
            "`aws --version`."
        ),
        user_command="brew install awscli",
        docs_url="https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html",
    )


def _check_aws_credentials(cfg: Config) -> PreflightIssue | None:
    """Confirm we can reach AWS with the configured profile + get an
    identity back. Uses boto3 sts:GetCallerIdentity — cheap, no
    Cost Explorer charge."""
    try:
        import boto3
        import botocore.exceptions
    except ImportError:
        # Caught earlier by _check_boto3; defer.
        return None

    aws_cfg = cfg.aws
    profile = (aws_cfg.profile if aws_cfg else None) or os.environ.get("AWS_PROFILE") or ""
    region = (aws_cfg.region if aws_cfg else None) or os.environ.get("AWS_REGION") or "us-east-1"

    session_kwargs: dict = {}
    if profile:
        session_kwargs["profile_name"] = profile
    if region:
        session_kwargs["region_name"] = region

    try:
        session = boto3.Session(**session_kwargs)
        sts = session.client("sts")
        identity = sts.get_caller_identity()
    except botocore.exceptions.NoCredentialsError:
        return PreflightIssue(
            label="AWS credentials not found",
            detail=(
                "boto3 couldn't locate credentials for "
                f"profile={profile or '(default chain)'}. "
                "Configure via `aws configure` for static keys or "
                "`aws sso login --profile <name>` for SSO."
            ),
            user_command=f"aws sso login --profile {profile or '<your-profile>'}",
            docs_url="https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html",
        )
    except botocore.exceptions.TokenRetrievalError:
        return PreflightIssue(
            label="AWS SSO token expired or missing",
            detail=(
                f"profile {profile!r} uses SSO and the cached token "
                "is stale. Re-authenticate and try again."
            ),
            user_command=f"aws sso login --profile {profile}" if profile else "aws sso login",
        )
    except botocore.exceptions.ProfileNotFound:
        return PreflightIssue(
            label=f"AWS profile {profile!r} not found",
            detail=(
                "The profile configured in ~/.ghosthunter/config.toml "
                "doesn't exist in ~/.aws/config. Fix the profile or "
                "re-run `ghosthunter init` to point at a different one."
            ),
            user_command="cat ~/.aws/config",
        )
    except botocore.exceptions.ClientError as exc:
        return PreflightIssue(
            label="sts:GetCallerIdentity failed",
            detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 — catch-all for surprising SDK errors
        return PreflightIssue(
            label="Unexpected error reaching AWS",
            detail=str(exc),
        )

    # Success — stash identity on cfg for the caller to display. Not
    # strictly a check output; we use an attribute on cfg.aws so the
    # CLI can render it after preflight passes.
    if cfg.aws is None:
        return None
    cfg.aws._last_sts_identity = {  # type: ignore[attr-defined]
        "Account": identity.get("Account"),
        "Arn": identity.get("Arn"),
        "UserId": identity.get("UserId"),
    }
    return None


def _check_cost_explorer_access(cfg: Config) -> PreflightIssue | None:
    """Try a single tiny Cost Explorer call to confirm ce:GetCostAndUsage
    permission. Uses a one-day window to minimize cost (~$0.01)."""
    try:
        import boto3
        import botocore.exceptions
    except ImportError:
        return None

    from datetime import date, timedelta

    aws_cfg = cfg.aws
    profile = (aws_cfg.profile if aws_cfg else None) or os.environ.get("AWS_PROFILE") or ""

    session_kwargs: dict = {}
    if profile:
        session_kwargs["profile_name"] = profile
    # CE lives in us-east-1 regardless of where the account's resources are.
    session_kwargs["region_name"] = "us-east-1"

    try:
        session = boto3.Session(**session_kwargs)
        ce = session.client("ce")
        end = date.today()
        start = end - timedelta(days=1)
        ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
        )
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("AccessDeniedException", "UnauthorizedOperation"):
            return PreflightIssue(
                label="AWS profile lacks Cost Explorer permission",
                detail=(
                    "ce:GetCostAndUsage is required for active mode. "
                    "Attach the policy below to the user / role in question."
                ),
                fix_command=(
                    "aws iam put-user-policy --user-name <USER> "
                    "--policy-name GhosthunterCostExplorer "
                    '--policy-document \'{"Version":"2012-10-17",'
                    '"Statement":[{"Effect":"Allow",'
                    '"Action":["ce:Get*","ce:List*","ce:Describe*"],'
                    '"Resource":"*"}]}\''
                ),
                docs_url="https://docs.aws.amazon.com/cost-management/latest/userguide/ce-access.html",
            )
        return PreflightIssue(
            label="Cost Explorer call failed",
            detail=f"{code or 'unknown error'}: {exc}",
        )
    except Exception as exc:  # noqa: BLE001
        return PreflightIssue(
            label="Unexpected error calling Cost Explorer",
            detail=str(exc),
        )
    return None


# ---------------------------------------------------------------------------
# GCP-specific checks
# ---------------------------------------------------------------------------
def _check_bigquery_package(_cfg: Config) -> PreflightIssue | None:
    try:
        from google.cloud import bigquery  # noqa: F401
    except ImportError:
        return PreflightIssue(
            label="google-cloud-bigquery is not installed",
            detail=(
                "GCP active mode uses the BigQuery client to read your "
                "billing export. Advisor mode (CSV paste) works without "
                "it."
            ),
            fix_command="pip install 'ghosthunter[gcp]'",
            fix_callable=lambda: _pip_install("ghosthunter[gcp]"),
        )
    return None


def _check_gcloud_cli(_cfg: Config) -> PreflightIssue | None:
    if shutil.which("gcloud") is not None:
        return None
    return PreflightIssue(
        label="`gcloud` CLI not found on PATH",
        detail=(
            "Ghosthunter shells out to gcloud / bq / gsutil during an "
            "investigation. Install the Google Cloud SDK."
        ),
        user_command="brew install --cask google-cloud-sdk",
        docs_url="https://cloud.google.com/sdk/docs/install",
    )


def _check_gcp_credentials(cfg: Config) -> PreflightIssue | None:
    """Confirm application-default credentials or service-account key
    are available and the project is set."""
    try:
        import google.auth
        import google.auth.exceptions
    except ImportError:
        return None

    try:
        credentials, project = google.auth.default()
    except google.auth.exceptions.DefaultCredentialsError:
        return PreflightIssue(
            label="GCP application-default credentials not found",
            detail=(
                "Set GOOGLE_APPLICATION_CREDENTIALS to a service-account "
                "JSON key, or run `gcloud auth application-default login`."
            ),
            user_command="gcloud auth application-default login",
            docs_url="https://cloud.google.com/docs/authentication/application-default-credentials",
        )
    except Exception as exc:  # noqa: BLE001
        return PreflightIssue(
            label="Unexpected error loading GCP credentials",
            detail=str(exc),
        )

    if not cfg.project_id:
        return PreflightIssue(
            label="GCP project not configured",
            detail=(
                "~/.ghosthunter/config.toml has no project_id. Run `ghosthunter init` to set one."
            ),
            user_command="ghosthunter init",
        )
    return None


def _check_billing_dataset(cfg: Config) -> PreflightIssue | None:
    if cfg.billing_dataset:
        return None
    return PreflightIssue(
        label="GCP billing_dataset not configured",
        detail=(
            "Active mode queries a BigQuery dataset that mirrors your "
            "billing export. Set it to something like "
            "'<project>.billing_export' — same one you see in the "
            "BigQuery console under Billing Export."
        ),
        user_command="ghosthunter init",
        docs_url="https://cloud.google.com/billing/docs/how-to/export-data-bigquery-setup",
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def run_preflight_aws(cfg: Config, console: Console) -> bool:
    """Full AWS active-mode preflight. Returns True when safe to proceed."""
    return run_preflight(
        cfg,
        checks=[
            _check_anthropic_api_key,
            _check_boto3,
            _check_aws_cli,
            _check_aws_credentials,
            _check_cost_explorer_access,
        ],
        console=console,
        title="[bold]AWS active mode — preflight[/bold]",
    )


def run_preflight_gcp(cfg: Config, console: Console) -> bool:
    """Full GCP active-mode preflight. Returns True when safe to proceed."""
    return run_preflight(
        cfg,
        checks=[
            _check_anthropic_api_key,
            _check_bigquery_package,
            _check_gcloud_cli,
            _check_gcp_credentials,
            _check_billing_dataset,
        ],
        console=console,
        title="[bold]GCP active mode — preflight[/bold]",
    )


# ---------------------------------------------------------------------------
# Fix helper
# ---------------------------------------------------------------------------
def _pip_install(spec: str) -> None:
    """Install a pip spec into the current Python (same venv as Ghosthunter).

    Runs synchronously so the caller can re-check immediately after.
    Raises ``RuntimeError`` on non-zero exit, with stderr attached.
    """
    cmd = [sys.executable, "-m", "pip", "install", spec]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"pip install {spec!r} failed (exit {result.returncode}):\n{result.stderr.strip()}"
        )


__all__ = [
    "PreflightIssue",
    "run_preflight",
    "run_preflight_aws",
    "run_preflight_gcp",
]
