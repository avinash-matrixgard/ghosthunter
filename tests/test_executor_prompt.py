"""Regression test for the Layer-6 Sonnet validator being hardcoded to GCP.

Real bug: in a live AWS advisor-mode investigation, Sonnet rejected a
legitimate `aws ce get-cost-and-usage` with:

    ✗ blocked (L6): This is an AWS CLI command (aws ce get-cost-and-usage),
    not a GCP command. This system is scoped to reviewing read-only GCP
    commands only.

Root cause: `executor.py` had hardcoded "read-only GCP commands" /
"production GCP project" in the Sonnet system prompt, even though the
static validator (Layer 2) was provider-scoped and would have routed
the command through the AWS allowlist just fine.

Fix: mirror the Phase 5 reasoner-prompt split — core text +
per-provider notes — and thread the `provider` arg through every
Executor() construction site.

This file locks down that:
  - The GCP prompt mentions gcloud/bq/gsutil and NOT AWS CLI tooling.
  - The AWS prompt mentions `aws`, `describe-*`/`list-*`/`get-*`, and
    the specific non-read-shaped reads that otherwise look suspicious
    (ce get-cost-and-usage, cloudtrail lookup-events, logs
    filter-log-events, sts get-caller-identity, dynamodb scan, etc).
  - Neither prompt bleeds into the other.
  - `Executor(provider="aws")` actually sends the AWS-flavored prompt
    to the Anthropic client at call time (not just stores it).
  - Back-compat alias SEMANTIC_VALIDATION_SYSTEM still equals the GCP
    prompt for pre-split import sites.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from ghosthunter.models.executor import (
    COMPRESSION_SYSTEM,
    SEMANTIC_VALIDATION_SYSTEM,
    Executor,
    build_compression_prompt,
    build_semantic_validation_prompt,
)


# ---------------------------------------------------------------------------
# Prompt composition — shape of the provider-specific text
# ---------------------------------------------------------------------------
class TestSemanticPrompt:
    def test_gcp_prompt_names_gcp_tooling(self):
        p = build_semantic_validation_prompt("gcp")
        low = p.lower()
        assert "gcp" in low
        assert "gcloud" in low
        assert "bq" in low
        assert "gsutil" in low
        # Must NOT leak AWS-specific wording.
        assert "aws ce" not in low
        assert "describe-*" not in p

    def test_aws_prompt_names_aws_tooling(self):
        p = build_semantic_validation_prompt("aws")
        low = p.lower()
        assert "aws" in low
        # AWS read-verb vocabulary present.
        assert "describe-*" in p
        assert "list-*" in p
        assert "get-*" in p
        # Specific non-`describe-/list-/get-` reads that tripped the old
        # prompt should be called out as legitimate.
        assert "ce get-cost-and-usage" in low
        assert "cloudtrail lookup-events" in low
        assert "filter-log-events" in low
        # AWS block must NOT leak GCP tooling language.
        assert "gcloud" not in low
        assert "bq query" not in low
        assert "gsutil" not in low

    def test_core_rules_survive_provider_split(self):
        """Both prompts must still carry the 'approve unless ...' core."""
        for prov in ("gcp", "aws"):
            p = build_semantic_validation_prompt(prov)
            assert "LAST CHECK" in p
            assert "semantic_check" in p
            assert "Bias toward APPROVE" in p

    def test_unknown_provider_falls_back_to_core_only(self):
        """Unknown provider strings → no leaked provider wording."""
        p = build_semantic_validation_prompt("azure")
        low = p.lower()
        assert "gcloud" not in low
        assert "describe-*" not in p
        assert "LAST CHECK" in p  # core text still present

    def test_back_compat_alias_points_at_gcp(self):
        assert SEMANTIC_VALIDATION_SYSTEM == build_semantic_validation_prompt("gcp")


class TestCompressionPrompt:
    def test_gcp_and_aws_compression_prompts_are_cloud_neutral(self):
        gcp = build_compression_prompt("gcp")
        aws = build_compression_prompt("aws")
        # Old prompt said "raw GCP command output"; regression: that's gone.
        assert "GCP" not in gcp
        assert "GCP" not in aws
        assert "cloud command output" in gcp.lower()
        assert "cloud command output" in aws.lower()

    def test_back_compat_alias_equals_gcp(self):
        assert COMPRESSION_SYSTEM == build_compression_prompt("gcp")


# ---------------------------------------------------------------------------
# Executor wiring — provider selects the right prompt AT CALL TIME
# ---------------------------------------------------------------------------
class TestExecutorWiresProviderPrompt:
    def test_default_is_gcp(self):
        e = Executor(client=MagicMock())
        assert e.provider == "gcp"
        assert e.semantic_system == build_semantic_validation_prompt("gcp")

    def test_aws_executor_uses_aws_prompt(self):
        e = Executor(client=MagicMock(), provider="aws")
        assert e.provider == "aws"
        assert e.semantic_system == build_semantic_validation_prompt("aws")
        assert "describe-*" in e.semantic_system
        assert "gcloud" not in e.semantic_system.lower()

    @pytest.mark.parametrize("provider", ["gcp", "aws"])
    def test_semantic_validate_sends_right_system_prompt(self, provider):
        """Mock the Anthropic client; confirm Executor sends the right system."""
        client = MagicMock()

        class _Block:
            type = "tool_use"
            name = "semantic_check"
            input = {"approved": True, "reason": "ok"}

        class _Response:
            content = [_Block()]

        async def _fake_create(**kwargs):
            _fake_create.captured = kwargs
            return _Response()

        client.messages.create = _fake_create

        e = Executor(client=client, provider=provider)
        asyncio.run(e.semantic_validate("aws ec2 describe-instances"))

        sent_system = _fake_create.captured["system"]
        assert sent_system == build_semantic_validation_prompt(provider)
        if provider == "aws":
            assert "ce get-cost-and-usage" in sent_system.lower()
        else:
            assert "gcloud" in sent_system.lower()

    def test_compress_uses_provider_scoped_system(self):
        """Compression is cloud-neutral today; still threaded through per-provider attr."""
        client = MagicMock()

        class _Txt:
            type = "text"
            text = "- fact 1\n- fact 2"

        class _Response:
            content = [_Txt()]

        async def _fake_create(**kwargs):
            _fake_create.captured = kwargs
            return _Response()

        client.messages.create = _fake_create

        e = Executor(client=client, provider="aws")
        asyncio.run(
            e.compress(
                command="aws ce get-cost-and-usage",
                output="[{}]",
                investigation_target="AWS Lambda spike",
                hypotheses=["H1: payload enricher"],
            )
        )
        assert _fake_create.captured["system"] == build_compression_prompt("aws")
        # Regression: old prompt leaked "raw GCP command output".
        assert "GCP" not in _fake_create.captured["system"]
