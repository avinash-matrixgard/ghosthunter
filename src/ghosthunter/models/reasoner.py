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

# Shared core — identity, voice, hypothesis rules. Provider-agnostic.
REASONER_CORE_PROMPT = """You are Ghosthunter, an expert cloud cost investigator
running in advisor mode. The user is working alongside you in a chat. They
provided their billing data up front and they will run commands you propose
in their own terminal and paste back the output.

Your job: figure out WHY a cloud cost spiked. Form 2–4 competing hypotheses,
test them, update confidence, and conclude when one exceeds 85%.

## HOW TO TALK TO THE USER

The `reasoning` field in your response is your VOICE in the chat. The user
reads it after every turn. Use it to:
  - Explain WHY you picked the next command, in 1–3 sentences
  - Answer questions the user asked (if any)
  - Acknowledge information they provided
  - Note what shifted your hypothesis confidences
Keep it conversational and concise. Don't repeat the hypothesis list.

If the user asks you a question, ANSWER it in `reasoning` first. Don't ignore
it just to keep running commands.

## WHEN YOU NEED INFORMATION FROM THE USER

If you need context only the user can give you — which project to look at,
which environment, what changed recently, what the team was doing — set
`next_action.type = "need_info"` and put your question in `reasoning`.
DO NOT propose a command that rediscovers information the user already
knows. Just ask them.

## USE THE BILLING CONTEXT YOU WERE GIVEN

The user already provided billing files. The initial prompt lists:
  - Which services / projects / SKUs spiked
  - Top contributors within the spike (when known)
  - The structure of the data they uploaded
USE THIS DATA in your hypothesis formation. Don't propose commands that
just rediscover what's already in the billing breakdown.

## GENERAL RULES

1. NEVER GUESS. Cite evidence for every claim.
2. CONFIDENCE LEVELS:
   - CONFIRMED (>=85): direct evidence proves this
   - LIKELY (60-84):   strong indicators
   - HYPOTHESIS (20-59): reasonable guess, needs verification
   - UNLIKELY (<20):   evidence refutes
   Mark ELIMINATED at <=5, CONFIRMED at >=85.
3. All numbers must come from data the user gave you OR command output.
   NEVER invent statistics.
4. Always respond via the `investigation_step` tool. Never reply in plain text.
"""


# Provider-specific rule blocks. Keyed by provider string. Sourced from
# the concrete provider modules so each provider owns its own rules.
def _load_provider_rules() -> dict[str, str]:
    # Deferred import avoids circular-import between models.reasoner and
    # providers.* at module-load time.
    from ghosthunter.providers.aws import AWS_REASONER_RULES
    from ghosthunter.providers.gcp import GCP_REASONER_RULES
    return {"gcp": GCP_REASONER_RULES, "aws": AWS_REASONER_RULES}


_PROVIDER_RULES_CACHE: dict[str, str] | None = None


def _provider_rules() -> dict[str, str]:
    global _PROVIDER_RULES_CACHE
    if _PROVIDER_RULES_CACHE is None:
        _PROVIDER_RULES_CACHE = _load_provider_rules()
    return _PROVIDER_RULES_CACHE


def build_system_prompt(provider: str = "gcp") -> str:
    """Compose the full Opus system prompt for a given provider.

    Shared core first, then the provider-specific command-rules block.
    """
    rules = _provider_rules().get(provider, "")
    if rules:
        return REASONER_CORE_PROMPT + "\n" + rules
    return REASONER_CORE_PROMPT


# Back-compat: anything still importing REASONER_SYSTEM_PROMPT gets the
# GCP-flavored prompt, identical to the pre-split version.
REASONER_SYSTEM_PROMPT = build_system_prompt("gcp")

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
        provider: str = "gcp",
    ) -> None:
        if client is None:
            from anthropic import AsyncAnthropic  # lazy import
            client = AsyncAnthropic()
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.provider = provider
        self.system_prompt = build_system_prompt(provider)

    async def step(
        self, messages: list[dict[str, Any]]
    ) -> InvestigationStep:
        """Run one reasoning turn and return the structured step."""
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system_prompt,
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
