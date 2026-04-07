"""Main investigation loop.

Flow:
    1. Fetch billing data, pick the largest spike to investigate.
    2. Build the initial user message describing the spike.
    3. Loop:
        a. Reasoner (Opus) returns hypotheses + next_action.
        b. If next_action is `conclude`, stop and return the conclusion.
        c. If next_action is `command`:
             - Static validator (Layers 1–4) — refuse if blocked.
             - Sonnet semantic check (Layer 6) — refuse if rejected.
             - Ask the user to approve (supervised mode, v1.0).
             - GCPProvider executes (Layer 7 sandbox).
             - Sonnet compresses output → Evidence.
             - Append to evidence chain + apply to HypothesisManager.
        d. Append a tool_result message and loop.
    4. Stop when we conclude, run out of budget, or the user aborts.

This file is the orchestration layer. All the hard logic (security, models,
hypothesis math) lives in the modules it imports.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from ghosthunter.evidence import Evidence, EvidenceChain
from ghosthunter.hypothesis import Hypothesis, HypothesisManager
from ghosthunter.models.executor import Executor, SemanticResult
from ghosthunter.models.reasoner import (
    InvestigationStep,
    NextAction,
    Reasoner,
)
from ghosthunter.providers.gcp import (
    CommandRejectedError,
    CommandResult,
    CommandTimeoutError,
    CostSpike,
    GCPProvider,
)
from ghosthunter.security.validator import SecurityValidator, ValidationResult


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------
@dataclass
class Budget:
    """Layer 5: hard caps on a single investigation."""
    max_commands: int = 15
    max_cost_usd: float = 1.0
    max_seconds: float = 600.0  # 10 minutes

    commands_used: int = 0
    cost_used_usd: float = 0.0
    seconds_used: float = 0.0

    def has_room_for_command(self) -> bool:
        return (
            self.commands_used < self.max_commands
            and self.cost_used_usd < self.max_cost_usd
            and self.seconds_used < self.max_seconds
        )

    def reason_exhausted(self) -> str:
        if self.commands_used >= self.max_commands:
            return f"command budget exhausted ({self.max_commands})"
        if self.cost_used_usd >= self.max_cost_usd:
            return f"cost budget exhausted (${self.max_cost_usd:.2f})"
        if self.seconds_used >= self.max_seconds:
            return f"time budget exhausted ({self.max_seconds:.0f}s)"
        return "unknown"


# ---------------------------------------------------------------------------
# Approval & event hooks
# ---------------------------------------------------------------------------
ApprovalDecision = Literal["approve", "reject", "abort"]
ApprovalHook = Callable[["PendingCommand"], Awaitable[ApprovalDecision]]
EventHook = Callable[["InvestigationEvent"], Awaitable[None]]


@dataclass
class PendingCommand:
    """A command awaiting human approval."""
    command: str
    tests_hypothesis: str | None
    rationale: str | None
    static_check: ValidationResult
    semantic_check: SemanticResult


@dataclass
class InvestigationEvent:
    """Anything the UI / audit log might want to render."""
    kind: Literal[
        "spike_selected",
        "step_started",
        "hypotheses_updated",
        "command_proposed",
        "command_blocked",
        "command_approved",
        "command_rejected_by_user",
        "command_executed",
        "evidence_added",
        "concluded",
        "aborted",
    ]
    payload: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Final result
# ---------------------------------------------------------------------------
@dataclass
class InvestigationResult:
    spike: CostSpike
    hypotheses: list[Hypothesis]
    evidence: list[Evidence]
    conclusion: dict[str, Any] | None
    aborted_reason: str | None
    budget: Budget

    @property
    def succeeded(self) -> bool:
        return self.conclusion is not None and self.aborted_reason is None


# ---------------------------------------------------------------------------
# Investigator
# ---------------------------------------------------------------------------
class Investigator:
    """Drive a single investigation from spike → conclusion."""

    def __init__(
        self,
        provider: GCPProvider,
        reasoner: Reasoner,
        executor: Executor,
        validator: SecurityValidator | None = None,
        approval_hook: ApprovalHook | None = None,
        event_hook: EventHook | None = None,
        budget: Budget | None = None,
    ) -> None:
        self.provider = provider
        self.reasoner = reasoner
        self.executor = executor
        self.validator = validator or SecurityValidator()
        self.approval_hook = approval_hook or _default_auto_reject
        self.event_hook = event_hook
        self.budget = budget or Budget()

        self.hypotheses = HypothesisManager()
        self.evidence = EvidenceChain()
        self._messages: list[dict[str, Any]] = []
        self._last_tool_use_id: str | None = None  # for round-tripping tool_use blocks

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------
    async def investigate(self, spike: CostSpike) -> InvestigationResult:
        await self._emit("spike_selected", {"spike": spike})

        self._messages = [
            {"role": "user", "content": _build_initial_prompt(spike)}
        ]

        conclusion: dict[str, Any] | None = None
        aborted_reason: str | None = None

        while True:
            await self._emit("step_started", {})
            try:
                step = await self.reasoner.step(self._messages)
            except Exception as exc:
                aborted_reason = f"reasoner error: {exc}"
                await self._emit("aborted", {"reason": aborted_reason})
                break

            self._adopt_hypothesis_snapshot(step)
            await self._emit(
                "hypotheses_updated",
                {"hypotheses": [h.__dict__ for h in self.hypotheses.all()]},
            )

            action = step.next_action

            if action.type == "conclude":
                conclusion = action.conclusion or {}
                await self._emit("concluded", {"conclusion": conclusion})
                break

            if action.type == "need_info":
                # v1.0 has no out-of-band info channel — treat as abort.
                aborted_reason = "reasoner needs info we cannot provide"
                await self._emit("aborted", {"reason": aborted_reason})
                break

            if action.type != "command" or not action.command:
                aborted_reason = f"unexpected next_action: {action.type}"
                await self._emit("aborted", {"reason": aborted_reason})
                break

            if not self.budget.has_room_for_command():
                aborted_reason = self.budget.reason_exhausted()
                await self._emit("aborted", {"reason": aborted_reason})
                break

            # ---- Command pipeline ----
            outcome = await self._handle_command(action)
            if outcome == "abort":
                aborted_reason = "user aborted investigation"
                await self._emit("aborted", {"reason": aborted_reason})
                break

            # Continue the loop — next reasoner.step() will see the new
            # tool_result message we just appended.

        return InvestigationResult(
            spike=spike,
            hypotheses=self.hypotheses.all(),
            evidence=list(self.evidence),
            conclusion=conclusion,
            aborted_reason=aborted_reason,
            budget=self.budget,
        )

    # ------------------------------------------------------------------
    # Command pipeline (one command from proposal to evidence)
    # ------------------------------------------------------------------
    async def _handle_command(
        self, action: NextAction
    ) -> Literal["continue", "abort"]:
        assert action.command  # checked by caller
        command = action.command

        # Layer 1–4: static validator
        static_check = self.validator.is_allowed(command)
        if not static_check.allowed:
            await self._emit(
                "command_blocked",
                {
                    "command": command,
                    "layer": static_check.layer,
                    "reason": static_check.reason,
                },
            )
            self._append_tool_feedback(
                command,
                f"BLOCKED by static validator ({static_check.layer}): {static_check.reason}",
            )
            return "continue"

        # Layer 6: Sonnet semantic check
        try:
            semantic = await self.executor.semantic_validate(command)
        except Exception as exc:
            await self._emit(
                "command_blocked",
                {"command": command, "layer": "L6", "reason": f"executor error: {exc}"},
            )
            self._append_tool_feedback(
                command, f"BLOCKED by semantic validator: {exc}"
            )
            return "continue"

        if not semantic.approved:
            await self._emit(
                "command_blocked",
                {"command": command, "layer": "L6", "reason": semantic.reason},
            )
            self._append_tool_feedback(
                command, f"BLOCKED by semantic check: {semantic.reason}"
            )
            return "continue"

        # User approval (supervised mode)
        pending = PendingCommand(
            command=command,
            tests_hypothesis=action.tests_hypothesis,
            rationale=action.rationale,
            static_check=static_check,
            semantic_check=semantic,
        )
        await self._emit("command_proposed", {"pending": pending})
        decision = await self.approval_hook(pending)

        if decision == "abort":
            return "abort"
        if decision == "reject":
            await self._emit("command_rejected_by_user", {"command": command})
            self._append_tool_feedback(
                command, "REJECTED by user — propose a different approach"
            )
            return "continue"

        await self._emit("command_approved", {"command": command})

        # Layer 7: sandboxed execution
        try:
            result = await self.provider.execute_command(command)
        except (CommandRejectedError, CommandTimeoutError) as exc:
            await self._emit(
                "command_blocked",
                {"command": command, "layer": "L7", "reason": str(exc)},
            )
            self._append_tool_feedback(command, f"EXECUTION FAILED: {exc}")
            return "continue"

        self.budget.commands_used += 1
        self.budget.seconds_used += result.duration_seconds
        await self._emit("command_executed", {"result": result})

        # Compress and turn into evidence
        try:
            summary = await self.executor.compress(
                command=command,
                output=_format_for_compression(result),
                investigation_target=self._target_summary(),
                hypotheses=[h.description for h in self.hypotheses.all()],
            )
        except Exception as exc:
            self._append_tool_feedback(
                command, f"COMPRESSION FAILED: {exc} — raw output suppressed"
            )
            return "continue"

        evidence = self.evidence.add(summary=summary, command=command)
        await self._emit("evidence_added", {"evidence": evidence})

        # Feed the compressed summary back to Opus as a tool_result.
        # (Opus assigns relations on the *next* turn via its hypothesis snapshot.)
        self._append_tool_feedback(
            command,
            f"Evidence {evidence.id}:\n{summary}",
        )
        return "continue"

    # ------------------------------------------------------------------
    # Message bookkeeping
    # ------------------------------------------------------------------
    def _adopt_hypothesis_snapshot(self, step: InvestigationStep) -> None:
        """Replace local hypothesis state with whatever Opus just returned."""
        hypotheses = [
            Hypothesis(
                id=h.id,
                description=h.description,
                confidence=h.confidence,
                evidence_for=list(h.evidence_for),
                evidence_against=list(h.evidence_against),
            )
            for h in step.hypotheses
        ]
        self.hypotheses.replace_all(hypotheses)

    def _append_tool_feedback(self, command: str, body: str) -> None:
        """Inject the result of a command back into the conversation.

        Opus reads these as user messages because we're not using a
        formal tool_use round-trip for command execution — Opus proposes
        commands via the `investigation_step` tool, and the *result* of
        running them is fed back as a user message describing what
        happened. This keeps the loop simple.
        """
        self._messages.append(
            {
                "role": "user",
                "content": (
                    f"Result of `{command}`:\n{body}\n\n"
                    "Update your hypotheses with this evidence and propose the next step."
                ),
            }
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _target_summary(self) -> str:
        leading = self.hypotheses.leading()
        if leading:
            return f"Leading hypothesis: {leading.description} ({leading.confidence}%)"
        return "Initial spike investigation"

    async def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.event_hook is None:
            return
        await self.event_hook(InvestigationEvent(kind=kind, payload=payload))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _build_initial_prompt(spike: CostSpike) -> str:
    daily = json.dumps(spike.daily_breakdown[-14:], indent=2) if spike.daily_breakdown else "(none)"
    return (
        "A cost spike has been detected on this GCP project. "
        "Investigate the root cause.\n\n"
        f"Service: {spike.service}\n"
        f"Current period cost: ${spike.current_cost:,.2f}\n"
        f"Previous period cost: ${spike.previous_cost:,.2f}\n"
        f"Change: {spike.change_percent:+.1f}% "
        f"(${spike.absolute_change:+,.2f})\n\n"
        f"Recent daily breakdown:\n{daily}\n\n"
        "Form 2–4 competing hypotheses with confidence scores, then propose "
        "the first read-only command that would best discriminate between them. "
        "Respond via the `investigation_step` tool."
    )


def _format_for_compression(result: CommandResult) -> str:
    parts = [
        f"exit_code: {result.exit_code}",
        f"duration_seconds: {result.duration_seconds:.2f}",
    ]
    if result.truncated:
        parts.append("note: stdout was truncated by the sandbox")
    if result.stderr.strip():
        parts.append(f"stderr:\n{result.stderr}")
    parts.append(f"stdout:\n{result.stdout}")
    return "\n\n".join(parts)


async def _default_auto_reject(_: PendingCommand) -> ApprovalDecision:
    """Safe default if no approval hook is wired up. Refuses everything."""
    return "reject"
