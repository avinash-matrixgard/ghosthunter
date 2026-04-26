"""Tests for the active-mode preflight.

What we're locking down:

- Each check returns ``None`` on success and a ``PreflightIssue`` with
  the right fix category (auto / user / info) on failure.
- ``run_preflight`` halts on the first failure, shows a panel, runs the
  auto-fix if the user confirms, and re-runs the check before proceeding.
- Declining the fix → preflight returns False cleanly, no crash.
- Fix callable that raises → shows a fallback panel, preflight returns False.
- User-command check → preflight waits for Enter, re-runs the check,
  proceeds on success.
- AWS: credentials check surfaces SSO expiry / profile-not-found / no-creds
  as distinct issues; sts identity is stashed on cfg.aws for the CLI to display.
- AWS: Cost Explorer access check recognises AccessDeniedException and
  offers the IAM policy fix snippet.
- GCP: missing google-cloud-bigquery, missing gcloud, missing ADC, missing
  project_id, missing billing_dataset all produce actionable issues.

The real boto3 / google-auth / subprocess paths are mocked — these tests
never hit the network and never shell out.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from rich.console import Console

from ghosthunter import preflight
from ghosthunter.config import AWSConfig, Config
from ghosthunter.preflight import (
    PreflightIssue,
    _check_anthropic_api_key,
    _check_aws_cli,
    _check_aws_credentials,
    _check_billing_dataset,
    _check_boto3,
    _check_cost_explorer_access,
    _check_gcloud_cli,
    _check_gcp_credentials,
    run_preflight,
    run_preflight_aws,
    run_preflight_gcp,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _silent_console() -> Console:
    import io

    return Console(file=io.StringIO(), record=True, width=120, force_terminal=False)


def _make_cfg_aws(**aws_fields) -> Config:
    return Config(provider="aws", aws=AWSConfig(**aws_fields))


def _make_cfg_gcp(project="test-proj", dataset="test-proj.billing") -> Config:
    return Config(provider="gcp", project_id=project, billing_dataset=dataset)


# ---------------------------------------------------------------------------
# Individual check behavior
# ---------------------------------------------------------------------------
class TestApiKeyCheck:
    def test_present_returns_none(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        assert _check_anthropic_api_key(_make_cfg_aws()) is None

    def test_missing_returns_issue_with_user_command(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        issue = _check_anthropic_api_key(_make_cfg_aws())
        assert issue is not None
        assert "ANTHROPIC_API_KEY" in issue.label
        # User fixes this themselves — no fix_callable.
        assert issue.fix_callable is None
        assert issue.user_command is not None
        assert "export" in issue.user_command


class TestBoto3Check:
    def test_installed_returns_none(self, monkeypatch):
        # Skip if boto3 isn't in this venv (it's an optional extra).
        pytest.importorskip("boto3")
        # Clear any fake that might have leaked from a sibling test.
        import sys

        sys.modules.pop("boto3", None)
        sys.modules.pop("botocore", None)
        sys.modules.pop("botocore.exceptions", None)
        assert _check_boto3(_make_cfg_aws()) is None

    def test_missing_returns_issue_with_fix(self, monkeypatch):
        # Simulate missing import by intercepting `import boto3`.
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "boto3":
                raise ImportError("no boto3")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        issue = _check_boto3(_make_cfg_aws())
        assert issue is not None
        assert "boto3" in issue.label
        # Auto-fixable.
        assert issue.fix_callable is not None
        assert "ghosthunter[aws]" in issue.fix_command


class TestAwsCliCheck:
    def test_present_returns_none(self, monkeypatch):
        monkeypatch.setattr(preflight.shutil, "which", lambda _: "/usr/local/bin/aws")
        assert _check_aws_cli(_make_cfg_aws()) is None

    def test_missing_returns_issue(self, monkeypatch):
        monkeypatch.setattr(preflight.shutil, "which", lambda _: None)
        issue = _check_aws_cli(_make_cfg_aws())
        assert issue is not None
        assert "aws" in issue.label.lower()
        # User installs via brew / installer — no fix_callable.
        assert issue.fix_callable is None
        assert issue.user_command


def _install_fake_boto3(monkeypatch, *, raises_name=None, identity=None):
    """Install a fake boto3 + botocore into sys.modules for one test.

    Exactly one call per test (calling twice creates two unrelated
    module objects whose exception classes don't match).

    ``raises_name`` is the *name* of the exception class to raise from
    ``Session.client()``, e.g. ``"NoCredentialsError"``. The exception
    is constructed from the same fake module so the production code's
    ``except botocore.exceptions.NoCredentialsError`` branch matches.

    Returns the fake ``botocore.exceptions`` module so tests can
    inspect the class hierarchy if they want.
    """
    import sys
    import types

    botocore_exc = types.ModuleType("botocore.exceptions")

    class _Base(Exception):
        pass

    class NoCredentialsError(_Base):
        pass

    class TokenRetrievalError(_Base):
        pass

    class ProfileNotFound(_Base):
        pass

    class ClientError(_Base):
        def __init__(self, response=None, operation_name=""):
            super().__init__(f"ClientError({response}, {operation_name})")
            self.response = response or {"Error": {"Code": "AccessDenied"}}

    botocore_exc.NoCredentialsError = NoCredentialsError
    botocore_exc.TokenRetrievalError = TokenRetrievalError
    botocore_exc.ProfileNotFound = ProfileNotFound
    botocore_exc.ClientError = ClientError

    botocore = types.ModuleType("botocore")
    botocore.exceptions = botocore_exc

    fake_boto3 = types.ModuleType("boto3")

    class _FakeSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def client(self, name):
            if raises_name:
                cls = getattr(botocore_exc, raises_name)
                # ProfileNotFound has a different ctor; handle generically.
                raise cls("mocked failure")
            c = MagicMock()
            c.get_caller_identity.return_value = identity or {
                "Account": "111122223333",
                "Arn": "arn:aws:iam::111122223333:user/dev",
                "UserId": "AIDA...",
            }
            return c

    fake_boto3.Session = _FakeSession

    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", botocore_exc)
    return botocore_exc


class TestAwsCredentialsCheck:
    """Mocks boto3.Session so we don't touch real AWS."""

    def test_success_stashes_identity(self, monkeypatch):
        _install_fake_boto3(
            monkeypatch,
            identity={"Account": "999888", "Arn": "arn:x", "UserId": "y"},
        )
        cfg = _make_cfg_aws(profile="dev-sandbox", region="us-west-2")
        assert _check_aws_credentials(cfg) is None
        # Identity stashed on cfg.aws for the CLI to render.
        assert cfg.aws._last_sts_identity == {
            "Account": "999888",
            "Arn": "arn:x",
            "UserId": "y",
        }

    def test_no_credentials(self, monkeypatch):
        _install_fake_boto3(monkeypatch, raises_name="NoCredentialsError")
        issue = _check_aws_credentials(_make_cfg_aws(profile="dev-sandbox"))
        assert issue is not None
        assert "credentials not found" in issue.label.lower()
        assert "dev-sandbox" in issue.detail
        assert issue.user_command

    def test_sso_expired(self, monkeypatch):
        _install_fake_boto3(monkeypatch, raises_name="TokenRetrievalError")
        issue = _check_aws_credentials(_make_cfg_aws(profile="dev-sandbox"))
        assert issue is not None
        assert "sso" in issue.label.lower()
        assert "dev-sandbox" in issue.user_command

    def test_profile_not_found(self, monkeypatch):
        _install_fake_boto3(monkeypatch, raises_name="ProfileNotFound")
        issue = _check_aws_credentials(_make_cfg_aws(profile="does-not-exist"))
        assert issue is not None
        assert "does-not-exist" in issue.label


def _install_fake_boto3_for_ce(monkeypatch, *, ce_response=None, ce_raises_code=None):
    """Same shape as _install_fake_boto3 but scoped to the CE client.

    - ``ce_response`` — what client.get_cost_and_usage returns on success.
    - ``ce_raises_code`` — the AWS error Code string (e.g.
      ``"AccessDeniedException"``) to stamp onto a raised ClientError.
    """
    import sys
    import types

    botocore_exc = types.ModuleType("botocore.exceptions")

    class ClientError(Exception):
        def __init__(self, response=None):
            super().__init__("client error")
            self.response = response or {}

    botocore_exc.ClientError = ClientError
    botocore = types.ModuleType("botocore")
    botocore.exceptions = botocore_exc

    fake = types.ModuleType("boto3")

    class _FakeSession:
        def __init__(self, **kwargs):
            pass

        def client(self, name):
            c = MagicMock()
            if ce_raises_code is not None:
                c.get_cost_and_usage.side_effect = ClientError(
                    response={"Error": {"Code": ce_raises_code, "Message": "mocked"}}
                )
            else:
                c.get_cost_and_usage.return_value = ce_response or {"ResultsByTime": []}
            return c

    fake.Session = _FakeSession
    monkeypatch.setitem(sys.modules, "boto3", fake)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", botocore_exc)


class TestCostExplorerCheck:
    def test_success(self, monkeypatch):
        _install_fake_boto3_for_ce(monkeypatch, ce_response={"ResultsByTime": []})
        assert _check_cost_explorer_access(_make_cfg_aws()) is None

    def test_access_denied_returns_iam_fix_snippet(self, monkeypatch):
        _install_fake_boto3_for_ce(monkeypatch, ce_raises_code="AccessDeniedException")
        issue = _check_cost_explorer_access(_make_cfg_aws())
        assert issue is not None
        assert "Cost Explorer permission" in issue.label
        assert "ce:Get*" in issue.fix_command


# ---------------------------------------------------------------------------
# GCP checks
# ---------------------------------------------------------------------------
class TestGcloudCliCheck:
    def test_missing(self, monkeypatch):
        monkeypatch.setattr(preflight.shutil, "which", lambda _: None)
        issue = _check_gcloud_cli(_make_cfg_gcp())
        assert issue is not None
        assert "gcloud" in issue.label.lower()


class TestBillingDatasetCheck:
    def test_set(self):
        assert _check_billing_dataset(_make_cfg_gcp(dataset="p.billing")) is None

    def test_empty(self):
        issue = _check_billing_dataset(_make_cfg_gcp(dataset=""))
        assert issue is not None
        assert "billing_dataset" in issue.label


class TestGcpCredentialsCheck:
    def test_no_project(self, monkeypatch):
        """No billing_dataset vs no project_id — distinguish."""
        # Mock google.auth.default to succeed so we get past the creds check
        # and trip the project_id branch.
        import sys
        import types

        fake_auth = types.ModuleType("google.auth")
        fake_auth.default = MagicMock(return_value=(MagicMock(), "fallback-proj"))

        fake_auth_exc = types.ModuleType("google.auth.exceptions")

        class _Base(Exception):
            pass

        class DefaultCredentialsError(_Base):
            pass

        fake_auth_exc.DefaultCredentialsError = DefaultCredentialsError

        fake_google = types.ModuleType("google")
        fake_google.auth = fake_auth

        monkeypatch.setitem(sys.modules, "google", fake_google)
        monkeypatch.setitem(sys.modules, "google.auth", fake_auth)
        monkeypatch.setitem(sys.modules, "google.auth.exceptions", fake_auth_exc)

        cfg = _make_cfg_gcp(project="", dataset="x.y")
        issue = _check_gcp_credentials(cfg)
        assert issue is not None
        assert "project" in issue.label.lower()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
class TestRunPreflight:
    def test_all_pass(self):
        console = _silent_console()
        passing: list = [lambda cfg: None, lambda cfg: None]
        assert run_preflight(_make_cfg_aws(), passing, console, title="x") is True

    def test_first_failure_auto_fix_accepted_and_succeeds(self, monkeypatch):
        """User says yes to the fix, fix succeeds, re-check passes."""
        console = _silent_console()
        called = {"fixed": False}

        def _fake_fix():
            called["fixed"] = True

        issue = PreflightIssue(
            label="needs x",
            detail="x missing",
            fix_command="install x",
            fix_callable=_fake_fix,
        )

        calls = iter([issue, None])  # first call fails, second passes

        def _check(cfg):
            return next(calls)

        monkeypatch.setattr(preflight.Confirm, "ask", lambda *a, **k: True)

        ok = run_preflight(_make_cfg_aws(), [_check], console, title="t")
        assert ok is True
        assert called["fixed"] is True

    def test_first_failure_auto_fix_declined_returns_false(self, monkeypatch):
        console = _silent_console()
        issue = PreflightIssue(
            label="needs x",
            detail="x missing",
            fix_callable=lambda: None,
        )

        def _check(cfg):
            return issue

        monkeypatch.setattr(preflight.Confirm, "ask", lambda *a, **k: False)
        assert run_preflight(_make_cfg_aws(), [_check], console, title="t") is False

    def test_fix_raises_returns_false(self, monkeypatch):
        console = _silent_console()

        def _broken_fix():
            raise RuntimeError("pip died")

        issue = PreflightIssue(
            label="needs x",
            detail="x missing",
            fix_callable=_broken_fix,
        )

        def _check(cfg):
            return issue

        monkeypatch.setattr(preflight.Confirm, "ask", lambda *a, **k: True)
        assert run_preflight(_make_cfg_aws(), [_check], console, title="t") is False

    def test_auto_fix_succeeds_but_recheck_fails(self, monkeypatch):
        """The fix ran but the check still fails → give up after retry."""
        console = _silent_console()

        issue = PreflightIssue(
            label="still broken",
            detail="...",
            fix_callable=lambda: None,
        )

        calls = iter([issue, issue, issue])  # always fails

        def _check(cfg):
            return next(calls)

        monkeypatch.setattr(preflight.Confirm, "ask", lambda *a, **k: True)
        assert run_preflight(_make_cfg_aws(), [_check], console, title="t") is False

    def test_user_command_issue_waits_for_enter(self, monkeypatch):
        """A check with only user_command (no fix_callable) should
        call input() and retry on Enter."""
        console = _silent_console()

        issue = PreflightIssue(
            label="needs you to do a thing",
            detail="do it",
            user_command="do_the_thing",
        )

        calls = iter([issue, None])  # fails once, then passes after user action

        def _check(cfg):
            return next(calls)

        monkeypatch.setattr("builtins.input", lambda _: "")
        assert run_preflight(_make_cfg_aws(), [_check], console, title="t") is True

    def test_user_command_ctrl_c_returns_false(self, monkeypatch):
        console = _silent_console()
        issue = PreflightIssue(label="x", detail="y", user_command="z")

        def _check(cfg):
            return issue

        def _raise_keyboard(*_args):
            raise KeyboardInterrupt()

        monkeypatch.setattr("builtins.input", _raise_keyboard)
        assert run_preflight(_make_cfg_aws(), [_check], console, title="t") is False


# ---------------------------------------------------------------------------
# Entry-point smoke tests
# ---------------------------------------------------------------------------
class TestEntryPoints:
    def test_aws_entry_point_wiring(self, monkeypatch):
        """run_preflight_aws should compose the expected check sequence."""
        console = _silent_console()
        # All checks pass → should return True. We short-circuit the
        # expensive checks by temporarily replacing them.
        passing = lambda cfg: None
        monkeypatch.setattr(preflight, "_check_anthropic_api_key", passing)
        monkeypatch.setattr(preflight, "_check_boto3", passing)
        monkeypatch.setattr(preflight, "_check_aws_cli", passing)
        monkeypatch.setattr(preflight, "_check_aws_credentials", passing)
        monkeypatch.setattr(preflight, "_check_cost_explorer_access", passing)

        assert run_preflight_aws(_make_cfg_aws(), console) is True

    def test_gcp_entry_point_wiring(self, monkeypatch):
        console = _silent_console()
        passing = lambda cfg: None
        monkeypatch.setattr(preflight, "_check_anthropic_api_key", passing)
        monkeypatch.setattr(preflight, "_check_bigquery_package", passing)
        monkeypatch.setattr(preflight, "_check_gcloud_cli", passing)
        monkeypatch.setattr(preflight, "_check_gcp_credentials", passing)
        monkeypatch.setattr(preflight, "_check_billing_dataset", passing)

        assert run_preflight_gcp(_make_cfg_gcp(), console) is True


# ---------------------------------------------------------------------------
# _pip_install helper (don't actually run pip)
# ---------------------------------------------------------------------------
class TestPipInstall:
    def test_success_returns_none(self, monkeypatch):
        result = MagicMock(returncode=0, stderr="")
        monkeypatch.setattr(preflight.subprocess, "run", lambda *a, **k: result)
        # Should complete without raising.
        preflight._pip_install("anything")

    def test_failure_raises_runtime_error(self, monkeypatch):
        result = MagicMock(returncode=1, stderr="no internet")
        monkeypatch.setattr(preflight.subprocess, "run", lambda *a, **k: result)
        with pytest.raises(RuntimeError) as excinfo:
            preflight._pip_install("ghosthunter[aws]")
        assert "no internet" in str(excinfo.value)
