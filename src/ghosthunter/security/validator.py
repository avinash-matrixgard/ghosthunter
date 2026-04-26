"""Security validator — orchestrates Layers 1–4.

Layers 5 (budget), 6 (Sonnet semantic check), and 7 (sandbox exec) are
enforced elsewhere in the pipeline. This module is the deterministic,
code-only gate.

The validator is provider-aware: pass `provider="gcp"` (default) or
`"aws"` to constrain allowlist + semantic checks to that provider.
No-arg construction keeps the GCP-only behavior existing callers depend on.
"""

from dataclasses import dataclass

from ghosthunter.security.allowlist import (
    matches_allowlist_for,
    validate_query_for,
)
from ghosthunter.security.blocklist import (
    find_fast_reject,
    has_unquoted_command_substitution,
    has_unquoted_redirect,
)
from ghosthunter.security.pipes import split_pipes, validate_pipes

MAX_COMMAND_LENGTH = 2000

DEFAULT_PROVIDER = "gcp"


@dataclass
class ValidationResult:
    allowed: bool
    reason: str = ""
    layer: str = ""


def _deny(reason: str, layer: str) -> ValidationResult:
    return ValidationResult(allowed=False, reason=reason, layer=layer)


def _allow() -> ValidationResult:
    return ValidationResult(allowed=True, reason="ok", layer="")


class SecurityValidator:
    """Run a command string through Layers 1–4. Cannot be bypassed by the LLM.

    Parameters
    ----------
    provider:
        Which provider's allowlist and semantic-query rules to apply.
        Defaults to ``"gcp"`` so zero-arg construction preserves pre-AWS
        behavior. Pass ``"aws"`` for AWS mode.
    """

    def __init__(self, provider: str = DEFAULT_PROVIDER) -> None:
        self.provider = provider

    def is_allowed(self, command: str) -> ValidationResult:
        # ---- Layer 4a: input hygiene (cheap pre-checks) ----
        if command is None or not command.strip():
            return _deny("empty command", "L4")
        if len(command) > MAX_COMMAND_LENGTH:
            return _deny(
                f"command exceeds max length ({MAX_COMMAND_LENGTH} chars)",
                "L4",
            )

        # ---- Layer 1: fast reject ----
        bad = find_fast_reject(command)
        if bad:
            return _deny(f"fast-reject pattern matched: {bad}", "L1")
        if has_unquoted_redirect(command):
            return _deny("unquoted redirect operator (< > >>)", "L1")
        bad_subst = has_unquoted_command_substitution(command)
        if bad_subst:
            return _deny(
                f"unquoted shell substitution: {bad_subst}",
                "L1",
            )

        # ---- Split pipes (quote-aware) ----
        segments = split_pipes(command)
        head = segments[0]
        pipe_segments = segments[1:]

        # ---- Layer 2: allowlist (primary gate) ----
        if not matches_allowlist_for(head, self.provider):
            return _deny("command not in allowlist", "L2")

        # ---- Layer 3: pipe validation ----
        ok, reason = validate_pipes(pipe_segments)
        if not ok:
            return _deny(reason, "L3")

        # ---- Layer 4b: provider-specific semantic check ----
        # (GCP: bq-query SELECT-only; AWS: SSM --with-decryption, etc.)
        ok, reason = validate_query_for(head, self.provider)
        if not ok:
            return _deny(reason, "L4")

        return _allow()
