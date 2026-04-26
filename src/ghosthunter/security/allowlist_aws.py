"""AWS allowlist (aws CLI).

Phase 2 ships the **core MVP** — the minimum set of patterns needed for
end-to-end advisor-mode AWS investigations covering cost-dominant
services (EC2, S3, RDS, Lambda, CloudWatch, Logs, IAM, CloudTrail, VPC,
Cost Explorer).

Phase 3 will expand to the full catalog (ECS/EKS, CloudFront, ELB,
API Gateway, Redshift, OpenSearch, Glue, SageMaker, Bedrock, etc).

Allowlist philosophy:

- **Base read-rule** (generated): ``^aws\\s+[a-z0-9-]+\\s+(describe|list|get)-[a-z0-9-]+\\b``
  covers read-shaped verbs generically across every AWS service. This is
  why Phase 2 can allow commands like ``aws ec2 describe-load-balancers``
  without listing every verb individually.
- **Explicit allow** for non-read-shaped commands we still want
  (``aws s3 ls``, ``aws sts get-caller-identity``, etc).
- **WRITE_DISGUISED_AS_READ** is checked FIRST. Any command that matches
  one of these patterns is rejected even if it would also match the base
  read rule. This catches ``get-*``-verbs that cause writes or leak
  secrets (``aws ec2 get-password-data``, ``aws secretsmanager
  get-secret-value``, ``aws kms decrypt``, ...).
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Base read-verb rule
#
# Matches any `aws <service> (describe|list|get)-<verb>` pattern. Handles
# both hyphenated and single-word verbs. All AWS services use kebab-case
# for their service names (e.g. ``ec2``, ``rds``, ``route53resolver``,
# ``ssm``, ``stepfunctions``).
# ---------------------------------------------------------------------------
BASE_READ_RULE = r"^aws\s+[a-z0-9-]+\s+(describe|list|get|batch-get)-[a-z0-9-]+\b"


# ---------------------------------------------------------------------------
# Explicit allowed patterns (for commands that don't match BASE_READ_RULE,
# or where we want to be very specific about what's permitted).
# ---------------------------------------------------------------------------
ALLOWED_PATTERNS: list[str] = [
    # ---- S3 (aws s3 ls uses the higher-level CLI, not s3api) ----
    r"^aws\s+s3\s+ls\b",
    # aws s3 ls s3://bucket — still ls, handled by the above.
    # ---- Cost Explorer / Budgets ----
    r"^aws\s+ce\s+get-cost-and-usage\b",
    r"^aws\s+ce\s+get-dimension-values\b",
    r"^aws\s+ce\s+get-tags\b",
    r"^aws\s+ce\s+get-cost-and-usage-with-resources\b",
    r"^aws\s+ce\s+get-cost-forecast\b",
    r"^aws\s+ce\s+get-anomalies\b",
    r"^aws\s+ce\s+get-anomaly-monitors\b",
    r"^aws\s+ce\s+get-anomaly-subscriptions\b",
    r"^aws\s+budgets\s+describe-budgets\b",
    r"^aws\s+budgets\s+describe-budget\b",
    r"^aws\s+cur\s+describe-report-definitions\b",
    # ---- STS (caller identity — used for sanity checks) ----
    r"^aws\s+sts\s+get-caller-identity\b",
    r"^aws\s+sts\s+decode-authorization-message\b",
    # ---- Read-only verbs that don't fit the describe/list/get pattern ----
    # (BASE_READ_RULE below catches everything that does.)
    #
    # CloudWatch Logs
    r"^aws\s+logs\s+filter-log-events\b",
    r"^aws\s+logs\s+tail\b",
    # CloudTrail
    r"^aws\s+cloudtrail\s+lookup-events\b",
    # EC2 routes
    r"^aws\s+ec2\s+search-transit-gateway-routes\b",
    r"^aws\s+ec2\s+search-local-gateway-routes\b",
    # Resource Groups — lists every tagged resource; hugely useful for cost
    r"^aws\s+resourcegroupstaggingapi\s+get-resources\b",
    r"^aws\s+resource-groups\s+search-resources\b",
    # DynamoDB — scan / query / batch-get-item are the primary read paths
    r"^aws\s+dynamodb\s+scan\b",
    r"^aws\s+dynamodb\s+query\b",
    r"^aws\s+dynamodb\s+batch-get-item\b",
    # IAM policy simulation (read-only, even though named simulate-*)
    r"^aws\s+iam\s+simulate-principal-policy\b",
    r"^aws\s+iam\s+simulate-custom-policy\b",
    # CloudFormation — template validation is read-only
    r"^aws\s+cloudformation\s+validate-template\b",
    r"^aws\s+cloudformation\s+detect-stack-drift\b",  # starts an async detection; read-oriented
    # Route 53 — test DNS resolution
    r"^aws\s+route53\s+test-dns-answer\b",
    # Athena — batch read
    r"^aws\s+athena\s+batch-get-query-execution\b",
    r"^aws\s+athena\s+batch-get-named-query\b",
    # Cost Anomaly Detection extras
    r"^aws\s+ce\s+get-anomaly-detectors\b",
    r"^aws\s+ce\s+get-cost-categories\b",
    r"^aws\s+ce\s+get-rightsizing-recommendation\b",
    r"^aws\s+ce\s+get-reservation-coverage\b",
    r"^aws\s+ce\s+get-reservation-purchase-recommendation\b",
    r"^aws\s+ce\s+get-reservation-utilization\b",
    r"^aws\s+ce\s+get-savings-plans-coverage\b",
    r"^aws\s+ce\s+get-savings-plans-purchase-recommendation\b",
    r"^aws\s+ce\s+get-savings-plans-utilization\b",
    r"^aws\s+ce\s+get-savings-plans-utilization-details\b",
    # Pricing API — read-only
    r"^aws\s+pricing\s+get-products\b",
    r"^aws\s+pricing\s+get-attribute-values\b",
    r"^aws\s+pricing\s+describe-services\b",  # matches base too, but explicit is fine
    # CloudWatch Logs Insights — get-query-results reads COMPLETED results.
    # (start-query is blocked; this only retrieves what's already finished.)
    r"^aws\s+logs\s+get-query-results\b",
    # Athena — get-query-results reads completed results
    r"^aws\s+athena\s+get-query-results\b",
    # Application Auto Scaling — read-only scheduled actions
    r"^aws\s+application-autoscaling\s+describe-scalable-targets\b",  # base catches, explicit ok
    # Organizations — describe-organization / list-accounts caught by base
    # AWS Config — read-only resource config
    r"^aws\s+configservice\s+list-discovered-resources\b",
    r"^aws\s+configservice\s+get-resource-config-history\b",
    # Compute Optimizer — reads rightsizing recommendations
    r"^aws\s+compute-optimizer\s+get-ec2-instance-recommendations\b",
    r"^aws\s+compute-optimizer\s+get-auto-scaling-group-recommendations\b",
    r"^aws\s+compute-optimizer\s+get-ebs-volume-recommendations\b",
    r"^aws\s+compute-optimizer\s+get-lambda-function-recommendations\b",
    r"^aws\s+compute-optimizer\s+get-rds-database-recommendations\b",
    # Trusted Advisor — reads checks (read-only)
    r"^aws\s+support\s+describe-trusted-advisor-checks\b",
    r"^aws\s+support\s+describe-trusted-advisor-check-result\b",
    r"^aws\s+support\s+describe-trusted-advisor-check-summaries\b",
    # ---- The base read rule picks up everything else that fits the
    #      describe-/list-/get- pattern across every service.
    BASE_READ_RULE,
]


# ---------------------------------------------------------------------------
# Write operations disguised as reads.
#
# Verbs that syntactically look like reads (``get-*``, ``start-query-results``,
# etc.) but actually cause side effects, leak secrets, or run workloads.
# Checked BEFORE the allow rules so they reject even when the command
# would otherwise match ``BASE_READ_RULE``.
# ---------------------------------------------------------------------------
WRITE_DISGUISED_AS_READ: list[str] = [
    # ---- Code execution / side effects ----
    r"^aws\s+lambda\s+invoke\b",
    r"^aws\s+lambda\s+invoke-async\b",
    r"^aws\s+sns\s+publish\b",
    r"^aws\s+sns\s+publish-batch\b",
    r"^aws\s+sqs\s+send-message\b",
    r"^aws\s+sqs\s+send-message-batch\b",
    r"^aws\s+kinesis\s+put-record\b",
    r"^aws\s+kinesis\s+put-records\b",
    r"^aws\s+kinesis\s+put-resource-policy\b",
    r"^aws\s+athena\s+start-query-execution\b",
    r"^aws\s+athena\s+start-calculation-execution\b",
    r"^aws\s+athena\s+start-session\b",
    r"^aws\s+stepfunctions\s+start-execution\b",
    r"^aws\s+stepfunctions\s+start-sync-execution\b",
    r"^aws\s+ssm\s+send-command\b",
    r"^aws\s+ssm\s+start-session\b",
    r"^aws\s+ssm\s+start-automation-execution\b",
    r"^aws\s+ec2\s+run-instances\b",
    r"^aws\s+ec2\s+start-instances\b",
    r"^aws\s+ec2\s+stop-instances\b",
    r"^aws\s+ec2\s+terminate-instances\b",
    r"^aws\s+ec2\s+reboot-instances\b",
    r"^aws\s+rds\s+start-db-instance\b",
    r"^aws\s+rds\s+stop-db-instance\b",
    r"^aws\s+rds\s+reboot-db-instance\b",
    r"^aws\s+logs\s+start-query\b",  # CloudWatch Logs Insights — starts a query job
    r"^aws\s+logs\s+start-live-tail\b",
    # ---- Credential / secret exfiltration ----
    r"^aws\s+secretsmanager\s+get-secret-value\b",
    r"^aws\s+secretsmanager\s+get-random-password\b",
    r"^aws\s+ec2\s+get-password-data\b",  # Windows admin password
    r"^aws\s+kms\s+decrypt\b",
    r"^aws\s+kms\s+encrypt\b",
    r"^aws\s+kms\s+re-encrypt\b",
    r"^aws\s+kms\s+generate-data-key\b",
    r"^aws\s+kms\s+generate-data-key-pair\b",
    r"^aws\s+kms\s+generate-data-key-pair-without-plaintext\b",
    r"^aws\s+kms\s+generate-data-key-without-plaintext\b",
    r"^aws\s+kms\s+generate-mac\b",
    r"^aws\s+kms\s+generate-random\b",
    r"^aws\s+sts\s+get-session-token\b",  # mints temporary credentials
    r"^aws\s+sts\s+get-federation-token\b",  # mints temporary credentials
    r"^aws\s+sts\s+assume-role\b",  # mints temporary credentials
    r"^aws\s+sts\s+assume-role-with-saml\b",
    r"^aws\s+sts\s+assume-role-with-web-identity\b",
    r"^aws\s+iam\s+get-credential-report\b",  # can contain sensitive data
    r"^aws\s+iam\s+get-access-key-last-used\b",  # leaks usage patterns
    # ---- Signing / signatures (could forge JWTs etc.) ----
    r"^aws\s+signer\s+sign-payload\b",
    # ---- Cognito / auth flows that mint tokens ----
    r"^aws\s+cognito-idp\s+initiate-auth\b",
    r"^aws\s+cognito-idp\s+admin-initiate-auth\b",
    r"^aws\s+cognito-idp\s+get-id\b",  # cognito-identity mints temporary creds
    r"^aws\s+cognito-identity\s+get-id\b",
    r"^aws\s+cognito-identity\s+get-credentials-for-identity\b",
    r"^aws\s+cognito-identity\s+get-open-id-token\b",
    r"^aws\s+cognito-identity\s+get-open-id-token-for-developer-identity\b",
    # ---- Lambda / model invocation (spends money, causes side effects) ----
    r"^aws\s+bedrock-runtime\s+invoke-model\b",
    r"^aws\s+bedrock-runtime\s+invoke-model-with-response-stream\b",
    r"^aws\s+bedrock-runtime\s+converse\b",
    r"^aws\s+bedrock-runtime\s+converse-stream\b",
    r"^aws\s+bedrock-agent-runtime\s+invoke-agent\b",
    r"^aws\s+bedrock-agent-runtime\s+retrieve-and-generate\b",
    r"^aws\s+sagemaker-runtime\s+invoke-endpoint\b",
    r"^aws\s+sagemaker-runtime\s+invoke-endpoint-async\b",
    r"^aws\s+sagemaker-runtime\s+invoke-endpoint-with-response-stream\b",
    # ---- CloudWatch Synthetics — trigger canaries (costs money, can hit user URLs) ----
    r"^aws\s+synthetics\s+start-canary\b",
    # ---- S3 data egress — CAN leak data, but also essential for cost forensics.
    # We allow these but warn via the reasoner prompt; block only the bulk
    # presigned-URL path that could be chained into automated exfil.
    r"^aws\s+s3\s+presign\b",
    r"^aws\s+s3api\s+get-object-attributes\b",  # metadata only, BUT cheap path to enumerate buckets
    # ---- EC2 VPC endpoint / network side effects ----
    r"^aws\s+ec2\s+send-diagnostic-interrupt\b",  # reboots instance via NMI
    r"^aws\s+ec2\s+reset-image-attribute\b",
    # ---- AWS Config — select-resource-config starts an ad-hoc SQL-ish query ----
    r"^aws\s+configservice\s+select-resource-config\b",
    r"^aws\s+configservice\s+select-aggregate-resource-config\b",
    # ---- QuickSight — get-dashboard-embed-url mints shareable URLs ----
    r"^aws\s+quicksight\s+get-dashboard-embed-url\b",
    r"^aws\s+quicksight\s+generate-embed-url-for-anonymous-user\b",
    r"^aws\s+quicksight\s+generate-embed-url-for-registered-user\b",
    # ---- Support case creation / comment (user-visible side effects) ----
    # "create-case" doesn't match BASE_READ_RULE anyway; listed for clarity.
    # No entry needed — kept in mind for future expansion.
    # ---- AWS CLI v2 stream readers that also run code ----
    r"^aws\s+rds-data\s+execute-statement\b",  # runs SQL against RDS Data API
    r"^aws\s+rds-data\s+batch-execute-statement\b",
    r"^aws\s+timestream-query\s+query\b",  # runs Timestream queries (costs money)
]


_COMPILED_ALLOW: list[re.Pattern[str]] = [re.compile(p, re.IGNORECASE) for p in ALLOWED_PATTERNS]
_COMPILED_BLOCK: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in WRITE_DISGUISED_AS_READ
]


def matches_allowlist_aws(command_head: str) -> bool:
    """Return True iff the command head is an AWS read-only command.

    Order (important for correctness):
      1. WRITE_DISGUISED_AS_READ → always reject (even if it matches BASE_READ_RULE).
      2. ALLOWED_PATTERNS (incl. BASE_READ_RULE) → accept.
      3. Otherwise → reject.
    """
    head = command_head.strip()
    for rx in _COMPILED_BLOCK:
        if rx.match(head):
            return False
    return any(rx.match(head) for rx in _COMPILED_ALLOW)


# SSM parameter reads with decryption → leak secrets, reject.
# Phase 3 adds more semantic rules here.
_SSM_WITH_DECRYPTION = re.compile(
    r"^aws\s+ssm\s+get-parameter(s|s-by-path)?\b.*--with-decryption",
    re.IGNORECASE,
)


def validate_query_aws(command: str) -> tuple[bool, str]:
    """Per-service semantic checks for AWS commands.

    Runs AFTER Layer 2 allowlist and BEFORE execution. Returns
    ``(ok, reason)``; if ok=False, the validator rejects at Layer 4.
    """
    stripped = command.strip()
    if _SSM_WITH_DECRYPTION.search(stripped):
        return (
            False,
            "aws ssm get-parameter* with --with-decryption is blocked (would leak secrets)",
        )
    return True, ""


__all__ = [
    "ALLOWED_PATTERNS",
    "BASE_READ_RULE",
    "WRITE_DISGUISED_AS_READ",
    "matches_allowlist_aws",
    "validate_query_aws",
]
