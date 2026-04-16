"""Sonnet executor: semantic command validation + output compression.

Sonnet is the second-line check between Opus and the shell. It does two
distinct jobs:

1. `semantic_validate(command)` — Layer 6. After the static validator
   approves a command, Sonnet looks at it again and answers a single
   question: "is running this command on a real production cloud account
   safe and useful for the investigation?" This catches things like a
   `gcloud logging read` / `aws logs filter-log-events` with no filter or
   --limit that would dump millions of sensitive log lines.

2. `compress(command, output, ...)` — squashes raw command stdout into
   ~500 tokens of facts before Opus ever sees it. This is what keeps
   Opus's context window small and its reasoning sharp.

Sonnet NEVER reasons about hypotheses. It just executes and summarizes.

Both jobs are parameterized by provider (`"gcp"` | `"aws"`) so the
system prompt tells Sonnet which CLI vocabulary it's validating. Without
this split Sonnet Layer-6-rejects legitimate AWS commands because the
old prompt said "read-only GCP only".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

EXECUTOR_MODEL = "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Semantic validation (Layer 6) — split into a provider-neutral core + a
# per-provider block that names the CLI and gives provider-relevant
# "unreasonable" examples. Same pattern used by models/reasoner.py.
# ---------------------------------------------------------------------------
_SEMANTIC_CORE = """You are a security checker for read-only cloud cost \
investigation commands.

A static validator has already confirmed the command is syntactically safe
(no shell injection, allowlisted verb, safe pipes only). Your job is the
LAST CHECK: would running this command on a real production cloud account
be safe and reasonable for a cost investigation?

Approve unless the command would:
- Return an unreasonable amount of data (e.g. a logs read with no time
  filter and no --limit/--max-items)
- Touch resources outside the cost-investigation scope (e.g. dumping
  IAM policies for unrelated accounts/projects)
- Match a pattern that looks crafted to exfiltrate sensitive data

Bias toward APPROVE. The static validator already blocked anything truly
destructive. Only veto if you can articulate a concrete concern.

Always respond via the `semantic_check` tool. Never reply in plain text.
"""

_SEMANTIC_PROVIDER_NOTES: dict[str, str] = {
    "gcp": """
Provider context for this investigation: **GCP**.
The command should be one of: gcloud, bq, gsutil. Allowlisted read verbs
include: list, describe, read, get-*, get-value, show, head.
A common red flag is `gcloud logging read` with no time window.
""",
    "aws": """
Provider context for this investigation: **AWS**.
The command should be the `aws` CLI. Allowlisted read verbs include:
describe-*, list-*, get-*, batch-get-*, plus specific reads like
`aws s3 ls`, `aws ce get-cost-and-usage`, `aws cloudtrail lookup-events`,
`aws logs filter-log-events`, `aws logs tail`, `aws sts get-caller-identity`,
`aws dynamodb scan|query|batch-get-item`, `aws iam simulate-*-policy`,
`aws cloudformation validate-template|detect-stack-drift`,
`aws ec2 search-*-gateway-routes`.
These ARE legitimate cost-investigation commands even if they don't look
like `describe-*`/`list-*`/`get-*`. APPROVE them.
A common red flag is `aws logs filter-log-events` with no --start-time
bound, or `aws ce get-cost-and-usage` with a multi-year window.
""",
}


def build_semantic_validation_prompt(provider: str = "gcp") -> str:
    """Compose the Sonnet Layer-6 system prompt for a given provider."""
    notes = _SEMANTIC_PROVIDER_NOTES.get(provider, "")
    return _SEMANTIC_CORE + notes


# Back-compat alias — pre-split code imports this name directly.
SEMANTIC_VALIDATION_SYSTEM = build_semantic_validation_prompt("gcp")

SEMANTIC_CHECK_TOOL: dict[str, Any] = {
    "name": "semantic_check",
    "description": "Approve or reject a command after the static validator passed it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "approved": {"type": "boolean"},
            "reason": {
                "type": "string",
                "description": "If rejected, explain concretely why. If approved, one short sentence.",
            },
        },
        "required": ["approved", "reason"],
    },
}


@dataclass
class SemanticResult:
    approved: bool
    reason: str


# ---------------------------------------------------------------------------
# Output compression — also provider-neutral. The old text said "raw GCP
# command output"; replaced with the cloud-generic phrasing so Sonnet
# treats AWS output the same way.
# ---------------------------------------------------------------------------
_COMPRESSION_CORE = """You compress raw cloud command output into a tight
factual summary for a cost investigator.

Rules:
1. Include exact numbers (counts, sizes, costs) — never round or estimate.
2. Surface anything that could shift hypothesis confidence up or down.
3. Note anomalies the investigator might want to spawn a new hypothesis for.
4. Drop raw JSON structure, repeated entries, and fields irrelevant to cost.
5. Stay under 500 tokens. If the output is huge, prioritize ruthlessly.

Output plain text bullets. No preamble, no markdown headers.
"""


def build_compression_prompt(provider: str = "gcp") -> str:
    """Compose the Sonnet compression system prompt for a given provider.

    Today the compression rules are identical across providers, so this
    is really a seam for future provider-specific compression hints (e.g.
    "AWS CloudWatch metric statistics use Datapoints[]; surface the
    trend, not every sample"). Kept parameterized for symmetry with
    `build_semantic_validation_prompt`.
    """
    return _COMPRESSION_CORE


# Back-compat alias — pre-split code imports this name directly.
COMPRESSION_SYSTEM = build_compression_prompt("gcp")


def _build_compression_user_message(
    command: str,
    output: str,
    investigation_target: str,
    hypotheses: list[str],
) -> str:
    hypotheses_block = "\n".join(f"- {h}" for h in hypotheses) or "(none yet)"
    return (
        f"Investigation target: {investigation_target}\n\n"
        f"Current hypotheses:\n{hypotheses_block}\n\n"
        f"Command that produced this output:\n{command}\n\n"
        f"Raw output:\n{output}"
    )


# ---------------------------------------------------------------------------
# Executor client
# ---------------------------------------------------------------------------
class ExecutorError(Exception):
    """Raised when Sonnet returns an unparseable response."""


class Executor:
    """Sonnet-backed validator + compressor.

    Parameters
    ----------
    provider:
        ``"gcp"`` (default) or ``"aws"``. Selects the right Layer-6
        system prompt so Sonnet doesn't reject legitimate AWS commands
        because the prompt said "GCP only". Zero-arg construction
        preserves pre-AWS behaviour.
    """

    def __init__(
        self,
        client: "AsyncAnthropic | None" = None,
        model: str = EXECUTOR_MODEL,
        max_validation_tokens: int = 512,
        max_compression_tokens: int = 800,
        max_raw_output_chars: int = 200_000,
        provider: str = "gcp",
    ) -> None:
        if client is None:
            from anthropic import AsyncAnthropic  # lazy import
            client = AsyncAnthropic()
        self.client = client
        self.model = model
        self.max_validation_tokens = max_validation_tokens
        self.max_compression_tokens = max_compression_tokens
        self.max_raw_output_chars = max_raw_output_chars
        self.provider = provider
        self.semantic_system = build_semantic_validation_prompt(provider)
        self.compression_system = build_compression_prompt(provider)

    # --------------------------------------------------------------
    # Layer 6
    # --------------------------------------------------------------
    async def semantic_validate(self, command: str) -> SemanticResult:
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_validation_tokens,
            system=self.semantic_system,
            tools=[SEMANTIC_CHECK_TOOL],
            tool_choice={"type": "tool", "name": "semantic_check"},
            messages=[
                {
                    "role": "user",
                    "content": f"Command to check:\n{command}",
                }
            ],
        )

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "semantic_check":
                payload = block.input
                return SemanticResult(
                    approved=bool(payload["approved"]),
                    reason=payload.get("reason", ""),
                )

        raise ExecutorError("Sonnet did not return a semantic_check tool call")

    # --------------------------------------------------------------
    # Compression
    # --------------------------------------------------------------
    async def compress(
        self,
        command: str,
        output: str,
        investigation_target: str,
        hypotheses: list[str],
    ) -> str:
        """Squash raw command output into a short factual summary."""
        # Hard cap on raw input — Sonnet still has a context window.
        if len(output) > self.max_raw_output_chars:
            output = (
                output[: self.max_raw_output_chars]
                + f"\n\n[... truncated, original was {len(output)} chars ...]"
            )

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_compression_tokens,
            system=self.compression_system,
            messages=[
                {
                    "role": "user",
                    "content": _build_compression_user_message(
                        command, output, investigation_target, hypotheses
                    ),
                }
            ],
        )

        parts: list[str] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        if not parts:
            raise ExecutorError("Sonnet returned no text blocks for compression")
        return "\n".join(parts).strip()
