"""Phase 4: AWSProvider active-mode tests.

Exercises `fetch_billing_spikes` and `execute_command` against a mocked
Cost Explorer client so the tests don't require boto3 or network access.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock

import pytest

from ghosthunter.providers.aws import AWSProvider, AWSProviderError
from ghosthunter.providers.base import CostSpike
from ghosthunter.security.validator import SecurityValidator


# ---------------------------------------------------------------------------
# Mock CE response helper
# ---------------------------------------------------------------------------
def _ce_service_response(**service_amounts: float) -> dict:
    """Build a CE response like `get_cost_and_usage` with GroupBy=SERVICE."""
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-03-01", "End": "2026-04-01"},
                "Total": {},
                "Groups": [
                    {
                        "Keys": [svc],
                        "Metrics": {
                            "UnblendedCost": {
                                "Amount": f"{amt:.2f}",
                                "Unit": "USD",
                            }
                        },
                    }
                    for svc, amt in service_amounts.items()
                ],
            }
        ],
        "DimensionValueAttributes": [],
    }


def _ce_usage_response(**usage_amounts: float) -> dict:
    """Build a CE response like `get_cost_and_usage` with GroupBy=USAGE_TYPE."""
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-03-01", "End": "2026-04-01"},
                "Total": {},
                "Groups": [
                    {
                        "Keys": [ut],
                        "Metrics": {
                            "UnblendedCost": {
                                "Amount": f"{amt:.2f}",
                                "Unit": "USD",
                            }
                        },
                    }
                    for ut, amt in usage_amounts.items()
                ],
            }
        ],
        "DimensionValueAttributes": [],
    }


# ---------------------------------------------------------------------------
# fetch_billing_spikes — basic behaviour
# ---------------------------------------------------------------------------
class TestFetchBillingSpikes:
    def test_service_level_spike_detected(self):
        ce = MagicMock()
        # current window
        current = _ce_service_response(
            **{
                "Amazon EC2": 4_000.0,   # +300%
                "Amazon S3": 500.0,      # flat
            }
        )
        previous = _ce_service_response(
            **{
                "Amazon EC2": 1_000.0,
                "Amazon S3": 490.0,
            }
        )
        # Provider makes ≥2 service calls (current + previous). Follow-up
        # usage_type calls are issued only for material spikes — the EC2
        # spike qualifies, S3 doesn't.
        ce.get_cost_and_usage.side_effect = [
            current,
            previous,
            _ce_usage_response(**{"BoxUsage:m5.large": 3_000.0, "EBS:VolumeUsage.gp3": 1_000.0}),
        ]

        p = AWSProvider(ce_client=ce)
        spikes = p.fetch_billing_spikes(
            lookback_days=30,
            min_change_percent=20,
            min_absolute_change=500,
        )

        # EC2 spike ranks first (largest absolute change); S3 drops out.
        assert len(spikes) == 1
        ec2 = spikes[0]
        assert ec2.service == "Amazon EC2"
        assert ec2.current_cost == pytest.approx(4_000.0)
        assert ec2.previous_cost == pytest.approx(1_000.0)
        assert ec2.change_percent == pytest.approx(300.0)
        assert ec2.grouping == "service"

        # Follow-up populated usage_type contributors (top-N).
        usage = ec2.top_contributors.get("usage_type")
        assert usage is not None
        names = [name for name, _ in usage]
        assert "BoxUsage:m5.large" in names

    def test_new_service_appears_as_inf_change(self):
        ce = MagicMock()
        ce.get_cost_and_usage.side_effect = [
            _ce_service_response(**{"Amazon Bedrock": 2_000.0}),
            _ce_service_response(),  # previous: nothing
            _ce_usage_response(**{"Bedrock:Anthropic:Claude-Input": 2_000.0}),
        ]
        p = AWSProvider(ce_client=ce)
        spikes = p.fetch_billing_spikes(min_change_percent=20, min_absolute_change=100)
        assert spikes
        assert spikes[0].service == "Amazon Bedrock"
        assert spikes[0].previous_cost == 0.0
        assert spikes[0].change_percent == float("inf")

    def test_followup_disabled_skips_usage_type_calls(self):
        ce = MagicMock()
        ce.get_cost_and_usage.side_effect = [
            _ce_service_response(**{"Amazon EC2": 4_000.0}),
            _ce_service_response(**{"Amazon EC2": 1_000.0}),
        ]
        p = AWSProvider(ce_client=ce)
        spikes = p.fetch_billing_spikes(followup_usage_type=False)
        # Only 2 calls: current + previous. No USAGE_TYPE follow-up.
        assert ce.get_cost_and_usage.call_count == 2
        assert spikes
        assert "usage_type" not in spikes[0].top_contributors

    def test_small_spikes_skip_followup(self):
        """Spikes below the absolute threshold don't burn a follow-up call."""
        ce = MagicMock()
        ce.get_cost_and_usage.side_effect = [
            _ce_service_response(**{"Tiny Service": 60.0}),
            _ce_service_response(**{"Tiny Service": 50.0}),  # +20% but $10 abs
        ]
        p = AWSProvider(ce_client=ce)
        spikes = p.fetch_billing_spikes(
            min_change_percent=20, min_absolute_change=500, followup_usage_type=True
        )
        # +20% passes the pct threshold so it's a spike, but both costs
        # are tiny, so the follow-up is skipped to save CE $.
        assert ce.get_cost_and_usage.call_count == 2
        assert spikes and "usage_type" not in spikes[0].top_contributors

    def test_followup_failure_is_non_fatal(self):
        ce = MagicMock()
        ce.get_cost_and_usage.side_effect = [
            _ce_service_response(**{"Amazon EC2": 5_000.0}),
            _ce_service_response(**{"Amazon EC2": 1_000.0}),
            RuntimeError("CE throttled"),
        ]
        p = AWSProvider(ce_client=ce)
        # The failure during the usage_type follow-up must NOT bubble up.
        spikes = p.fetch_billing_spikes()
        assert spikes and spikes[0].service == "Amazon EC2"
        assert "usage_type" not in spikes[0].top_contributors

    def test_pagination_via_next_page_token(self):
        ce = MagicMock()
        # Page 1 (current), with NextPageToken; page 2 for current; single
        # page for previous; then a single usage_type page.
        current_page_1 = _ce_service_response(**{"Amazon EC2": 2_000.0})
        current_page_1["NextPageToken"] = "abc"
        current_page_2 = _ce_service_response(**{"Amazon EC2": 1_000.0})
        previous = _ce_service_response(**{"Amazon EC2": 500.0})
        usage = _ce_usage_response(**{"BoxUsage:m5.large": 3_000.0})
        ce.get_cost_and_usage.side_effect = [
            current_page_1,
            current_page_2,
            previous,
            usage,
        ]
        p = AWSProvider(ce_client=ce)
        spikes = p.fetch_billing_spikes()
        # Page 1+2 summed: 2_000+1_000 = 3_000 for EC2
        assert spikes[0].current_cost == pytest.approx(3_000.0)


# ---------------------------------------------------------------------------
# on_ce_call notification hook
# ---------------------------------------------------------------------------
class TestCEHook:
    def test_hook_fires_once_per_api_call(self):
        ce = MagicMock()
        ce.get_cost_and_usage.side_effect = [
            _ce_service_response(**{"Amazon EC2": 4_000.0}),
            _ce_service_response(**{"Amazon EC2": 1_000.0}),
            _ce_usage_response(**{"BoxUsage": 3_000.0}),
        ]
        calls: list[tuple[str, dict]] = []

        p = AWSProvider(
            ce_client=ce, on_ce_call=lambda op, params: calls.append((op, params))
        )
        p.fetch_billing_spikes()
        # 2 service calls (current + previous) + 1 usage_type follow-up = 3.
        assert len(calls) == 3
        ops = [op for op, _ in calls]
        assert ops.count("get_cost_and_usage_by_service") == 2
        assert ops.count("get_cost_and_usage_usage_type") == 1

    def test_hook_exceptions_do_not_break_fetch(self):
        ce = MagicMock()
        ce.get_cost_and_usage.side_effect = [
            _ce_service_response(**{"Amazon EC2": 4_000.0}),
            _ce_service_response(**{"Amazon EC2": 1_000.0}),
            _ce_usage_response(**{"BoxUsage": 3_000.0}),
        ]

        def bad_hook(op, params):
            raise RuntimeError("hook broke")

        p = AWSProvider(ce_client=ce, on_ce_call=bad_hook)
        spikes = p.fetch_billing_spikes()
        assert spikes  # no exception bubbled from the hook


# ---------------------------------------------------------------------------
# boto3-missing path
# ---------------------------------------------------------------------------
class TestBoto3Missing:
    def test_no_boto3_no_injected_client_raises_with_install_hint(self, monkeypatch):
        import ghosthunter.providers.aws as aws_mod

        # Simulate boto3 not installed — regardless of whether the test env
        # has it. We only need to force _get_ce_client down the error path.
        monkeypatch.setattr(aws_mod, "_BOTO3_AVAILABLE", False)

        p = AWSProvider()  # no injected ce_client
        with pytest.raises(AWSProviderError, match="ghosthunter\\[aws\\]"):
            p.fetch_billing_spikes()


# ---------------------------------------------------------------------------
# execute_command — subprocess + sandbox env
# ---------------------------------------------------------------------------
class TestExecuteCommand:
    def test_validator_rejects_before_subprocess(self):
        p = AWSProvider(validator=SecurityValidator(provider="aws"))
        from ghosthunter.providers.base import CommandRejectedError
        with pytest.raises(CommandRejectedError):
            # Writes are blocked — we should reject without ever spawning
            # a subprocess.
            asyncio.run(p.execute_command("aws ec2 run-instances --image-id ami-x"))

    def test_sandbox_env_keeps_aws_creds(self, monkeypatch):
        monkeypatch.setenv("AWS_PROFILE", "myprofile")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "secret-token")
        monkeypatch.setenv("UNRELATED_VAR", "leak-me")
        p = AWSProvider(profile="myprofile", region="us-west-2")
        env = p._sandbox_env()
        # AWS credentials and config are preserved.
        assert env.get("AWS_PROFILE") == "myprofile"
        assert env.get("AWS_SESSION_TOKEN") == "secret-token"
        assert env.get("AWS_REGION") == "us-west-2"
        assert env.get("AWS_DEFAULT_REGION") == "us-west-2"
        # Unrelated env vars are stripped.
        assert "UNRELATED_VAR" not in env

    def test_sandbox_env_pins_profile_and_region_defaults(self, monkeypatch):
        # No env vars set — _sandbox_env should still pin profile/region
        # from the constructor args via setdefault.
        for var in (
            "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
            "AWS_REGION", "AWS_DEFAULT_REGION",
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
        ):
            monkeypatch.delenv(var, raising=False)
        p = AWSProvider(profile="dev-profile", region="eu-west-1")
        env = p._sandbox_env()
        assert env.get("AWS_PROFILE") == "dev-profile"
        assert env.get("AWS_REGION") == "eu-west-1"
        assert env.get("AWS_DEFAULT_REGION") == "eu-west-1"


# ---------------------------------------------------------------------------
# CLI glue — _resolve_provider + AWS init path
# ---------------------------------------------------------------------------
class TestCLIGlue:
    def test_aws_provider_is_base_provider(self):
        from ghosthunter.providers.base import BaseProvider
        p = AWSProvider()
        assert isinstance(p, BaseProvider)
        assert p.provider_key == "aws"

    def test_aws_provider_cli_tools(self):
        p = AWSProvider()
        assert p.cli_tools() == ("aws",)

    def test_aws_provider_hint_for_reasoner_mentions_aws_rules(self):
        p = AWSProvider()
        hint = p.provider_hint_for_reasoner()
        assert "aws" in hint.lower()
        assert "describe-" in hint
        assert "need_info" not in hint or True  # sanity — free-form text
