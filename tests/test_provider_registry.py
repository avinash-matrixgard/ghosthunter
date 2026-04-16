"""Phase 1 dispatch tests for the provider-aware allowlist + validator.

These lock down the refactor that split `security/allowlist.py` into
`allowlist_gcp.py` + `allowlist_aws.py` and made `SecurityValidator`
take a `provider` parameter.

Assertions worth noting:
  * Zero-arg `SecurityValidator()` preserves the GCP-only behavior that
    every existing caller and test depends on.
  * `SecurityValidator(provider="aws")` rejects GCP commands even though
    they pass the Layer-1 blocklist — cross-provider isolation.
  * In Phase 1 the AWS allowlist is empty, so every AWS command is also
    rejected under `provider="aws"`. Phase 2 fills it in.
"""
from __future__ import annotations

import pytest

from ghosthunter.providers.base import (
    BaseProvider,
    CommandResult,
    CostSpike,
    ProviderError,
)
from ghosthunter.providers.gcp import (
    CommandRejectedError as GCP_CommandRejectedError,
    CommandResult as GCP_CommandResult,
    CommandTimeoutError as GCP_CommandTimeoutError,
    CostSpike as GCP_CostSpike,
    GCPProvider,
    GCPProviderError,
)
from ghosthunter.security.allowlist import (
    infer_provider,
    matches_allowlist,
    matches_allowlist_for,
    validate_bq_query,
    validate_query_for,
)
from ghosthunter.security.allowlist_aws import (
    ALLOWED_PATTERNS as AWS_PATTERNS,
    matches_allowlist_aws,
)
from ghosthunter.security.allowlist_gcp import (
    matches_allowlist_gcp,
)
from ghosthunter.security.validator import SecurityValidator


# ---------------------------------------------------------------------------
# Dispatch — infer_provider
# ---------------------------------------------------------------------------
class TestInferProvider:
    @pytest.mark.parametrize(
        "cmd,expected",
        [
            ("gcloud compute instances list", "gcp"),
            ("bq query 'SELECT 1'", "gcp"),
            ("gsutil ls gs://x", "gcp"),
            ("aws ec2 describe-instances", "aws"),
            ("kubectl get pods", None),  # unknown CLI
            ("", None),
            ("   ", None),
        ],
    )
    def test_infer(self, cmd, expected):
        assert infer_provider(cmd) == expected


# ---------------------------------------------------------------------------
# Dispatch — matches_allowlist_for
# ---------------------------------------------------------------------------
class TestAllowlistFor:
    def test_gcp_command_allowed_under_gcp(self):
        assert matches_allowlist_for("gcloud compute instances list", "gcp")

    def test_gcp_command_rejected_under_aws(self):
        # Cross-provider isolation: gcloud asked under aws provider fails
        assert not matches_allowlist_for(
            "gcloud compute instances list", "aws"
        )

    def test_aws_command_rejected_under_gcp(self):
        assert not matches_allowlist_for(
            "aws ec2 describe-instances", "gcp"
        )

    def test_aws_read_command_allowed_under_aws_post_phase2(self):
        # Phase 2: core allowlist populated. Read-shaped aws commands pass
        # under provider=aws. Still rejected under provider=gcp (isolation).
        assert matches_allowlist_for(
            "aws ec2 describe-instances", "aws"
        )
        assert not matches_allowlist_for(
            "aws ec2 describe-instances", "gcp"
        )

    def test_unknown_provider_rejects_everything(self):
        assert not matches_allowlist_for(
            "gcloud compute instances list", "azure"
        )


# ---------------------------------------------------------------------------
# Dispatch — validate_query_for
# ---------------------------------------------------------------------------
class TestValidateQueryFor:
    def test_gcp_bq_select_ok(self):
        ok, _ = validate_query_for("bq query 'SELECT * FROM t'", "gcp")
        assert ok

    def test_gcp_bq_insert_rejected(self):
        ok, reason = validate_query_for(
            "bq query 'INSERT INTO t VALUES(1)'", "gcp"
        )
        assert not ok
        assert "INSERT" in reason

    def test_aws_phase1_no_op(self):
        # Phase 1 AWS validator always returns (True, "") — no semantic
        # checks yet. Phase 3 adds them.
        ok, reason = validate_query_for("aws ec2 describe-instances", "aws")
        assert ok
        assert reason == ""


# ---------------------------------------------------------------------------
# Back-compat — un-parameterized functions route to GCP
# ---------------------------------------------------------------------------
class TestBackCompat:
    def test_matches_allowlist_defaults_to_gcp(self):
        # Existing callers of `matches_allowlist(cmd)` get the GCP ruleset.
        assert matches_allowlist("gcloud compute instances list")
        assert not matches_allowlist("aws ec2 describe-instances")

    def test_validate_bq_query_defaults_to_gcp(self):
        ok, _ = validate_bq_query("bq query 'SELECT 1'")
        assert ok
        ok, reason = validate_bq_query("bq query 'DROP TABLE x'")
        assert not ok
        assert "DROP" in reason


# ---------------------------------------------------------------------------
# SecurityValidator — provider parameter
# ---------------------------------------------------------------------------
class TestValidatorProviderScoping:
    def test_default_is_gcp(self):
        v = SecurityValidator()
        assert v.provider == "gcp"
        # Sanity: a known-good GCP command passes end-to-end.
        assert v.is_allowed("gcloud compute instances list").allowed

    def test_explicit_gcp_allows_gcloud(self):
        v = SecurityValidator(provider="gcp")
        assert v.is_allowed("gcloud run services list").allowed

    def test_explicit_aws_rejects_gcloud(self):
        v = SecurityValidator(provider="aws")
        r = v.is_allowed("gcloud compute instances list")
        assert not r.allowed
        assert r.layer == "L2"  # blocked at allowlist

    def test_explicit_aws_allows_aws_read_post_phase2(self):
        # Phase 2: AWS allowlist populated. Read-shaped aws commands pass.
        v = SecurityValidator(provider="aws")
        assert v.is_allowed("aws ec2 describe-instances").allowed

    def test_explicit_aws_rejects_write_disguised_as_read(self):
        # WRITE_DISGUISED_AS_READ is checked BEFORE the generated read rule.
        v = SecurityValidator(provider="aws")
        assert not v.is_allowed("aws lambda invoke --function-name x").allowed
        assert not v.is_allowed(
            "aws secretsmanager get-secret-value --secret-id x"
        ).allowed


# ---------------------------------------------------------------------------
# BaseProvider — GCPProvider conforms
# ---------------------------------------------------------------------------
class TestGCPInheritsBase:
    def test_gcp_is_base_provider(self):
        p = GCPProvider(project_id="demo-proj")
        assert isinstance(p, BaseProvider)
        assert p.provider_key == "gcp"

    def test_gcp_metadata_methods(self):
        p = GCPProvider(project_id="demo-proj")
        # env_keep_list pins GCP auth vars
        env = p.env_keep_list()
        assert "GOOGLE_APPLICATION_CREDENTIALS" in env
        assert "CLOUDSDK_CORE_PROJECT" in env
        # cli_tools advertises gcloud/bq/gsutil
        assert p.cli_tools() == ("gcloud", "bq", "gsutil")
        # reasoner hint mentions gcloud rules
        assert "gcloud" in p.provider_hint_for_reasoner()


# ---------------------------------------------------------------------------
# Back-compat types — `providers.gcp` re-exports the neutral dataclasses
# ---------------------------------------------------------------------------
class TestTypeReexports:
    def test_gcp_reexports_are_base_types(self):
        # Anyone still importing these from providers.gcp gets the
        # real provider-neutral classes from providers.base.
        assert GCP_CostSpike is CostSpike
        assert GCP_CommandResult is CommandResult
        assert GCP_CommandRejectedError.__base__ is ProviderError or \
            issubclass(GCP_CommandRejectedError, ProviderError)
        assert issubclass(GCP_CommandTimeoutError, ProviderError)

    def test_gcp_provider_error_alias(self):
        # GCPProviderError is kept as an alias for ProviderError so
        # `raise GCPProviderError(...)` keeps working.
        assert GCPProviderError is ProviderError
