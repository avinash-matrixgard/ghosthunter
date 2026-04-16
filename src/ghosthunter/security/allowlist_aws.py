"""AWS allowlist (aws CLI).

Phase 1 skeleton — patterns are empty so `SecurityValidator(provider="aws")`
rejects every command. This proves the provider-dispatch wiring works
without yet shipping AWS functionality.

Phase 2 fills `ALLOWED_PATTERNS` with the core read-only surface and
`WRITE_DISGUISED_AS_READ` with the write-verbs that look like reads.
Phase 3 expands to the full AWS service catalog.

Allowlist philosophy (fleshed out in Phase 2):

- **Base read-rule** (generated): `^aws\\s+[a-z0-9-]+\\s+(describe|list|get)-[a-z0-9-]+\\b`
  covers read-shaped verbs across every AWS service.
- **Explicit allow**: services whose read verbs don't match the base
  pattern (e.g. `aws s3 ls`, `aws ce get-cost-and-usage`, `aws sts get-caller-identity`).
- **WRITE_DISGUISED_AS_READ**: checked FIRST. Verbs that look like reads
  but cause side effects (`aws lambda invoke`, `aws kms decrypt`,
  `aws secretsmanager get-secret-value`, `aws ssm start-session`, ...).
- **Custom validation** (Phase 3): reject `--with-decryption` on SSM,
  enforce `--max-items` / `--limit` where output could be huge.
"""
from __future__ import annotations

import re

# ---- Phase 2+ will populate these ----
ALLOWED_PATTERNS: list[str] = []
WRITE_DISGUISED_AS_READ: list[str] = []

_COMPILED_ALLOW: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in ALLOWED_PATTERNS
]
_COMPILED_BLOCK: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in WRITE_DISGUISED_AS_READ
]


def matches_allowlist_aws(command_head: str) -> bool:
    """Return True iff the command head is an AWS read-only command.

    Order:
      1. If it matches WRITE_DISGUISED_AS_READ → reject.
      2. If it matches an explicit or base ALLOWED pattern → allow.
      3. Otherwise → reject.

    In Phase 1 both lists are empty, so every AWS command is rejected.
    """
    head = command_head.strip()
    for rx in _COMPILED_BLOCK:
        if rx.match(head):
            return False
    return any(rx.match(head) for rx in _COMPILED_ALLOW)


def validate_query_aws(command: str) -> tuple[bool, str]:
    """Per-service semantic checks for AWS commands.

    Phase 3 will add:
      - `aws ssm get-parameter*` must NOT include `--with-decryption`.
      - `aws dynamodb scan|query` should warn if `--max-items`/`--limit` absent.
    """
    return True, ""


__all__ = [
    "ALLOWED_PATTERNS",
    "WRITE_DISGUISED_AS_READ",
    "matches_allowlist_aws",
    "validate_query_aws",
]
