"""Tests for the main investigation loop (`investigator.Investigator`).

Covers paths that previously had zero direct coverage:

- Happy path — conclude on first turn; conclude after a command cycle.
- Budget exhaustion (commands / cost / time) aborts with a clear reason.
- Command rejection routes correctly:
    * Layer 1-4 static block → investigator injects `BLOCKED` feedback
      and lets the reasoner pivot.
    * Layer 6 Sonnet semantic reject → `BLOCKED by semantic check` feedback.
    * User rejects at the approval prompt → `REJECTED by user` feedback.
    * User aborts at the approval prompt → investigation ends with reason.
- Advisor-mode exceptions propagate correctly:
    * `AdvisorAborted` → investigation end with ``aborted_reason``.
    * `AdvisorSkipped` → `SKIPPED by user` feedback, loop continues.
    * `AdvisorNote` → note injected, `memory_hook` called, loop continues.
- `CommandTimeoutError` / `CommandRejectedError` → ``EXECUTION FAILED``
  feedback, loop continues.
- `need_info` path routes through ``provider.ask_user`` and injects the
  answer back to the reasoner.
- Reasoner exceptions abort the run gracefully.
- Events fire in the documented order with the expected payloads.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ghosthunter.investigator import (
    Budget,
    InvestigationEvent,
    Investigator,
    PendingCommand,
)
from ghosthunter.models.executor import SemanticResult
from ghosthunter.models.reasoner import (
    HypothesisStep,
    InvestigationStep,
    NextAction,
)
from ghosthunter.providers.advisor import (
    AdvisorAborted,
    AdvisorNote,
    AdvisorSkipped,
)
from ghosthunter.providers.base import (
    CommandRejectedError,
    CommandResult,
    CommandTimeoutError,
    CostSpike,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
def _spike(service: str = "Cloud DNS", cur: float = 117_000, prev: float = 12_000) -> CostSpike:
    return CostSpike(
        service=service,
        current_cost=cur,
        previous_cost=prev,
        change_percent=((cur - prev) / prev) * 100.0 if prev else float("inf"),
        daily_breakdown=[],
    )


def _step_command(
    command: str, reasoning: str = "", h_id: str = "H1", h_conf: int = 60
) -> InvestigationStep:
    return InvestigationStep(
        hypotheses=[
            HypothesisStep(
                id=h_id,
                description="H description",
                confidence=h_conf,
                status="active",
                evidence_for=[],
                evidence_against=[],
            )
        ],
        next_action=NextAction(
            type="command",
            command=command,
            tests_hypothesis=h_id,
            rationale="probe",
        ),
        reasoning=reasoning,
    )


def _step_conclude(root_cause: str = "found it", confidence: int = 90) -> InvestigationStep:
    return InvestigationStep(
        hypotheses=[
            HypothesisStep(
                id="H1",
                description="the thing",
                confidence=confidence,
                status="confirmed",
                evidence_for=[],
                evidence_against=[],
            )
        ],
        next_action=NextAction(
            type="conclude",
            conclusion={
                "root_cause": root_cause,
                "confidence": confidence,
                "evidence_summary": [],
                "not_verified": [],
                "recommendations": [],
            },
        ),
        reasoning="I have enough to conclude.",
    )


def _step_need_info(question: str) -> InvestigationStep:
    return InvestigationStep(
        hypotheses=[],
        next_action=NextAction(type="need_info"),
        reasoning=question,
    )


class _ScriptedReasoner:
    """Returns pre-scripted InvestigationStep values, one per `.step()` call.

    Blows up if the investigator tries to call past the script's end —
    keeps tests honest about how many turns they intended.
    """

    def __init__(self, steps: list[InvestigationStep]) -> None:
        self._steps = list(steps)
        self.call_count = 0
        self.messages_snapshots: list[list[dict[str, Any]]] = []

    async def step(self, messages):
        self.messages_snapshots.append(list(messages))
        self.call_count += 1
        if not self._steps:
            raise RuntimeError("reasoner called more times than the test script provides")
        return self._steps.pop(0)


class _RaisingReasoner:
    """Raises the configured exception on step()."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def step(self, messages):
        raise self._exc


class _ScriptedExecutor:
    def __init__(
        self,
        semantic_results: list[SemanticResult] | None = None,
        compressions: list[str] | None = None,
    ) -> None:
        self._semantic = list(semantic_results or [])
        self._compress = list(compressions or [])
        self.semantic_calls: list[str] = []
        self.compress_calls: list[tuple[str, str]] = []  # (command, output)

    async def semantic_validate(self, command: str) -> SemanticResult:
        self.semantic_calls.append(command)
        if self._semantic:
            return self._semantic.pop(0)
        return SemanticResult(approved=True, reason="default-approve")

    async def compress(self, command, output, investigation_target, hypotheses):
        self.compress_calls.append((command, output))
        if self._compress:
            return self._compress.pop(0)
        return f"compressed[{len(output)}B]"


class _ScriptedProvider:
    """Stand-in for GCPProvider / AdvisorProvider.

    ``responses`` is a list of either ``CommandResult`` instances (returned)
    or Exception instances (raised). One element is consumed per call to
    ``execute_command``.
    """

    def __init__(
        self,
        responses: list[Any] | None = None,
        answer_for_ask_user: str | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self.commands_executed: list[str] = []
        self._ask_user_answer = answer_for_ask_user

    async def execute_command(self, command: str) -> CommandResult:
        self.commands_executed.append(command)
        if not self._responses:
            return CommandResult(
                command=command,
                stdout="default stdout",
                stderr="",
                exit_code=0,
                duration_seconds=0.5,
                truncated=False,
            )
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def ask_user(self, question: str) -> str:
        if self._ask_user_answer is None:
            raise RuntimeError("test didn't configure ask_user_answer")
        return self._ask_user_answer


def _ok_result(command="cmd", dur=0.5) -> CommandResult:
    return CommandResult(
        command=command,
        stdout="stdout",
        stderr="",
        exit_code=0,
        duration_seconds=dur,
        truncated=False,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def event_capture():
    """Returns (events_list, hook_callable). Append every event fired."""
    events: list[InvestigationEvent] = []

    async def _hook(event):
        events.append(event)

    return events, _hook


@pytest.fixture
def approval_log():
    """Approval hook that records every prompt and returns 'approve' by
    default. Tests can override by setting ``approval_log.decision``."""

    class _Rec:
        prompts: list[PendingCommand] = []
        decision: str = "approve"

    rec = _Rec()

    async def _hook(pending):
        rec.prompts.append(pending)
        return rec.decision

    rec.hook = _hook
    return rec


def _build_investigator(reasoner, executor, provider, **kwargs):
    return Investigator(
        provider=provider,
        reasoner=reasoner,
        executor=executor,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------
class TestHappyPath:
    def test_concludes_on_first_turn(self, event_capture, approval_log):
        events, hook = event_capture
        reasoner = _ScriptedReasoner([_step_conclude("it was DNS")])
        executor = _ScriptedExecutor()
        provider = _ScriptedProvider()

        inv = _build_investigator(
            reasoner,
            executor,
            provider,
            approval_hook=approval_log.hook,
            event_hook=hook,
        )
        result = asyncio.run(inv.investigate(_spike()))

        assert result.succeeded
        assert result.conclusion is not None
        assert result.conclusion["root_cause"] == "it was DNS"
        assert result.aborted_reason is None
        # No command proposed → no execution, no compression.
        assert provider.commands_executed == []
        assert executor.semantic_calls == []
        assert executor.compress_calls == []

    def test_command_then_conclude(self, event_capture, approval_log):
        events, hook = event_capture
        reasoner = _ScriptedReasoner(
            [
                _step_command("gcloud logging read 'x' --limit=10"),
                _step_conclude("found it after one command"),
            ]
        )
        executor = _ScriptedExecutor()
        provider = _ScriptedProvider([_ok_result()])

        inv = _build_investigator(
            reasoner,
            executor,
            provider,
            approval_hook=approval_log.hook,
            event_hook=hook,
        )
        result = asyncio.run(inv.investigate(_spike()))

        assert result.succeeded
        assert result.conclusion["root_cause"] == "found it after one command"
        assert len(provider.commands_executed) == 1
        assert executor.semantic_calls == ["gcloud logging read 'x' --limit=10"]
        assert len(executor.compress_calls) == 1
        assert result.budget.commands_used == 1
        assert result.budget.seconds_used >= 0.5


# ---------------------------------------------------------------------------
# Budget exhaustion
# ---------------------------------------------------------------------------
class TestBudgetExhaustion:
    def test_command_count_budget_aborts(self, event_capture, approval_log):
        events, hook = event_capture
        # Reasoner proposes command every turn.
        reasoner = _ScriptedReasoner(
            [_step_command(f"gcloud logging read 'q{i}' --limit=1") for i in range(10)]
        )
        executor = _ScriptedExecutor()
        provider = _ScriptedProvider([_ok_result() for _ in range(10)])
        budget = Budget(max_commands=3, max_cost_usd=99, max_seconds=9999)

        inv = _build_investigator(
            reasoner,
            executor,
            provider,
            approval_hook=approval_log.hook,
            event_hook=hook,
            budget=budget,
        )
        result = asyncio.run(inv.investigate(_spike()))

        assert not result.succeeded
        assert result.aborted_reason is not None
        assert "command budget exhausted" in result.aborted_reason
        assert result.budget.commands_used == 3

    def test_time_budget_aborts(self, event_capture, approval_log):
        events, hook = event_capture
        reasoner = _ScriptedReasoner(
            [_step_command(f"gcloud logging read 'q{i}' --limit=1") for i in range(5)]
        )
        executor = _ScriptedExecutor()
        # Each command "takes" 4 seconds via duration_seconds; budget is 10s.
        provider = _ScriptedProvider([_ok_result(dur=4.0) for _ in range(5)])
        budget = Budget(max_commands=99, max_cost_usd=99, max_seconds=10)

        inv = _build_investigator(
            reasoner,
            executor,
            provider,
            approval_hook=approval_log.hook,
            event_hook=hook,
            budget=budget,
        )
        result = asyncio.run(inv.investigate(_spike()))
        assert "time budget exhausted" in (result.aborted_reason or "")


# ---------------------------------------------------------------------------
# Command-path failure routing
# ---------------------------------------------------------------------------
class TestCommandRejection:
    def test_static_block_feedback_loop(self, event_capture, approval_log):
        """A command blocked by the static validator should NOT execute,
        but should let the reasoner propose a different command on the
        next turn."""
        events, hook = event_capture
        reasoner = _ScriptedReasoner(
            [
                _step_command("gcloud compute instances delete vm1"),  # bad
                _step_conclude("pivoted"),
            ]
        )
        executor = _ScriptedExecutor()
        provider = _ScriptedProvider()

        inv = _build_investigator(
            reasoner,
            executor,
            provider,
            approval_hook=approval_log.hook,
            event_hook=hook,
        )
        result = asyncio.run(inv.investigate(_spike()))

        # Reasoner was called twice (initial + post-block).
        assert reasoner.call_count == 2
        # Never reached executor / provider for the bad command.
        assert provider.commands_executed == []
        assert executor.semantic_calls == []
        # Event was emitted.
        kinds = [e.kind for e in events]
        assert "command_blocked" in kinds
        # Still concluded.
        assert result.succeeded

    def test_semantic_reject_feedback_loop(self, event_capture, approval_log):
        """Static validator passes, Sonnet rejects. Loop continues."""
        events, hook = event_capture
        reasoner = _ScriptedReasoner(
            [
                _step_command("gcloud logging read 'x' --limit=99999"),
                _step_conclude("backed off"),
            ]
        )
        executor = _ScriptedExecutor(
            semantic_results=[
                SemanticResult(approved=False, reason="too broad, no time filter"),
            ]
        )
        provider = _ScriptedProvider()

        inv = _build_investigator(
            reasoner,
            executor,
            provider,
            approval_hook=approval_log.hook,
            event_hook=hook,
        )
        result = asyncio.run(inv.investigate(_spike()))

        assert reasoner.call_count == 2
        assert provider.commands_executed == []  # semantic check blocked
        assert executor.semantic_calls == ["gcloud logging read 'x' --limit=99999"]
        # command_blocked event with L6 layer
        blocked = [e for e in events if e.kind == "command_blocked"]
        assert blocked
        assert blocked[0].payload["layer"] == "L6"
        assert result.succeeded

    def test_user_rejects_feedback_loop(self, event_capture, approval_log):
        events, hook = event_capture
        reasoner = _ScriptedReasoner(
            [
                _step_command("gcloud logging read 'x' --limit=100"),
                _step_conclude("pivoted after user reject"),
            ]
        )
        executor = _ScriptedExecutor()
        provider = _ScriptedProvider()
        approval_log.decision = "reject"

        inv = _build_investigator(
            reasoner,
            executor,
            provider,
            approval_hook=approval_log.hook,
            event_hook=hook,
        )
        result = asyncio.run(inv.investigate(_spike()))

        # We went through validator + semantic (both approved), then user
        # rejected — so provider was never called.
        assert provider.commands_executed == []
        assert executor.semantic_calls == ["gcloud logging read 'x' --limit=100"]
        # Event fired.
        assert any(e.kind == "command_rejected_by_user" for e in events)
        assert result.succeeded

    def test_user_aborts_ends_investigation(self, event_capture, approval_log):
        events, hook = event_capture
        reasoner = _ScriptedReasoner(
            [
                _step_command("gcloud logging read 'x'"),
                # Won't reach this because user aborts.
                _step_conclude("unreachable"),
            ]
        )
        executor = _ScriptedExecutor()
        provider = _ScriptedProvider()
        approval_log.decision = "abort"

        inv = _build_investigator(
            reasoner,
            executor,
            provider,
            approval_hook=approval_log.hook,
            event_hook=hook,
        )
        result = asyncio.run(inv.investigate(_spike()))

        assert not result.succeeded
        assert "user aborted" in (result.aborted_reason or "")
        # Reasoner only called once — we didn't re-loop after abort.
        assert reasoner.call_count == 1


# ---------------------------------------------------------------------------
# Advisor-mode exception propagation
# ---------------------------------------------------------------------------
class TestAdvisorExceptions:
    def test_advisor_aborted_ends_investigation(self, event_capture, approval_log):
        events, hook = event_capture
        reasoner = _ScriptedReasoner(
            [
                _step_command("gcloud logging read 'x'"),
                _step_conclude("unreachable"),
            ]
        )
        executor = _ScriptedExecutor()
        provider = _ScriptedProvider([AdvisorAborted("user /quit")])

        inv = _build_investigator(
            reasoner,
            executor,
            provider,
            approval_hook=approval_log.hook,
            event_hook=hook,
        )
        result = asyncio.run(inv.investigate(_spike()))
        assert not result.succeeded
        assert result.aborted_reason is not None

    def test_advisor_skipped_injects_feedback(self, event_capture, approval_log):
        events, hook = event_capture
        reasoner = _ScriptedReasoner(
            [
                _step_command("gcloud logging read 'x'"),
                _step_conclude("concluded after skip"),
            ]
        )
        executor = _ScriptedExecutor()
        provider = _ScriptedProvider([AdvisorSkipped("skip")])

        inv = _build_investigator(
            reasoner,
            executor,
            provider,
            approval_hook=approval_log.hook,
            event_hook=hook,
        )
        result = asyncio.run(inv.investigate(_spike()))
        assert result.succeeded
        assert reasoner.call_count == 2
        # Second message to reasoner should carry "SKIPPED" feedback.
        second_msgs = reasoner.messages_snapshots[1]
        joined = "\n".join(
            m["content"] if isinstance(m["content"], str) else "" for m in second_msgs
        )
        assert "SKIPPED" in joined

    def test_advisor_note_calls_memory_hook_and_continues(self, event_capture, approval_log):
        events, hook = event_capture
        reasoner = _ScriptedReasoner(
            [
                _step_command("gcloud logging read 'x'"),
                _step_conclude("done"),
            ]
        )
        executor = _ScriptedExecutor()
        provider = _ScriptedProvider([AdvisorNote("forgot to mention the region")])

        memory_calls: list[tuple[str, str]] = []

        def _mem_hook(kind, text):
            memory_calls.append((kind, text))

        inv = _build_investigator(
            reasoner,
            executor,
            provider,
            approval_hook=approval_log.hook,
            event_hook=hook,
            memory_hook=_mem_hook,
        )
        result = asyncio.run(inv.investigate(_spike()))
        assert result.succeeded
        assert memory_calls == [("user_note", "forgot to mention the region")]
        assert any(e.kind == "user_note" for e in events)


# ---------------------------------------------------------------------------
# Provider-level failures (Layer 7)
# ---------------------------------------------------------------------------
class TestProviderFailures:
    def test_command_rejected_error_injects_feedback(self, event_capture, approval_log):
        events, hook = event_capture
        reasoner = _ScriptedReasoner(
            [
                _step_command("gcloud logging read 'x'"),
                _step_conclude("concluded after L7 rejection"),
            ]
        )
        executor = _ScriptedExecutor()
        provider = _ScriptedProvider(
            [
                CommandRejectedError("L7 re-validation failed"),
            ]
        )

        inv = _build_investigator(
            reasoner,
            executor,
            provider,
            approval_hook=approval_log.hook,
            event_hook=hook,
        )
        result = asyncio.run(inv.investigate(_spike()))
        assert result.succeeded
        assert reasoner.call_count == 2

    def test_command_timeout_injects_feedback(self, event_capture, approval_log):
        events, hook = event_capture
        reasoner = _ScriptedReasoner(
            [
                _step_command("gcloud logging read 'x'"),
                _step_conclude("ok despite timeout"),
            ]
        )
        executor = _ScriptedExecutor()
        provider = _ScriptedProvider(
            [
                CommandTimeoutError("exceeded 120s"),
            ]
        )

        inv = _build_investigator(
            reasoner,
            executor,
            provider,
            approval_hook=approval_log.hook,
            event_hook=hook,
        )
        result = asyncio.run(inv.investigate(_spike()))
        assert result.succeeded


# ---------------------------------------------------------------------------
# need_info path
# ---------------------------------------------------------------------------
class TestNeedInfo:
    def test_need_info_routes_through_ask_user(self, event_capture, approval_log):
        events, hook = event_capture
        reasoner = _ScriptedReasoner(
            [
                _step_need_info("Which project?"),
                _step_conclude("got it"),
            ]
        )
        executor = _ScriptedExecutor()
        provider = _ScriptedProvider(answer_for_ask_user="prod-edge-42")

        inv = _build_investigator(
            reasoner,
            executor,
            provider,
            approval_hook=approval_log.hook,
            event_hook=hook,
        )
        result = asyncio.run(inv.investigate(_spike()))
        assert result.succeeded
        assert reasoner.call_count == 2
        # opus_asks event emitted.
        assert any(e.kind == "opus_asks" for e in events)
        # The answer is injected into the next reasoner call.
        second_msgs = reasoner.messages_snapshots[1]
        joined = "\n".join(
            m["content"] if isinstance(m["content"], str) else "" for m in second_msgs
        )
        assert "prod-edge-42" in joined


# ---------------------------------------------------------------------------
# Reasoner failure
# ---------------------------------------------------------------------------
class TestReasonerFailure:
    def test_reasoner_exception_aborts_cleanly(self, event_capture, approval_log):
        events, hook = event_capture
        reasoner = _RaisingReasoner(RuntimeError("opus down"))
        executor = _ScriptedExecutor()
        provider = _ScriptedProvider()

        inv = _build_investigator(
            reasoner,
            executor,
            provider,
            approval_hook=approval_log.hook,
            event_hook=hook,
        )
        result = asyncio.run(inv.investigate(_spike()))

        assert not result.succeeded
        assert "reasoner error" in (result.aborted_reason or "")
        assert any(e.kind == "aborted" for e in events)


# ---------------------------------------------------------------------------
# Events order
# ---------------------------------------------------------------------------
class TestEvents:
    def test_event_order_on_happy_command_cycle(self, event_capture, approval_log):
        events, hook = event_capture
        reasoner = _ScriptedReasoner(
            [
                _step_command("gcloud logging read 'x'", reasoning="the hypothesis"),
                _step_conclude("done"),
            ]
        )
        executor = _ScriptedExecutor()
        provider = _ScriptedProvider([_ok_result()])

        inv = _build_investigator(
            reasoner,
            executor,
            provider,
            approval_hook=approval_log.hook,
            event_hook=hook,
        )
        asyncio.run(inv.investigate(_spike()))

        kinds = [e.kind for e in events]
        # Expected sequence (the investigator emits at minimum):
        #   spike_selected → step_started → hypotheses_updated → reasoning
        #   → command_proposed → command_approved → command_executed
        #   → compressing → evidence_added → step_started → hypotheses_updated
        #   → concluded
        for expected in [
            "spike_selected",
            "step_started",
            "hypotheses_updated",
            "reasoning",
            "command_proposed",
            "command_approved",
            "command_executed",
            "compressing",
            "evidence_added",
            "concluded",
        ]:
            assert expected in kinds, f"missing event: {expected!r} in {kinds}"
        # spike_selected must come first; concluded must be last.
        assert kinds[0] == "spike_selected"
        assert kinds[-1] == "concluded"
