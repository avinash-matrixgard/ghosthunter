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
    ReasonerSchemaError,
)

# Max times we'll ask Opus to re-emit its tool_use before aborting.
# ReasonerSchemaError typically means Opus returned hypotheses as strings
# or dropped next_action.type; a single corrective nudge usually fixes it.
_MAX_SCHEMA_RETRIES_PER_INVESTIGATION = 2
from ghosthunter.providers.gcp import (
    CommandRejectedError,
    CommandResult,
    CommandTimeoutError,
    CostSpike,
    GCPProvider,
)

# Optional advisor-mode exceptions; imported lazily so a missing rich
# install doesn't break active mode. All subclass GCPProviderError so
# we can catch them generically below.
try:
    from ghosthunter.providers.advisor import (
        AdvisorAborted,
        AdvisorNote,
        AdvisorSkipped,
    )
except ImportError:  # pragma: no cover
    AdvisorAborted = AdvisorNote = AdvisorSkipped = None  # type: ignore
from ghosthunter.security.prompt_sanitizer import (
    sanitize_for_prompt,
    wrap_as_untrusted_output,
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

# Memory hook signature: called synchronously when a durable fact arrives
# mid-investigation. kind is "need_info_answer" or "user_note". The chat
# session implementation wires this to the palace.
MemoryHook = Callable[[str, str], None]  # (kind, text) → None


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
        "reasoning",
        "hypotheses_updated",
        "opus_asks",
        "command_proposed",
        "command_blocked",
        "command_approved",
        "command_rejected_by_user",
        "command_executed",
        "compressing",  # Sonnet is turning command output into evidence
        "evidence_added",
        "user_note",
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
        memory_hook: MemoryHook | None = None,
    ) -> None:
        self.provider = provider
        self.reasoner = reasoner
        self.executor = executor
        self.validator = validator or SecurityValidator()
        self.approval_hook = approval_hook or _default_auto_reject
        self.event_hook = event_hook
        self.budget = budget or Budget()
        # Called synchronously with (kind, text) when a durable fact should
        # be persisted to memory. kind ∈ {"need_info_answer", "user_note"}.
        # None = memory disabled.
        self.memory_hook = memory_hook

        self.hypotheses = HypothesisManager()
        self.evidence = EvidenceChain()
        self._messages: list[dict[str, Any]] = []
        self._last_tool_use_id: str | None = None  # for round-tripping tool_use blocks

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------
    async def investigate(
        self,
        spike: CostSpike,
        additional_context: str | None = None,
    ) -> InvestigationResult:
        """Run an investigation.

        `additional_context` is appended to the initial user message and
        gives Opus information the chat session knows but the spike object
        doesn't (e.g. which billing files were loaded, joinability caveats).
        """
        await self._emit("spike_selected", {"spike": spike})

        self._messages = [
            {
                "role": "user",
                "content": _build_initial_prompt(spike, additional_context),
            }
        ]

        conclusion: dict[str, Any] | None = None
        aborted_reason: str | None = None
        schema_retries_used = 0

        while True:
            await self._emit("step_started", {})
            try:
                step = await self.reasoner.step(self._messages)
            except ReasonerSchemaError as exc:
                # Opus returned a malformed tool_use payload. Nudge it
                # with a concrete correction and retry — this recovers
                # ~95% of shape slips we've seen in the wild. Only after
                # _MAX_SCHEMA_RETRIES_PER_INVESTIGATION do we abort.
                if schema_retries_used < _MAX_SCHEMA_RETRIES_PER_INVESTIGATION:
                    schema_retries_used += 1
                    await self._emit(
                        "reasoner_schema_retry",
                        {
                            "detail": exc.detail,
                            "attempt": schema_retries_used,
                        },
                    )
                    self._messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your previous response was malformed: "
                                f"{exc.detail}. Please respond again via "
                                "the `investigation_step` tool. Hypotheses "
                                "MUST be a list of objects, each with "
                                "`id`, `description`, `confidence`, and "
                                "`status`. `next_action` MUST be an object "
                                "with `type` in (command, conclude, "
                                "need_info)."
                            ),
                        }
                    )
                    continue
                aborted_reason = f"reasoner schema error (retries exhausted): {exc.detail}"
                await self._emit("aborted", {"reason": aborted_reason})
                break
            except Exception as exc:
                aborted_reason = f"reasoner error: {exc}"
                await self._emit("aborted", {"reason": aborted_reason})
                break

            self._adopt_hypothesis_snapshot(step)
            await self._emit(
                "hypotheses_updated",
                {"hypotheses": [h.__dict__ for h in self.hypotheses.all()]},
            )
            if step.reasoning:
                await self._emit("reasoning", {"text": step.reasoning})

            action = step.next_action

            if action.type == "conclude":
                conclusion = action.conclusion or {}
                await self._emit("concluded", {"conclusion": conclusion})
                break

            if action.type == "need_info":
                # Opus is asking the user a clarifying question. The
                # question is in step.reasoning. If the provider supports
                # interactive prompting (advisor mode), ask and inject
                # the answer. Otherwise abort.
                question = step.reasoning or "Opus needs more information."
                if hasattr(self.provider, "ask_user"):
                    await self._emit("opus_asks", {"question": question})
                    try:
                        answer = await self.provider.ask_user(question)  # type: ignore[attr-defined]
                    except Exception as exc:
                        if AdvisorAborted is not None and isinstance(exc, AdvisorAborted):
                            aborted_reason = "user aborted"
                            await self._emit("aborted", {"reason": aborted_reason})
                            break
                        raise
                    self._messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"My answer: {answer}\n\n"
                                "Use this to update your hypotheses and "
                                "propose your next step."
                            ),
                        }
                    )
                    # Persist the answer as a durable fact if memory is wired.
                    # Skip the "I don't know" sentinel from /skip.
                    if (
                        self.memory_hook is not None
                        and answer
                        and "declined to answer" not in answer
                    ):
                        try:
                            self.memory_hook("need_info_answer", answer)
                        except Exception:
                            pass
                    continue
                else:
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
    async def _handle_command(self, action: NextAction) -> Literal["continue", "abort"]:
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
            self._append_tool_feedback(command, f"BLOCKED by semantic validator: {exc}")
            return "continue"

        if not semantic.approved:
            await self._emit(
                "command_blocked",
                {"command": command, "layer": "L6", "reason": semantic.reason},
            )
            self._append_tool_feedback(command, f"BLOCKED by semantic check: {semantic.reason}")
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
            self._append_tool_feedback(command, "REJECTED by user — propose a different approach")
            return "continue"

        await self._emit("command_approved", {"command": command})

        # Layer 7: sandboxed execution (or advisor-mode print-and-wait)
        try:
            result = await self.provider.execute_command(command)
        except (CommandRejectedError, CommandTimeoutError) as exc:
            await self._emit(
                "command_blocked",
                {"command": command, "layer": "L7", "reason": str(exc)},
            )
            self._append_tool_feedback(command, f"EXECUTION FAILED: {exc}")
            return "continue"
        except Exception as exc:
            # Advisor mode signals user intent via custom exceptions.
            if AdvisorAborted is not None and isinstance(exc, AdvisorAborted):
                return "abort"
            if AdvisorSkipped is not None and isinstance(exc, AdvisorSkipped):
                await self._emit("command_rejected_by_user", {"command": command})
                self._append_tool_feedback(
                    command,
                    "SKIPPED by user — propose a different command",
                )
                return "continue"
            if AdvisorNote is not None and isinstance(exc, AdvisorNote):
                await self._emit("user_note", {"command": command, "note": exc.note})
                self._append_tool_feedback(
                    command,
                    f"SKIPPED by user. The user added this note:\n\n"
                    f"{exc.note}\n\n"
                    "Update your hypotheses with this new information and "
                    "propose your next command (or conclude if appropriate).",
                )
                if self.memory_hook is not None:
                    try:
                        self.memory_hook("user_note", exc.note)
                    except Exception:
                        pass
                return "continue"
            raise

        self.budget.commands_used += 1
        self.budget.seconds_used += result.duration_seconds
        await self._emit("command_executed", {"result": result})

        # Compress and turn into evidence. Emit a separate event so the
        # renderer can swap the spinner from "validating" / "executed" into
        # "compressing" with a byte-count hint.
        compress_input = _format_for_compression(result)
        await self._emit(
            "compressing",
            {"command": command, "bytes": len(compress_input)},
        )
        try:
            summary = await self.executor.compress(
                command=command,
                output=compress_input,
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
def _build_initial_prompt(spike: CostSpike, additional_context: str | None = None) -> str:
    daily = json.dumps(spike.daily_breakdown[-14:], indent=2) if spike.daily_breakdown else "(none)"

    # Descriptions come from the billing file's ChargeDescription column
    # (FOCUS 1.0) or lineItem/LineItemDescription (AWS CUR) and translate
    # opaque SKU / UsageType codes into human language. If the file
    # didn't have such a column, ``contributor_descriptions`` is empty
    # and we fall back to showing just the ID.
    descriptions = getattr(spike, "contributor_descriptions", {}) or {}

    contributors_block = ""
    if spike.top_contributors:
        lines: list[str] = []
        for dim, items in spike.top_contributors.items():
            if not items:
                continue
            lines.append(f"\nTop {dim}s in current period (driving the spike):")
            for name, cost in items:
                desc = descriptions.get(f"{dim}:{name}")
                if desc:
                    lines.append(f"  - {name}: ${cost:,.2f}  —  {desc}")
                else:
                    lines.append(f"  - {name}: ${cost:,.2f}")
        contributors_block = "\n".join(lines)

    inference_block = ""
    likely_homes = getattr(spike, "likely_homes", None) or []
    if likely_homes:
        grp = getattr(spike, "grouping", "service")
        if grp == "service":
            header = (
                "\n## Likely project home(s) — INFERRED from billing totals\n"
                "These projects most likely host this service. The Console "
                "exports cannot be joined directly, but name matches, "
                "percent-change correlations, and total magnitudes give "
                "strong signals. USE THIS INFERENCE before asking the user "
                "where the spike is."
            )
        else:
            header = (
                "\n## Likely service contents — INFERRED from billing totals\n"
                "These services most likely run inside this project. Use this "
                "to focus your hypotheses."
            )
        lines = [header]
        for name, score, reason in likely_homes:
            lines.append(f"  - {name}  (confidence score {score})")
            lines.append(f"      reason: {reason}")
        inference_block = "\n".join(lines)

    grouping = getattr(spike, "grouping", "service")
    grouping_label = {
        "service": "Service",
        "project": "Project",
        "sku": "SKU",
        "location": "Location",
    }.get(grouping, "Spike")

    context_block = ""
    if additional_context:
        context_block = f"\n## Context provided up front\n{additional_context}\n"

    return (
        "A cost spike has been detected. Investigate the root cause.\n"
        f"{context_block}\n"
        f"## Spike details\n"
        f"{grouping_label}: {spike.service}\n"
        f"Current period cost: ${spike.current_cost:,.2f}\n"
        f"Previous period cost: ${spike.previous_cost:,.2f}\n"
        f"Change: {spike.change_percent:+.1f}% "
        f"(${spike.absolute_change:+,.2f})\n\n"
        f"Recent daily breakdown:\n{daily}"
        f"{contributors_block}"
        f"{inference_block}\n\n"
        "## What to do now\n"
        "Use the breakdown above to form your initial hypotheses — it tells "
        "you exactly where the cost moved. If you need information that's "
        "NOT in the data above (like which project a service is in, or what "
        "changed recently), ASK THE USER via next_action.type=need_info — "
        "do not run a command to find it.\n\n"
        "Form 2–4 competing hypotheses with confidence scores, then either "
        "ask a clarifying question OR propose the first read-only command "
        "that would best discriminate between them. Respond via the "
        "`investigation_step` tool."
    )


def _format_for_compression(result: CommandResult) -> str:
    """Format a CommandResult for the compression LLM with prompt-injection defense.

    Pasted command output is untrusted (per ghosthunter#5, Apr 29 2026 audit).
    Two layers of defense before the text reaches Sonnet:

      1. ``sanitize_for_prompt`` strips known prompt-injection markers
         ("ignore previous instructions", role-redefinition phrases, etc).
      2. ``wrap_as_untrusted_output`` wraps the result in a defensive
         ``<command_output>`` frame telling the LLM not to follow embedded
         instructions.

    Both layers are best-effort. The deterministic command validator
    (Layers 1–4) still holds regardless — pasted output cannot be tricked
    into executing dangerous commands. This defense exists to prevent
    misdirected investigations (wasted budget on bad hypotheses).
    """
    sanitized_stdout = sanitize_for_prompt(result.stdout).sanitized
    sanitized_stderr = sanitize_for_prompt(result.stderr).sanitized

    parts = [
        f"exit_code: {result.exit_code}",
        f"duration_seconds: {result.duration_seconds:.2f}",
    ]
    if result.truncated:
        parts.append("note: stdout was truncated by the sandbox")
    if sanitized_stderr.strip():
        parts.append(f"stderr:\n{sanitized_stderr}")
    parts.append(f"stdout:\n{sanitized_stdout}")

    inner = "\n\n".join(parts)
    return wrap_as_untrusted_output(inner)


async def _default_auto_reject(_: PendingCommand) -> ApprovalDecision:
    """Safe default if no approval hook is wired up. Refuses everything."""
    return "reject"
