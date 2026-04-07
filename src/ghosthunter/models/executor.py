"""Sonnet executor: semantic command validation + output compression.

Sonnet is the second-line check between Opus and the shell. It does two
distinct jobs:

1. `semantic_validate(command)` — Layer 6. After the static validator
   approves a command, Sonnet looks at it again and answers a single
   question: "is running this command on a production GCP project safe
   and useful for the investigation?" This catches things like a
   `gcloud logging read` filter that would dump 50M sensitive log lines.

2. `compress(command, output, ...)` — squashes raw command stdout into
   ~500 tokens of facts before Opus ever sees it. This is what keeps
   Opus's context window small and its reasoning sharp.

Sonnet NEVER reasons about hypotheses. It just executes and summarizes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

EXECUTOR_MODEL = "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Semantic validation (Layer 6)
# ---------------------------------------------------------------------------
SEMANTIC_VALIDATION_SYSTEM = """You are a security checker for read-only GCP commands.

A static validator has already confirmed the command is syntactically safe
(no shell injection, allowlisted verb, safe pipes only). Your job is the
LAST CHECK: would running this command on a real production GCP project
be safe and reasonable for a cost investigation?

Approve unless the command would:
- Return an unreasonable amount of data (e.g. `gcloud logging read` with
  no time filter and no --limit)
- Touch resources outside the cost-investigation scope (e.g. dumping IAM
  policies for unrelated projects)
- Match a pattern that looks crafted to exfiltrate sensitive data

Bias toward APPROVE. The static validator already blocked anything truly
destructive. Only veto if you can articulate a concrete concern.

Always respond via the `semantic_check` tool. Never reply in plain text.
"""

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
# Output compression
# ---------------------------------------------------------------------------
COMPRESSION_SYSTEM = """You compress raw GCP command output into a tight
factual summary for a cost investigator.

Rules:
1. Include exact numbers (counts, sizes, costs) — never round or estimate.
2. Surface anything that could shift hypothesis confidence up or down.
3. Note anomalies the investigator might want to spawn a new hypothesis for.
4. Drop raw JSON structure, repeated entries, and fields irrelevant to cost.
5. Stay under 500 tokens. If the output is huge, prioritize ruthlessly.

Output plain text bullets. No preamble, no markdown headers.
"""


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
    """Sonnet-backed validator + compressor."""

    def __init__(
        self,
        client: "AsyncAnthropic | None" = None,
        model: str = EXECUTOR_MODEL,
        max_validation_tokens: int = 512,
        max_compression_tokens: int = 800,
        max_raw_output_chars: int = 200_000,
    ) -> None:
        if client is None:
            from anthropic import AsyncAnthropic  # lazy import
            client = AsyncAnthropic()
        self.client = client
        self.model = model
        self.max_validation_tokens = max_validation_tokens
        self.max_compression_tokens = max_compression_tokens
        self.max_raw_output_chars = max_raw_output_chars

    # --------------------------------------------------------------
    # Layer 6
    # --------------------------------------------------------------
    async def semantic_validate(self, command: str) -> SemanticResult:
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_validation_tokens,
            system=SEMANTIC_VALIDATION_SYSTEM,
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
            system=COMPRESSION_SYSTEM,
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
