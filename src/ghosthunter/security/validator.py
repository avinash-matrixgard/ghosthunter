"""Security validator — orchestrates Layers 1–4.

Layers 5 (budget), 6 (Sonnet semantic check), and 7 (sandbox exec) are
enforced elsewhere in the pipeline. This module is the deterministic,
code-only gate.
"""
from dataclasses import dataclass

from ghosthunter.security.allowlist import matches_allowlist, validate_bq_query
from ghosthunter.security.blocklist import find_fast_reject, has_unquoted_redirect
from ghosthunter.security.pipes import split_pipes, validate_pipes

MAX_COMMAND_LENGTH = 2000


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
    """Run a command string through Layers 1–4. Cannot be bypassed by the LLM."""

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

        # ---- Split pipes (quote-aware) ----
        segments = split_pipes(command)
        head = segments[0]
        pipe_segments = segments[1:]

        # ---- Layer 2: allowlist (primary gate) ----
        if not matches_allowlist(head):
            return _deny("command not in allowlist", "L2")

        # ---- Layer 3: pipe validation ----
        ok, reason = validate_pipes(pipe_segments)
        if not ok:
            return _deny(reason, "L3")

        # ---- Layer 4b: bq query SELECT-only enforcement ----
        ok, reason = validate_bq_query(head)
        if not ok:
            return _deny(reason, "L4")

        return _allow()
