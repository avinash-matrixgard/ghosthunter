"""Phase 3: exhaustive AWS allowlist coverage.

This file locks down the full AWS catalog:
  - Every entry in ALLOWED_PATTERNS passes the validator.
  - Every entry in WRITE_DISGUISED_AS_READ is rejected.
  - Read commands across 40+ AWS services (caught by BASE_READ_RULE).
  - 200-command fuzz: synthetic `aws {service} {write-verb}-*` must all fail.
  - Targeted regressions for the semantic rules (SSM --with-decryption,
    athena start- vs get-query-results, secretsmanager read vs write).

Complements `test_security_aws.py` (which covers the Phase 2 core only).
"""
from __future__ import annotations

import random
import re

import pytest

from ghosthunter.security.allowlist_aws import (
    ALLOWED_PATTERNS,
    BASE_READ_RULE,
    WRITE_DISGUISED_AS_READ,
    matches_allowlist_aws,
    validate_query_aws,
)
from ghosthunter.security.validator import SecurityValidator


@pytest.fixture
def aws_validator():
    return SecurityValidator(provider="aws")


# ---------------------------------------------------------------------------
# Every explicit ALLOWED_PATTERNS entry allows its canonical form.
# ---------------------------------------------------------------------------
def _pattern_to_probe(pattern: str) -> str:
    """Turn a regex pattern into a realistic probe command.

    The patterns are of the form ``^aws\\s+SVC\\s+VERB\\b...``. We strip
    the regex noise and append a generic ``--region us-east-1`` so the
    Layer-1 length / encoding checks don't flag anything.
    """
    if pattern == BASE_READ_RULE:
        return "aws ec2 describe-instances"
    s = pattern
    s = s.replace(r"^aws\s+", "aws ")
    s = s.replace(r"\s+", " ")
    s = s.replace(r"\b", "")
    s = re.sub(r"[()?*+|]", "", s)
    # Many patterns end with .* for trailing flags — strip those.
    s = s.replace(".*", "").strip()
    return s + " --region us-east-1"


class TestEveryAllowedPattern:
    @pytest.mark.parametrize("pattern", ALLOWED_PATTERNS)
    def test_pattern_matches_its_probe(self, pattern):
        probe = _pattern_to_probe(pattern)
        assert matches_allowlist_aws(probe), (
            f"pattern {pattern!r} should match its probe {probe!r}"
        )


# ---------------------------------------------------------------------------
# Every WRITE_DISGUISED_AS_READ entry rejects its canonical form.
# ---------------------------------------------------------------------------
class TestEveryBlocklistEntry:
    @pytest.mark.parametrize("pattern", WRITE_DISGUISED_AS_READ)
    def test_entry_rejects_its_probe(self, pattern):
        probe = _pattern_to_probe(pattern)
        assert not matches_allowlist_aws(probe), (
            f"pattern {pattern!r} should REJECT its probe {probe!r}"
        )


# ---------------------------------------------------------------------------
# Broad service coverage — read commands across the catalog.
# Each of these should pass via either an explicit rule or BASE_READ_RULE.
# ---------------------------------------------------------------------------
CATALOG_READS = [
    # Compute
    "aws ec2 describe-instances",
    "aws ec2 describe-volumes",
    "aws ec2 describe-snapshots",
    "aws ec2 describe-nat-gateways",
    "aws ec2 describe-vpc-endpoints",
    "aws ec2 describe-transit-gateways",
    "aws ec2 describe-regions",
    "aws ec2 describe-availability-zones",
    "aws ec2 describe-spot-instance-requests",
    "aws ec2 describe-spot-fleet-requests",
    "aws ec2 describe-reserved-instances",
    "aws ec2 describe-capacity-reservations",
    "aws ec2 describe-images --owners amazon",
    "aws ec2 describe-addresses",
    "aws ec2 describe-key-pairs",
    # Containers
    "aws ecs list-clusters",
    "aws ecs describe-clusters",
    "aws ecs describe-services",
    "aws ecs list-tasks",
    "aws eks list-clusters",
    "aws eks describe-cluster --name prod",
    "aws eks list-nodegroups --cluster-name prod",
    "aws ecr describe-repositories",
    "aws ecr list-images --repository-name foo",
    # Storage
    "aws s3 ls",
    "aws s3api list-buckets",
    "aws s3api get-bucket-location --bucket x",
    "aws s3api get-bucket-lifecycle-configuration --bucket x",
    "aws s3api get-bucket-versioning --bucket x",
    "aws s3api get-bucket-policy --bucket x",
    "aws s3api list-objects-v2 --bucket x",
    "aws s3api get-object-tagging --bucket x --key y",
    "aws efs describe-file-systems",
    "aws efs describe-mount-targets",
    "aws fsx describe-file-systems",
    "aws backup list-backup-vaults",
    # Databases
    "aws rds describe-db-instances",
    "aws rds describe-db-clusters",
    "aws rds describe-db-snapshots",
    "aws rds describe-reserved-db-instances",
    "aws dynamodb list-tables",
    "aws dynamodb describe-table --table-name foo",
    "aws dynamodb scan --table-name foo --max-items 10",
    "aws dynamodb query --table-name foo --key-condition-expression '#k = :v'",
    "aws dynamodb batch-get-item --request-items file://req.json",
    "aws elasticache describe-cache-clusters",
    "aws memorydb describe-clusters",
    "aws docdb describe-db-clusters",
    "aws neptune describe-db-clusters",
    "aws timestream-write describe-database --database-name foo",
    # Networking / edge
    "aws elbv2 describe-load-balancers",
    "aws elbv2 describe-target-groups",
    "aws elbv2 describe-target-health --target-group-arn arn:x",
    "aws elb describe-load-balancers",
    "aws apigateway get-rest-apis",
    "aws apigatewayv2 get-apis",
    "aws cloudfront list-distributions",
    "aws cloudfront get-distribution-config --id X",
    "aws route53 list-hosted-zones",
    "aws route53 list-resource-record-sets --hosted-zone-id Z",
    "aws route53 test-dns-answer --hosted-zone-id Z --record-name x --record-type A",
    "aws vpc-lattice list-services",
    # Serverless / integrations
    "aws lambda list-functions",
    "aws lambda get-function --function-name foo",
    "aws lambda list-function-url-configs --function-name foo",
    "aws apigateway get-resources --rest-api-id x",
    "aws stepfunctions list-state-machines",
    "aws stepfunctions describe-state-machine --state-machine-arn arn:sm",
    "aws stepfunctions list-executions --state-machine-arn arn:sm",
    "aws eventbridge list-rules",
    # Messaging / streaming
    "aws sns list-topics",
    "aws sns get-topic-attributes --topic-arn arn:t",
    "aws sqs list-queues",
    "aws sqs get-queue-attributes --queue-url q",
    "aws kinesis list-streams",
    "aws kinesis describe-stream --stream-name s",
    "aws kinesisanalyticsv2 list-applications",
    "aws msk list-clusters-v2",
    "aws msk describe-cluster-v2 --cluster-arn arn:k",
    # Analytics
    "aws athena list-data-catalogs",
    "aws athena get-query-results --query-execution-id x",
    "aws athena batch-get-query-execution --query-execution-ids x",
    "aws glue get-databases",
    "aws glue get-tables --database-name db",
    "aws glue get-jobs",
    "aws glue list-jobs",
    "aws redshift describe-clusters",
    "aws redshift-data describe-statement --id x",
    "aws opensearch list-domain-names",
    "aws opensearch describe-domain --domain-name x",
    "aws emr list-clusters",
    "aws emr-containers list-virtual-clusters",
    "aws dms describe-replication-tasks",
    "aws quicksight list-dashboards --aws-account-id 111122223333",
    # ML
    "aws sagemaker list-endpoints",
    "aws sagemaker describe-endpoint --endpoint-name x",
    "aws sagemaker list-training-jobs",
    "aws sagemaker list-models",
    "aws bedrock list-foundation-models",
    "aws bedrock get-foundation-model --model-identifier x",
    # Observability
    "aws cloudwatch list-metrics",
    "aws cloudwatch get-metric-statistics --namespace AWS/EC2 --metric-name CPU",
    "aws cloudwatch describe-alarms",
    "aws cloudwatch get-dashboard --dashboard-name x",
    "aws logs describe-log-groups",
    "aws logs filter-log-events --log-group-name x",
    "aws logs tail /aws/lambda/foo",
    "aws logs get-log-events --log-group-name x --log-stream-name y",
    "aws logs get-query-results --query-id x",
    "aws xray get-trace-summaries --start-time 2026-01-01 --end-time 2026-01-02",
    "aws synthetics describe-canaries",
    # IAM / SSO / Organizations
    "aws iam list-users",
    "aws iam list-roles",
    "aws iam list-policies",
    "aws iam get-role --role-name x",
    "aws iam get-account-summary",
    "aws iam simulate-principal-policy --policy-source-arn arn:x --action-names s3:GetObject",
    "aws iam simulate-custom-policy --policy-input-list 'x' --action-names s3:GetObject",
    "aws organizations describe-organization",
    "aws organizations list-accounts",
    "aws organizations describe-account --account-id 111122223333",
    "aws identitystore list-users --identity-store-id x",
    "aws sso-admin list-instances",
    # Governance / security
    "aws config describe-configuration-recorders",
    "aws config list-discovered-resources --resource-type AWS::EC2::Instance",
    "aws config get-resource-config-history --resource-type AWS::EC2::Instance --resource-id i-x",
    "aws cloudtrail list-trails",
    "aws cloudtrail lookup-events --max-results 10",
    "aws securityhub get-findings",
    "aws guardduty list-detectors",
    "aws guardduty list-findings --detector-id x",
    "aws wafv2 list-web-acls --scope REGIONAL",
    "aws waf list-rules",
    # Misc
    "aws sts get-caller-identity",
    "aws sts decode-authorization-message --encoded-message foo",
    "aws resourcegroupstaggingapi get-resources --tag-filters Key=env,Values=prod",
    "aws ce get-cost-and-usage --time-period Start=2026-01-01,End=2026-02-01 --metrics UnblendedCost",
    "aws ce get-dimension-values --dimension SERVICE",
    "aws ce get-rightsizing-recommendation --service AmazonEC2",
    "aws ce get-savings-plans-coverage",
    "aws budgets describe-budgets --account-id 111122223333",
    "aws cur describe-report-definitions",
    "aws pricing get-products --service-code AmazonEC2",
    "aws compute-optimizer get-ec2-instance-recommendations",
    "aws compute-optimizer get-lambda-function-recommendations",
    "aws support describe-trusted-advisor-checks --language en",
    "aws cloudformation describe-stacks",
    "aws cloudformation list-stacks",
    "aws cloudformation validate-template --template-body file://x.yaml",
    "aws cloudformation detect-stack-drift --stack-name s",
    "aws codebuild list-projects",
    "aws codebuild batch-get-projects --names p",
    "aws codebuild list-builds",
    "aws codedeploy list-applications",
    "aws codepipeline list-pipelines",
    "aws codepipeline get-pipeline --name p",
    "aws appsync list-apis",
    "aws appsync get-graphql-api --api-id a",
    "aws ssm describe-instance-information",
    "aws ssm get-parameter --name /prod/db/host",
    "aws secretsmanager list-secrets",
    "aws secretsmanager describe-secret --secret-id x",
    "aws kms list-keys",
    "aws kms describe-key --key-id alias/x",
    "aws acm list-certificates",
    "aws acm-pca list-certificate-authorities",
]


class TestBroadCatalogReads:
    @pytest.mark.parametrize("cmd", CATALOG_READS)
    def test_read_allowed(self, aws_validator, cmd):
        r = aws_validator.is_allowed(cmd)
        assert r.allowed, (
            f"{cmd!r} should be allowed; layer={r.layer} reason={r.reason!r}"
        )


# ---------------------------------------------------------------------------
# Explicit targeted regressions.
# ---------------------------------------------------------------------------
class TestTargetedRegressions:
    # --- Athena: start-query-execution blocked, get-query-results allowed ---
    def test_athena_start_blocked(self, aws_validator):
        assert not aws_validator.is_allowed(
            "aws athena start-query-execution --query-string 'SELECT 1'"
        ).allowed

    def test_athena_get_query_results_allowed(self, aws_validator):
        assert aws_validator.is_allowed(
            "aws athena get-query-results --query-execution-id x"
        ).allowed

    # --- CloudWatch Logs Insights: start-query blocked, get-query-results allowed ---
    def test_logs_start_query_blocked(self, aws_validator):
        assert not aws_validator.is_allowed(
            "aws logs start-query --log-group-name x --query-string 'fields @message'"
        ).allowed

    def test_logs_get_query_results_allowed(self, aws_validator):
        assert aws_validator.is_allowed(
            "aws logs get-query-results --query-id x"
        ).allowed

    # --- SSM: get-parameter allowed only without --with-decryption ---
    @pytest.mark.parametrize(
        "cmd",
        [
            "aws ssm get-parameter --name /x --with-decryption",
            "aws ssm get-parameters --names /x --with-decryption",
            "aws ssm get-parameters-by-path --path / --with-decryption",
            "aws ssm get-parameters-by-path --path / --with-decryption --recursive",
        ],
    )
    def test_ssm_with_decryption_rejected(self, aws_validator, cmd):
        r = aws_validator.is_allowed(cmd)
        assert not r.allowed
        assert r.layer == "L4"

    def test_ssm_without_decryption_allowed(self, aws_validator):
        assert aws_validator.is_allowed(
            "aws ssm get-parameter --name /prod/db/host"
        ).allowed

    # --- Secrets Manager: metadata allowed, value blocked ---
    def test_secretsmanager_describe_allowed(self, aws_validator):
        assert aws_validator.is_allowed(
            "aws secretsmanager describe-secret --secret-id x"
        ).allowed

    def test_secretsmanager_list_allowed(self, aws_validator):
        assert aws_validator.is_allowed("aws secretsmanager list-secrets").allowed

    def test_secretsmanager_get_secret_value_blocked(self, aws_validator):
        assert not aws_validator.is_allowed(
            "aws secretsmanager get-secret-value --secret-id foo"
        ).allowed

    # --- STS: identity allowed, token minting blocked ---
    def test_sts_get_caller_identity_allowed(self, aws_validator):
        assert aws_validator.is_allowed("aws sts get-caller-identity").allowed

    @pytest.mark.parametrize(
        "cmd",
        [
            "aws sts assume-role --role-arn arn:x --role-session-name s",
            "aws sts get-session-token",
            "aws sts get-federation-token --name foo",
        ],
    )
    def test_sts_token_minting_blocked(self, aws_validator, cmd):
        assert not aws_validator.is_allowed(cmd).allowed

    # --- EC2: get-password-data blocked (Windows admin password leak) ---
    def test_ec2_get_password_data_blocked(self, aws_validator):
        assert not aws_validator.is_allowed(
            "aws ec2 get-password-data --instance-id i-x"
        ).allowed

    # --- KMS: list/describe allowed, crypto operations blocked ---
    def test_kms_list_keys_allowed(self, aws_validator):
        assert aws_validator.is_allowed("aws kms list-keys").allowed

    @pytest.mark.parametrize(
        "cmd",
        [
            "aws kms decrypt --ciphertext-blob fileb://x",
            "aws kms encrypt --key-id alias/x --plaintext foo",
            "aws kms generate-data-key --key-id alias/x --key-spec AES_256",
            "aws kms generate-random --number-of-bytes 32",
        ],
    )
    def test_kms_crypto_operations_blocked(self, aws_validator, cmd):
        assert not aws_validator.is_allowed(cmd).allowed

    # --- Bedrock / SageMaker: list allowed, invoke blocked ---
    def test_bedrock_list_allowed(self, aws_validator):
        assert aws_validator.is_allowed("aws bedrock list-foundation-models").allowed

    @pytest.mark.parametrize(
        "cmd",
        [
            "aws bedrock-runtime invoke-model --model-id x --body b",
            "aws bedrock-runtime invoke-model-with-response-stream --model-id x --body b",
            "aws bedrock-runtime converse --model-id x",
            "aws bedrock-agent-runtime invoke-agent --agent-id a --session-id s",
            "aws sagemaker-runtime invoke-endpoint --endpoint-name x --body b",
        ],
    )
    def test_model_invocation_blocked(self, aws_validator, cmd):
        assert not aws_validator.is_allowed(cmd).allowed

    # --- RDS Data API / Timestream: queries are WRITES (they execute) ---
    @pytest.mark.parametrize(
        "cmd",
        [
            "aws rds-data execute-statement --sql 'SELECT 1' --resource-arn arn:x --secret-arn arn:y",
            "aws rds-data batch-execute-statement --sql 'SELECT 1' --resource-arn arn:x --secret-arn arn:y",
            "aws timestream-query query --query-string 'SELECT 1'",
        ],
    )
    def test_rds_data_and_timestream_queries_blocked(self, aws_validator, cmd):
        assert not aws_validator.is_allowed(cmd).allowed

    # --- Config select-resource-config runs an ad-hoc query ---
    def test_config_select_resource_config_blocked(self, aws_validator):
        assert not aws_validator.is_allowed(
            "aws configservice select-resource-config --expression 'SELECT *'"
        ).allowed


# ---------------------------------------------------------------------------
# Fuzz — 200 synthetic write-verb commands across many services.
# Not a single one should be allowed.
# ---------------------------------------------------------------------------
FUZZ_SERVICES = [
    "ec2", "s3api", "s3", "rds", "lambda", "iam", "cloudformation",
    "cloudwatch", "logs", "sqs", "sns", "kinesis", "dynamodb",
    "ecs", "eks", "route53", "cloudfront", "elbv2", "apigateway",
    "apigatewayv2", "sagemaker", "bedrock", "stepfunctions",
    "eventbridge", "config", "secretsmanager", "kms", "acm",
    "redshift", "opensearch", "glue", "athena", "emr",
    "codebuild", "codedeploy", "codepipeline", "ssm",
    "batch", "datasync", "transfer",
]

FUZZ_WRITE_VERBS = [
    "create", "delete", "update", "put", "tag", "untag",
    "attach", "detach", "modify", "enable", "disable",
    "reset", "rotate", "restore", "register", "deregister",
    "promote", "demote", "activate", "deactivate",
    "import", "export", "upgrade", "downgrade",
    "cancel", "confirm", "copy", "move", "publish",
    "subscribe", "unsubscribe", "replace", "accept",
    "reject", "set",
]


def _make_fuzz_cases(seed: int = 42, count: int = 200) -> list[str]:
    rng = random.Random(seed)
    cases = []
    while len(cases) < count:
        svc = rng.choice(FUZZ_SERVICES)
        verb = rng.choice(FUZZ_WRITE_VERBS)
        # Attach a random kebab-case noun so we exercise the `*-<tail>` form.
        nouns = [
            "resource", "instance", "policy", "item", "config",
            "permission", "rule", "subscription", "topic", "key",
        ]
        noun = rng.choice(nouns)
        suffix = rng.choice(["", "-set", "-request", "-batch", "-v2"])
        cmd = f"aws {svc} {verb}-{noun}{suffix} --name x"
        cases.append(cmd)
    return cases


class TestWriteVerbFuzz:
    @pytest.mark.parametrize("cmd", _make_fuzz_cases())
    def test_synthetic_write_rejected(self, aws_validator, cmd):
        r = aws_validator.is_allowed(cmd)
        assert not r.allowed, (
            f"fuzz: {cmd!r} should be BLOCKED; got layer={r.layer} reason={r.reason!r}"
        )


# ---------------------------------------------------------------------------
# Invariants — properties the allowlist must always hold.
# ---------------------------------------------------------------------------
class TestAllowlistInvariants:
    def test_every_pattern_compiles(self):
        # Any regex error here means the module wouldn't even import; pytest
        # still surfaces it as a clear failure.
        for p in ALLOWED_PATTERNS:
            re.compile(p, re.IGNORECASE)
        for p in WRITE_DISGUISED_AS_READ:
            re.compile(p, re.IGNORECASE)

    def test_blocklist_entries_are_all_aws(self):
        for p in WRITE_DISGUISED_AS_READ:
            assert p.startswith(r"^aws\s+"), (
                f"block-list entry must start with ^aws\\s+ : {p!r}"
            )

    def test_allowed_entries_are_all_aws_or_base_rule(self):
        for p in ALLOWED_PATTERNS:
            if p is BASE_READ_RULE:
                continue
            assert p.startswith(r"^aws\s+"), (
                f"allowed entry must start with ^aws\\s+ : {p!r}"
            )

    def test_base_read_rule_compiles_and_matches_example(self):
        assert re.compile(BASE_READ_RULE, re.IGNORECASE).match(
            "aws newservice describe-something"
        )

    def test_blocklist_checked_before_base_read_rule(self):
        # get-password-data matches the base rule literally; must still fail.
        assert not matches_allowlist_aws(
            "aws ec2 get-password-data --instance-id i-x"
        )
        # Same for secretsmanager get-secret-value.
        assert not matches_allowlist_aws(
            "aws secretsmanager get-secret-value --secret-id foo"
        )

    def test_validate_query_aws_is_idempotent_on_reads(self):
        # Benign reads always pass the semantic layer.
        for cmd in CATALOG_READS[:20]:
            ok, _ = validate_query_aws(cmd)
            assert ok, f"semantic layer shouldn't reject {cmd!r}"
