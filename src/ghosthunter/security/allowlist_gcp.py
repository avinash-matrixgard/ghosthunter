"""GCP allowlist (gcloud / bq / gsutil).

Moved verbatim from the old flat `allowlist.py` as part of the provider
split. The dispatcher in `allowlist.py` routes GCP commands here; AWS
commands go to `allowlist_aws`.
"""

from __future__ import annotations

import re

ALLOWED_PATTERNS: list[str] = [
    # ---- Billing & Cost ----
    r"^gcloud\s+billing\s+accounts\s+list\b",
    r"^gcloud\s+billing\s+accounts\s+describe\b",
    r"^gcloud\s+billing\s+projects\s+list\b",
    r"^gcloud\s+billing\s+budgets\s+list\b",
    r"^gcloud\s+billing\s+budgets\s+describe\b",
    r"^bq\s+ls\b",
    r"^bq\s+show\b",
    r"^bq\s+head\b",
    r"^bq\s+query\s+.*['\"]\s*SELECT\b",  # SELECT only; flexible flag ordering
    # ---- Compute Engine ----
    r"^gcloud\s+compute\s+instances\s+list\b",
    r"^gcloud\s+compute\s+instances\s+describe\b",
    r"^gcloud\s+compute\s+instance-templates\s+list\b",
    r"^gcloud\s+compute\s+instance-groups\s+list\b",
    r"^gcloud\s+compute\s+instance-groups\s+list-instances\b",
    r"^gcloud\s+compute\s+machine-types\s+list\b",
    r"^gcloud\s+compute\s+disks\s+list\b",
    r"^gcloud\s+compute\s+disks\s+describe\b",
    r"^gcloud\s+compute\s+snapshots\s+list\b",
    r"^gcloud\s+compute\s+images\s+list\b",
    r"^gcloud\s+compute\s+zones\s+list\b",
    r"^gcloud\s+compute\s+regions\s+list\b",
    # ---- Networking ----
    r"^gcloud\s+compute\s+networks\s+list\b",
    r"^gcloud\s+compute\s+networks\s+describe\b",
    r"^gcloud\s+compute\s+networks\s+subnets\s+list\b",
    r"^gcloud\s+compute\s+firewall-rules\s+list\b",
    r"^gcloud\s+compute\s+firewall-rules\s+describe\b",
    r"^gcloud\s+compute\s+routers\s+list\b",
    r"^gcloud\s+compute\s+routers\s+describe\b",
    r"^gcloud\s+compute\s+routers\s+get-status\b",
    r"^gcloud\s+compute\s+routers\s+get-nat-mapping-info\b",
    r"^gcloud\s+compute\s+routers\s+nats\s+describe\b",
    r"^gcloud\s+compute\s+routes\s+list\b",
    r"^gcloud\s+compute\s+addresses\s+list\b",
    r"^gcloud\s+compute\s+forwarding-rules\s+list\b",
    r"^gcloud\s+compute\s+backend-services\s+list\b",
    r"^gcloud\s+compute\s+backend-services\s+get-health\b",
    r"^gcloud\s+compute\s+health-checks\s+list\b",
    r"^gcloud\s+compute\s+url-maps\s+list\b",
    r"^gcloud\s+compute\s+vpn-gateways\s+list\b",
    r"^gcloud\s+compute\s+vpn-tunnels\s+list\b",
    # ---- GKE ----
    r"^gcloud\s+container\s+clusters\s+list\b",
    r"^gcloud\s+container\s+clusters\s+describe\b",
    r"^gcloud\s+container\s+node-pools\s+list\b",
    r"^gcloud\s+container\s+node-pools\s+describe\b",
    r"^gcloud\s+container\s+operations\s+list\b",
    r"^gcloud\s+container\s+images\s+list\b",
    # ---- Cloud Storage (metadata only) ----
    r"^gcloud\s+storage\s+buckets\s+list\b",
    r"^gcloud\s+storage\s+buckets\s+describe\b",
    r"^gcloud\s+storage\s+ls\b",
    r"^gsutil\s+ls\b",
    r"^gsutil\s+du\b",
    r"^gsutil\s+stat\b",
    # ---- Cloud SQL ----
    r"^gcloud\s+sql\s+instances\s+list\b",
    r"^gcloud\s+sql\s+instances\s+describe\b",
    r"^gcloud\s+sql\s+databases\s+list\b",
    r"^gcloud\s+sql\s+backups\s+list\b",
    r"^gcloud\s+sql\s+operations\s+list\b",
    # ---- Cloud Functions / Run ----
    r"^gcloud\s+functions\s+list\b",
    r"^gcloud\s+functions\s+describe\b",
    r"^gcloud\s+functions\s+logs\s+read\b",
    r"^gcloud\s+run\s+services\s+list\b",
    r"^gcloud\s+run\s+services\s+describe\b",
    r"^gcloud\s+run\s+revisions\s+list\b",
    # ---- Logging ----
    r"^gcloud\s+logging\s+read\b",
    r"^gcloud\s+logging\s+logs\s+list\b",
    r"^gcloud\s+logging\s+metrics\s+list\b",
    r"^gcloud\s+logging\s+metrics\s+describe\b",
    r"^gcloud\s+logging\s+sinks\s+list\b",
    r"^gcloud\s+logging\s+resource-descriptors\s+list\b",
    # ---- Monitoring ----
    r"^gcloud\s+monitoring\s+dashboards\s+list\b",
    r"^gcloud\s+monitoring\s+dashboards\s+describe\b",
    r"^gcloud\s+monitoring\s+metrics\s+list\b",
    r"^gcloud\s+monitoring\s+channels\s+list\b",
    r"^gcloud\s+monitoring\s+policies\s+list\b",
    r"^gcloud\s+monitoring\s+policies\s+describe\b",
    # ---- DNS ----
    r"^gcloud\s+dns\s+managed-zones\s+list\b",
    r"^gcloud\s+dns\s+managed-zones\s+describe\b",
    r"^gcloud\s+dns\s+record-sets\s+list\b",
    r"^gcloud\s+dns\s+policies\s+list\b",
    # ---- IAM (read-only) ----
    r"^gcloud\s+iam\s+service-accounts\s+list\b",
    r"^gcloud\s+iam\s+service-accounts\s+describe\b",
    r"^gcloud\s+iam\s+roles\s+list\b",
    r"^gcloud\s+iam\s+roles\s+describe\b",
    r"^gcloud\s+projects\s+get-iam-policy\b",
    r"^gcloud\s+projects\s+list\b",
    r"^gcloud\s+projects\s+describe\b",
    # ---- Pub/Sub ----
    r"^gcloud\s+pubsub\s+topics\s+list\b",
    r"^gcloud\s+pubsub\s+topics\s+describe\b",
    r"^gcloud\s+pubsub\s+subscriptions\s+list\b",
    r"^gcloud\s+pubsub\s+subscriptions\s+describe\b",
    # ---- Config / Auth ----
    r"^gcloud\s+config\s+list\b",
    r"^gcloud\s+config\s+get-value\b",
    r"^gcloud\s+auth\s+list\b",
    r"^gcloud\s+info\b",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in ALLOWED_PATTERNS]


def matches_allowlist_gcp(command_head: str) -> bool:
    """Return True if the leading (pre-pipe) part of the command is allowed."""
    head = command_head.strip()
    return any(rx.match(head) for rx in _COMPILED)


# Keywords that may not appear anywhere in a `bq query` payload.
BQ_FORBIDDEN_KEYWORDS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "CREATE",
    "ALTER",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "MERGE",
)


def validate_query_gcp(command: str) -> tuple[bool, str]:
    """Defense-in-depth check for `bq query` commands.

    Returns (ok, reason). If the command isn't a bq query, ok=True.
    """
    stripped = command.strip()
    if not stripped.lower().startswith("bq query"):
        return True, ""
    upper = stripped.upper()
    for kw in BQ_FORBIDDEN_KEYWORDS:
        # Use word boundaries so 'CREATED_AT' doesn't match 'CREATE'.
        if re.search(rf"\b{kw}\b", upper):
            return False, f"bq query contains forbidden keyword: {kw}"
    return True, ""


__all__ = [
    "ALLOWED_PATTERNS",
    "BQ_FORBIDDEN_KEYWORDS",
    "matches_allowlist_gcp",
    "validate_query_gcp",
]
