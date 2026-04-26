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

## HOW TO WRITE RECOMMENDATIONS WHEN YOU CONCLUDE

When you set `next_action.type = "conclude"`, the `recommendations` array
is where the user looks to decide what to do next. Most users don't
read prose — they scan for "what do I run?". Give them concrete actions.

Each recommendation SHOULD be an object with these fields:
  - `urgency`: one of "immediate" | "this_week" | "this_month" | "monitoring"
      - "immediate"  — do it in the next hour; it's currently costing
                       money or leaking data
      - "this_week"  — schedule it on this sprint; harden posture
      - "this_month" — architectural change worth doing but not urgent
      - "monitoring" — a permanent alert / budget / dashboard to set up
                       so this type of issue surfaces earlier next time
  - `description`: one crisp sentence about WHAT this does and WHY.
      No fluff. Read like a change ticket summary.
  - `command`: the exact shell command to run, when a command can make
      the change. Use the same provider CLI conventions you've been
      using in commands this turn. If the fix is a console click,
      policy decision, or vendor call, OMIT `command` rather than
      invent one.
  - `verification`: the exact command (or clear check) that proves the
      fix worked. Same format as `command`. OMIT if no programmatic
      verification is possible.

Use prose-string recommendations (no object structure) ONLY for advice
that genuinely has no actionable command form — things like "talk to
the owning team" or "open a ticket with your vendor."

Prefer object form heavily. A good conclusion has 3–6 recommendations,
at least half of them with `command` populated.

DO NOT invent commands. If you're not certain of the exact syntax for a
fix, describe the action in `description` and leave `command` empty —
that's honest. The user would rather copy your description and search
the docs than paste your guess and have it fail.

## WHEN THE USER CAN'T RUN COMMANDS OR FIND MORE DATA

If the user explicitly tells you they have no access to run commands, no
access to the cloud console, no way to look up more data, or asks you to
"work with what you have" — STOP asking questions. They have already
given you everything they can give you. Your job at that point is:

1. Re-read the billing-context block carefully. Look at `top_contributors`,
   `contributor_descriptions` (SKU IDs translated to human descriptions
   like "g5.4xlarge Instance Hour"), top accounts, top regions, daily
   breakdown. The answer is usually right there.
2. Consolidate your hypotheses into a single best-guess root cause based
   ONLY on the billing data you already have.
3. Set `next_action.type = "conclude"` with:
   - a `root_cause` that names the most likely cause from the billing
     signal (e.g. "GPU-heavy EC2 workload in us-east-1 account X — the
     top SKU maps to g5.4xlarge on-demand instance hours"),
   - a `confidence` that honestly reflects your uncertainty (50–75 is
     appropriate when you couldn't verify at the resource level),
   - `not_verified` listing what you could NOT confirm without access
     (specific instance IDs, when they launched, who owns them, etc.),
   - `recommendations` that the user CAN act on from a billing console
     (filter by SKU, check budgets, contact the account owner).

Do NOT keep asking variations of "can you look it up?" after the user
has already said they can't. That is the single most frustrating failure
mode for advisor-mode users.

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
                            # Each recommendation can be either a plain
                            # prose string (back-compat, any existing
                            # caller) or a structured object so the CLI
                            # can render the command in a paste-safe
                            # block and show a verification step
                            # alongside. Object form is preferred.
                            "recommendations": {
                                "type": "array",
                                "items": {
                                    "oneOf": [
                                        {"type": "string"},
                                        {
                                            "type": "object",
                                            "properties": {
                                                "urgency": {
                                                    "type": "string",
                                                    "enum": [
                                                        "immediate",
                                                        "this_week",
                                                        "this_month",
                                                        "monitoring",
                                                    ],
                                                },
                                                "description": {"type": "string"},
                                                "command": {"type": "string"},
                                                "verification": {"type": "string"},
                                            },
                                            "required": [
                                                "urgency",
                                                "description",
                                            ],
                                        },
                                    ]
                                },
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
        """Parse Opus's tool_use input defensively.

        Opus *usually* returns the exact JSON shape we asked for via the
        ``INVESTIGATION_TOOL`` schema, but under long-context pressure it
        occasionally slips — returns hypotheses as plain strings instead
        of objects, drops ``next_action.type``, etc. The pre-v1.0.2
        version crashed with an opaque ``string indices must be integers``
        TypeError and aborted the whole investigation.

        We now:
          1. Coerce minor shape slips (string hypothesis → dict with the
             string as description; missing confidence → 50; etc.).
          2. Raise a typed ``ReasonerSchemaError`` for un-coerceable
             shapes so the investigator can retry once with a nudge.
        """
        if not isinstance(payload, dict):
            raise ReasonerSchemaError(f"payload is not a dict (got {type(payload).__name__})")

        raw_hypotheses = payload.get("hypotheses", [])
        if not isinstance(raw_hypotheses, list):
            raise ReasonerSchemaError(
                f"hypotheses is not a list (got {type(raw_hypotheses).__name__})",
                raw_payload=payload,
            )

        hypotheses: list[HypothesisStep] = []
        for idx, raw in enumerate(raw_hypotheses):
            coerced = _coerce_hypothesis(raw, idx)
            if coerced is not None:
                hypotheses.append(coerced)

        # Distinguish two cases:
        #   - raw list was empty → legit (e.g. Opus concluding with no
        #     hypotheses to carry forward); preserve original behaviour.
        #   - raw list had items but *all* were unsalvageable → real
        #     schema slip; raise so the investigator can retry.
        if raw_hypotheses and not hypotheses:
            raise ReasonerSchemaError(
                f"no valid hypotheses could be parsed from {len(raw_hypotheses)} items",
                raw_payload=payload,
            )

        reasoning_raw = payload.get("reasoning", "")
        reasoning = reasoning_raw if isinstance(reasoning_raw, str) else str(reasoning_raw or "")

        action = _coerce_next_action(
            payload.get("next_action"),
            fallback_reasoning=reasoning,
        )

        return cls(
            hypotheses=hypotheses,
            next_action=action,
            reasoning=reasoning,
        )


# ---------------------------------------------------------------------------
# Shape coercion helpers
# ---------------------------------------------------------------------------
def _coerce_hypothesis(raw: Any, idx: int) -> "HypothesisStep | None":
    """Best-effort parse of one hypothesis entry.

    Accepts:
      - The canonical dict shape (``id``, ``description``, ``confidence``,
        ``status``, optional evidence lists).
      - A bare string — treated as the description; other fields filled
        with sensible defaults. This is the most common Opus slip-up.

    Returns None for un-salvageable entries (e.g., non-string, non-dict,
    or a dict without any description text). The caller filters Nones.
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        return HypothesisStep(
            id=f"H{idx + 1}",
            description=text,
            confidence=50,
            status="active",
            evidence_for=[],
            evidence_against=[],
        )

    if not isinstance(raw, dict):
        return None

    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        # Description is load-bearing — without it the hypothesis is useless.
        return None

    hid = raw.get("id") or f"H{idx + 1}"
    confidence_raw = raw.get("confidence", 50)
    try:
        confidence = int(confidence_raw)
    except (TypeError, ValueError):
        confidence = 50
    confidence = max(0, min(100, confidence))

    status = raw.get("status", "active")
    if status not in ("active", "eliminated", "confirmed"):
        status = "active"

    evidence_for = raw.get("evidence_for", []) or []
    evidence_against = raw.get("evidence_against", []) or []
    if not isinstance(evidence_for, list):
        evidence_for = []
    if not isinstance(evidence_against, list):
        evidence_against = []

    return HypothesisStep(
        id=str(hid),
        description=description,
        confidence=confidence,
        status=status,  # type: ignore[arg-type]
        evidence_for=[str(e) for e in evidence_for],
        evidence_against=[str(e) for e in evidence_against],
    )


def _coerce_next_action(raw: Any, *, fallback_reasoning: str = "") -> NextAction:
    """Best-effort parse of ``next_action``.

    If Opus dropped or corrupted the field, fall back to a ``need_info``
    action carrying the reasoning text as rationale — that way the
    investigator can still surface the model's prose to the user rather
    than bailing out entirely.
    """
    if not isinstance(raw, dict):
        return NextAction(
            type="need_info",
            rationale=fallback_reasoning or "(Opus returned a malformed next_action)",
        )

    action_type = raw.get("type")
    if action_type not in ("command", "conclude", "need_info"):
        return NextAction(
            type="need_info",
            rationale=(
                fallback_reasoning or f"(Opus returned unknown next_action.type={action_type!r})"
            ),
        )

    conclusion = raw.get("conclusion")
    if conclusion is not None and not isinstance(conclusion, dict):
        conclusion = None

    return NextAction(
        type=action_type,  # type: ignore[arg-type]
        command=raw.get("command") if isinstance(raw.get("command"), str) else None,
        tests_hypothesis=raw.get("tests_hypothesis")
        if isinstance(raw.get("tests_hypothesis"), str)
        else None,
        rationale=raw.get("rationale") if isinstance(raw.get("rationale"), str) else None,
        conclusion=conclusion,
    )


# ---------------------------------------------------------------------------
# Reasoner client
# ---------------------------------------------------------------------------
class ReasonerError(Exception):
    """Raised when Opus returns an unparseable or empty response."""


class ReasonerSchemaError(ReasonerError):
    """Raised when Opus's tool_use payload has the wrong shape.

    Separated from the generic ``ReasonerError`` so the investigator can
    catch *just* this case and retry with a corrective nudge rather than
    aborting the whole investigation. See `InvestigationStep.from_tool_input`.

    Attributes:
        detail: Human-readable description of which shape invariant failed.
        raw_payload: The offending payload (or None), for diagnostics.
    """

    def __init__(self, detail: str, raw_payload: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.raw_payload = raw_payload


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

    async def step(self, messages: list[dict[str, Any]]) -> InvestigationStep:
        """Run one reasoning turn and return the structured step.

        Transient Anthropic API failures (429 rate limit, 529 overloaded,
        5xx server, network blips) are retried with exponential backoff
        inside ``call_with_retry``. Terminal failures raise a typed
        ``ModelAPIError`` subclass with an actionable hint — see
        ``models/_api_retry.py``.
        """
        from ghosthunter.models._api_retry import call_with_retry

        async def _do_call():
            return await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self.system_prompt,
                tools=[INVESTIGATION_TOOL],
                tool_choice={"type": "tool", "name": "investigation_step"},
                messages=messages,
            )

        response = await call_with_retry(_do_call, op_name="Opus reasoning")

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "investigation_step":
                return InvestigationStep.from_tool_input(block.input)

        raise ReasonerError("Opus did not return an investigation_step tool call")
