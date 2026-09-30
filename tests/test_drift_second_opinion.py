"""The drift model's second opinion decides, and a lens that is reading is not idle.

From the first unattended run of the harness inside an evaluation (security-eval,
prerequisite P6; one run, a local model): the security lens spent each turn
reading the repository and reported nothing yet. Every one of those turns
scored `no_progress` 0.50, was escalated to the drift model, which called it
on-brief (0.26) -- and was refocused anyway, four times, then stopped with its
budget half spent, while the status line said "4/4 agents done".

Three defects, each pinned here:

* the second opinion was requested *after* the directive had been issued, so
  it was paid for, logged, and never able to change anything;
* a turn spent reading the workspace scored the same as an idle one, and was
  told to "read a specific file" -- what it had just done;
* the status line counted a stopped agent as done.

And one guard that letting the model decide makes necessary: a scope violation
cannot be talked down by a second opinion.
"""

from __future__ import annotations

import json
from typing import Any

from supervisor_harness.config import Policy
from supervisor_harness.core.drift import (
    TurnContext,
    assess_heuristically,
    decide_directive,
    merge_assessments,
)
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    AgentSpec,
    AgentStatus,
    AgentTurn,
    DirectiveKind,
    DriftAssessment,
    DriftSignal,
    RunMode,
    Severity,
    Usage,
)
from supervisor_harness.providers.base import (
    CompletionRequest,
    CompletionResponse,
    ProviderError,
)
from supervisor_harness.store.events import EventType

from .conftest import FakeProvider

PROMPT = "Review src/auth for security problems"


def quiet_turn(tool_calls: int = 0) -> AgentTurn:
    return AgentTurn(output="still looking", claimed_status=AgentStatus.RUNNING,
                     usage=Usage(tool_calls=tool_calls))


def assess(turn: AgentTurn, agent: AgentSpec | None = None) -> DriftAssessment:
    agent = agent or AgentSpec(objectives=["find injection paths"])
    return assess_heuristically(TurnContext(
        agent=agent, turn=turn, previous_turns=[AgentTurn(output="first look")],
        brief="", task_prompt=PROMPT, turn_index=1,
    ))


# -- the heuristics ------------------------------------------------------------


def test_an_idle_turn_is_still_no_progress() -> None:
    kinds = {s.kind for s in assess(quiet_turn()).signals}
    assert "no_progress" in kinds


def test_a_turn_spent_reading_is_a_weaker_signal_that_does_not_correct_on_its_own() -> None:
    assessment = assess(quiet_turn(tool_calls=3))
    kinds = {s.kind for s in assessment.signals}

    assert "unreported_exploration" in kinds
    assert "no_progress" not in kinds
    agent = AgentSpec(objectives=["find injection paths"])
    directive = decide_directive(assessment, agent, quiet_turn(3), Policy(), turns_used=2)
    assert directive.kind is DirectiveKind.CONTINUE
    assert directive.corrections == [], "plenty of budget left: let it work"


def test_a_lens_still_only_reading_near_the_end_of_its_budget_is_told_to_report() -> None:
    """Budget exhaustion stops an agent after its last turn -- too late to answer."""
    agent = AgentSpec(objectives=["find injection paths"])
    assessment = assess(quiet_turn(tool_calls=3), agent)

    directive = decide_directive(assessment, agent, quiet_turn(3), Policy(),
                                 turns_used=agent.budget.max_turns - 2)

    assert directive.kind is DirectiveKind.CONTINUE
    assert any("reported nothing" in c for c in directive.corrections)
    assert not any("read a specific file" in c for c in directive.corrections)


def test_a_second_opinion_cannot_talk_the_harness_out_of_a_scope_violation() -> None:
    heuristic = DriftAssessment(
        on_task=False, score=0.85,
        signals=[DriftSignal(kind="scope_paths", severity=Severity.HIGH, detail="x", score=0.85)],
    )
    model = DriftAssessment(on_task=True, score=0.0)

    merged = merge_assessments(heuristic, model)

    assert merged.score == 0.85
    assert not merged.on_task


def test_a_second_opinion_can_lower_a_score_the_heuristics_are_unsure_of() -> None:
    heuristic = DriftAssessment(
        on_task=False, score=0.5,
        signals=[DriftSignal(kind="no_progress", severity=Severity.MEDIUM, detail="x", score=0.5)],
    )
    merged = merge_assessments(heuristic, DriftAssessment(on_task=True, score=0.1))
    assert merged.score < 0.45
    assert merged.on_task


# -- inside a live run -----------------------------------------------------------


class QuietThenReporting(FakeProvider):
    """Analysis agents say nothing on their second turn; the drift model calls it on-brief."""

    def __init__(self) -> None:
        super().__init__()
        self.turns: dict[str, int] = {}

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if self._classify(request) == "analysis":
            key = request.messages[0].content[:300]
            self.turns[key] = self.turns.get(key, 0) + 1
            if self.turns[key] == 2:
                payload: dict[str, Any] = {"output": "still examining the handlers",
                                           "findings": [], "status": "running"}
                return CompletionResponse(text=json.dumps(payload), model="fake-1",
                                          provider="fake", usage=Usage(10, 5))
        return await super().complete(request)


def _events(supervisor: Supervisor, run_id: str) -> list[Any]:
    return list(supervisor.store.log(run_id).read())


async def test_the_second_opinion_is_taken_before_the_directive_and_the_directive_follows_it(
    supervisor: Supervisor,
) -> None:
    fake = QuietThenReporting()
    fake.overrides["analysis"] = {"output": "first look at src/auth/login.py",
                                  "findings": [], "status": "running"}
    supervisor.router.register("fake", fake)

    response = await supervisor.run(PROMPT, mode=RunMode.REPORT)
    events = _events(supervisor, response.run_id)

    merged = [e for e in events if e.type is EventType.DRIFT_ASSESSED
              and e.payload["assessment"].get("checked_by") == "heuristics+model"]
    assert merged, "the quiet turn should have been escalated"
    for opinion in merged:
        agent_id = opinion.payload["agent_id"]
        turn_id = opinion.payload["assessment"].get("turn_id")
        directive = next(e for e in events if e.type is EventType.DIRECTIVE_ISSUED
                         and e.payload["directive"]["agent_id"] == agent_id
                         and e.payload["directive"].get("turn_id") == turn_id)
        assert opinion.seq < directive.seq, "the opinion came after the decision"
        issued = directive.payload["directive"]
        # A stop for running out of turns is the budget, not a correction: the
        # fixture allows three turns.
        corrected = issued["kind"] in ("refocus", "narrow") or (
            issued["kind"] == "stop" and "budget exhausted" not in issued.get("rationale", "")
        )
        assert not corrected, (
            "the drift model called the turn on-brief, so it should not have been corrected"
        )


class DriftModelDown(QuietThenReporting):
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if self._classify(request) == "drift":
            raise ProviderError("fake", "HTTP 529", retryable=False)
        return await super().complete(request)


async def test_a_failed_second_opinion_leaves_the_heuristics_standing_and_says_so(
    supervisor: Supervisor,
) -> None:
    fake = DriftModelDown()
    fake.overrides["analysis"] = {"output": "first look", "findings": [], "status": "running"}
    supervisor.router.register("fake", fake)

    response = await supervisor.run(PROMPT, mode=RunMode.REPORT)
    events = _events(supervisor, response.run_id)

    assert any(e.type is EventType.NOTE and "second opinion failed" in e.payload.get("text", "")
               for e in events)
    assert response.action == "complete"


class Reading(FakeProvider):
    """Reads one file on the first round of every analysis turn."""

    def __init__(self) -> None:
        super().__init__()
        self.rounds: dict[str, int] = {}

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if self._classify(request) == "analysis":
            key = request.messages[-1].content[:300]
            first = key not in self.rounds
            self.rounds[key] = self.rounds.get(key, 0) + 1
            if first and not request.messages[-1].content.startswith("["):
                payload = {"tool_calls": [{"tool": "read_file",
                                           "args": {"path": "src/auth/login.py"}}]}
                return CompletionResponse(text=json.dumps(payload), model="fake-1",
                                          provider="fake", usage=Usage(10, 5))
        return await super().complete(request)


async def test_tool_calls_are_counted_on_the_turn(supervisor: Supervisor) -> None:
    """What the heuristic reads, and what `Budget.max_tool_calls` is enforced against."""
    supervisor.router.register("fake", Reading())

    response = await supervisor.run(PROMPT, mode=RunMode.REPORT)
    state = supervisor.store.open(response.run_id).state

    analysis = [t for t in state.turns if state.agents[t.agent_id].kind.value == "analysis"]
    assert analysis
    assert any(t.usage.tool_calls >= 1 for t in analysis)


async def test_the_status_line_does_not_call_a_stopped_agent_done(
    supervisor: Supervisor,
) -> None:
    response = await supervisor.run(PROMPT, mode=RunMode.REPORT)
    session = supervisor.store.open(response.run_id)
    agent = next(iter(session.state.agents.values()))
    await supervisor.lifecycle._set_status(session, agent, AgentStatus.STOPPED)

    ledger = supervisor.reporting.ledger(response.run_id)

    total = len(session.state.agents)
    assert f"{total - 1}/{total} agents done" in ledger
    assert "1 stopped" in ledger
