"""Opus reasoner: forms hypotheses, designs commands, interprets evidence.

Opus NEVER sees raw command output — only Sonnet's compressed summaries.
This keeps Opus focused on reasoning, not parsing JSON.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

REASONER_MODEL = "claude-opus-4-6"

REASONER_SYSTEM_PROMPT = """You are Ghosthunter, an expert cloud cost investigator.

Your job: figure out WHY a cloud cost spiked, not just what spiked. You form
2–4 competing hypotheses, test them with read-only commands, and update
confidence based on evidence until one hypothesis exceeds 85%.

## CRITICAL RULES

1. NEVER GUESS. If you don't have evidence, say "I need to verify X".

2. EVERY claim must cite evidence:
   ❌ "The Lambda functions are causing high NAT costs"
   ✓  "The Lambda functions MAY be causing high NAT costs. Evidence needed: <command>"

3. CONFIDENCE LEVELS:
   - CONFIRMED (>=85): Direct evidence proves this
   - LIKELY (60-84):   Strong indicators, not proven
   - HYPOTHESIS (20-59): Reasonable guess, needs verification
   - UNLIKELY (<20):   Evidence refutes this
   When confidence drops to <=5, mark the hypothesis ELIMINATED.
   When confidence reaches >=85, mark it CONFIRMED and conclude.

4. All numbers must come from command output. NEVER invent statistics.

5. You CAN say "I don't know" or "the evidence is inconclusive".

6. Commands must be GCP read-only (gcloud, bq, gsutil). The security layer
   will reject anything destructive — don't waste a turn proposing it.

7. Always return your response via the `investigation_step` tool. Never
   reply in plain text.
"""

# Tool schema Opus uses to return structured investigation steps.
INVESTIGATION_TOOL: dict[str, Any] = {
    "name": "investigation_step",
    "description": "Report current hypotheses and propose the next action.",
    "input_schema": {
        "type": "object",
        "properties": {
            "hypotheses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                        "status": {
                            "type": "string",
                            "enum": ["active", "eliminated", "confirmed"],
                        },
                        "evidence_for": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "evidence_against": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["id", "description", "confidence", "status"],
                },
            },
            "reasoning": {
                "type": "string",
                "description": "Brief explanation of how new evidence shifted confidence.",
            },
            "next_action": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["command", "conclude", "need_info"],
                    },
                    "command": {"type": "string"},
                    "tests_hypothesis": {"type": "string"},
                    "rationale": {"type": "string"},
                    "conclusion": {
                        "type": "object",
                        "properties": {
                            "root_cause": {"type": "string"},
                            "confidence": {"type": "integer"},
                            "evidence_summary": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "not_verified": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "recommendations": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                },
                "required": ["type"],
            },
        },
        "required": ["hypotheses", "next_action"],
    },
}


# ---------------------------------------------------------------------------
# Typed step (mirrors the tool schema)
# ---------------------------------------------------------------------------
@dataclass
class HypothesisStep:
    id: str
    description: str
    confidence: int
    status: Literal["active", "eliminated", "confirmed"]
    evidence_for: list[str]
    evidence_against: list[str]


@dataclass
class NextAction:
    type: Literal["command", "conclude", "need_info"]
    command: str | None = None
    tests_hypothesis: str | None = None
    rationale: str | None = None
    conclusion: dict[str, Any] | None = None


@dataclass
class InvestigationStep:
    hypotheses: list[HypothesisStep]
    next_action: NextAction
    reasoning: str = ""

    @classmethod
    def from_tool_input(cls, payload: dict[str, Any]) -> "InvestigationStep":
        hypotheses = [
            HypothesisStep(
                id=h["id"],
                description=h["description"],
                confidence=h["confidence"],
                status=h["status"],
                evidence_for=h.get("evidence_for", []),
                evidence_against=h.get("evidence_against", []),
            )
            for h in payload.get("hypotheses", [])
        ]
        action_payload = payload["next_action"]
        action = NextAction(
            type=action_payload["type"],
            command=action_payload.get("command"),
            tests_hypothesis=action_payload.get("tests_hypothesis"),
            rationale=action_payload.get("rationale"),
            conclusion=action_payload.get("conclusion"),
        )
        return cls(
            hypotheses=hypotheses,
            next_action=action,
            reasoning=payload.get("reasoning", ""),
        )


# ---------------------------------------------------------------------------
# Reasoner client
# ---------------------------------------------------------------------------
class ReasonerError(Exception):
    """Raised when Opus returns an unparseable or empty response."""


class Reasoner:
    """Opus-backed hypothesis engine.

    The conversation is stateless from the model's POV — the investigator
    rebuilds the message history each turn from its own state. This makes
    the loop replayable and easy to debug.
    """

    def __init__(
        self,
        client: "AsyncAnthropic | None" = None,
        model: str = REASONER_MODEL,
        max_tokens: int = 4096,
    ) -> None:
        if client is None:
            from anthropic import AsyncAnthropic  # lazy import
            client = AsyncAnthropic()
        self.client = client
        self.model = model
        self.max_tokens = max_tokens

    async def step(
        self, messages: list[dict[str, Any]]
    ) -> InvestigationStep:
        """Run one reasoning turn and return the structured step."""
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=REASONER_SYSTEM_PROMPT,
            tools=[INVESTIGATION_TOOL],
            tool_choice={"type": "tool", "name": "investigation_step"},
            messages=messages,
        )

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "investigation_step":
                return InvestigationStep.from_tool_input(block.input)

        raise ReasonerError(
            "Opus did not return an investigation_step tool call"
        )
