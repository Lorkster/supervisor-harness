"""A clean "done" on the last allowed turn is accepted (review finding 9)."""

from __future__ import annotations

from supervisor_harness.config import Policy
from supervisor_harness.core.drift import decide_directive
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    AgentStatus,
    AgentTurn,
    Budget,
    DirectiveKind,
    DriftAssessment,
    Usage,
)


def _agent() -> AgentSpec:
    return AgentSpec(id="agt_t", role="security", title="t", kind=AgentKind.ANALYSIS,
                     budget=Budget(max_turns=3))


def test_done_with_nothing_drifting_on_the_last_turn_is_accepted() -> None:
    turn = AgentTurn(claimed_status=AgentStatus.DONE, output="all checked")
    directive = decide_directive(DriftAssessment(score=0.0), _agent(), turn, Policy(),
                                 turns_used=3)
    assert directive.kind is DirectiveKind.ACCEPT


def test_the_turn_budget_still_stops_an_agent_that_is_not_done() -> None:
    turn = AgentTurn(claimed_status=AgentStatus.RUNNING, output="still reading")
    directive = decide_directive(DriftAssessment(score=0.0), _agent(), turn, Policy(),
                                 turns_used=3)
    assert directive.kind is DirectiveKind.STOP


def test_a_done_that_is_also_adrift_is_still_stopped_at_the_budget() -> None:
    from supervisor_harness.models import DriftSignal

    turn = AgentTurn(claimed_status=AgentStatus.DONE, output="x")
    adrift = DriftAssessment(score=0.5, on_task=False,
                             signals=[DriftSignal(kind="brief_echo", detail="d")])
    directive = decide_directive(adrift, _agent(), turn, Policy(), turns_used=3)
    assert directive.kind is DirectiveKind.STOP


def test_another_ceiling_is_not_waived_by_a_clean_done() -> None:
    agent = _agent()
    agent.budget = Budget(max_turns=3, max_tokens=10)
    turn = AgentTurn(claimed_status=AgentStatus.DONE, output="x")
    directive = decide_directive(DriftAssessment(score=0.0), agent, turn, Policy(),
                                 turns_used=3, usage=Usage(input_tokens=50))
    assert directive.kind is DirectiveKind.STOP
