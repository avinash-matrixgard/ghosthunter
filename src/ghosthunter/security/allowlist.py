"""Layer 2 allowlist dispatcher.

Routes a command head to the right per-provider allowlist module based on
the CLI binary the command starts with (`gcloud|bq|gsutil` → GCP;
`aws` → AWS). The actual patterns live in `allowlist_gcp.py` and
`allowlist_aws.py`.

Backward-compatible: the original module-level names `matches_allowlist`,
`validate_bq_query`, `ALLOWED_PATTERNS`, and `BQ_FORBIDDEN_KEYWORDS` still
resolve to the GCP ruleset, so existing callers and tests keep working
without edits.
"""

from __future__ import annotations

from ghosthunter.security.allowlist_aws import (
    matches_allowlist_aws,
    validate_query_aws,
)
from ghosthunter.security.allowlist_gcp import (
    ALLOWED_PATTERNS,  # re-export for back-compat (GCP patterns)
    BQ_FORBIDDEN_KEYWORDS,  # re-export for back-compat
    matches_allowlist_gcp,
    validate_query_gcp,
)

# CLI binary -> provider key. Anything else is an unknown provider.
_CLI_TO_PROVIDER: dict[str, str] = {
    "gcloud": "gcp",
    "bq": "gcp",
    "gsutil": "gcp",
    "aws": "aws",
}


def infer_provider(command_head: str) -> str | None:
    """Return the provider key implied by a command, or None if unknown."""
    head = command_head.strip()
    if not head:
        return None
    first = head.split(maxsplit=1)[0].lower()
    return _CLI_TO_PROVIDER.get(first)


def matches_allowlist_for(command_head: str, provider: str) -> bool:
    """Allowlist match against a specific provider's ruleset.

    If the command's CLI binary doesn't belong to `provider` (e.g.
    `gcloud ...` asked against provider=`aws`), the answer is False —
    enforce cross-provider isolation.
    """
    inferred = infer_provider(command_head)
    if inferred is not None and inferred != provider:
        return False
    if provider == "gcp":
        return matches_allowlist_gcp(command_head)
    if provider == "aws":
        return matches_allowlist_aws(command_head)
    return False


def validate_query_for(command: str, provider: str) -> tuple[bool, str]:
    """Semantic query validation (bq SELECT-only, SSM no-decryption, ...)."""
    if provider == "gcp":
        return validate_query_gcp(command)
    if provider == "aws":
        return validate_query_aws(command)
    return True, ""


# ---------------------------------------------------------------------------
# Back-compat API — keep the original un-parameterized functions pointing
# at the GCP ruleset so existing callers don't need to change.
# ---------------------------------------------------------------------------
def matches_allowlist(command_head: str) -> bool:
    """Legacy entry point. Equivalent to matches_allowlist_for(..., "gcp").

    Kept so the current `validator.py`, tests, and anything else depending
    on this name continues to resolve without edits.
    """
    return matches_allowlist_gcp(command_head)


def validate_bq_query(command: str) -> tuple[bool, str]:
    """Legacy entry point. Equivalent to validate_query_for(..., "gcp")."""
    return validate_query_gcp(command)


__all__ = [
    "ALLOWED_PATTERNS",
    "BQ_FORBIDDEN_KEYWORDS",
    "infer_provider",
    "matches_allowlist",
    "matches_allowlist_for",
    "validate_bq_query",
    "validate_query_for",
]
