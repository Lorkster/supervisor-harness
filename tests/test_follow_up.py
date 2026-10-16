"""Turnstone's remaining two: follow up with the agent that did the work, and keep
"blocked" apart from "needs the owner".

Turnstone's coordinator, when a child reports, decides whether the goal is met
and then closes it out, "push[es] a follow-up" to the same child, or covers the
gap another way; and it keeps ``blocked`` -- a dependency it may clear itself --
apart from ``needs_user``. Here a failed task waited for the checkpoint, and was
sent back to a fresh agent only if the checkpoint failed and the run's one
remediation budget allowed; every blocked agent went to the owner.

Which task a stop waits for is the coordinator's to decide now (batch O,
`tests/test_coordinator.py`); without it, a stop goes to the owner.
"""

from __future__ import annotations

from typing import Any

import pytest

from supervisor_harness.agents.brief import build_follow_up
from supervisor_harness.contracts import EXECUTION_TURN_SCHEMA
from supervisor_harness.core.conversation import (
    IMPLEMENTER_SYSTEM,
    REPORT_TOOL,
    Stint,
    stint_payload,
)
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    AgentStatus,
    AgentTurn,
    CriterionStatus,
    Decision,
    Directive,
    DirectiveKind,
    DoDCriterion,
    ExecutionTask,
    RunMode,
    RunState,
    TaskStatus,
)
from supervisor_harness.providers.base import CompletionRequest, CompletionResponse

from .test_implementer_conversation import DONE, READ, NativeFake, _calls, _conversing
from .test_send_back_criteria import PROMPT

# -- "blocked" is not "needs the owner" ----------------------------------------------------


def test_a_blocked_report_says_what_it_needs() -> None:
    props = REPORT_TOOL["parameters"]["properties"]
    assert props["needs"]["enum"] == ["work", "owner"] and "waiting_for" in props
    assert {"needs", "waiting_for"} <= set(EXECUTION_TURN_SCHEMA["properties"])
    assert '"work" when another task in the plan must land something first' in IMPLEMENTER_SYSTEM
    payload = stint_payload(Stint(), {"status": "blocked", "summary": "s", "needs": "work",
                                      "waiting_for": "Build the page"})
    assert (payload["needs"], payload["waiting_for"]) == ("work", "Build the page")


def _plan(supervisor: Supervisor, run_id: str) -> tuple[Any, AgentSpec, dict[str, ExecutionTask]]:
    page = ExecutionTask(title="Build the page", status=TaskStatus.APPROVED,
                         decision=Decision.APPROVE)
    tests = ExecutionTask(title="Write the browser tests", status=TaskStatus.IN_PROGRESS,
                          attempts=1, decision=Decision.APPROVE)
    after = ExecutionTask(title="Run the gates", depends_on=[tests.id],
                          status=TaskStatus.APPROVED, decision=Decision.APPROVE)
    done = ExecutionTask(title="Scaffold", status=TaskStatus.VERIFIED, decision=Decision.APPROVE)
    me = AgentSpec(id="agt_t", kind=AgentKind.EXECUTION, task_id=tests.id,
                   status=AgentStatus.RUNNING)
    tasks = {t.title: t for t in (page, tests, after, done)}
    state = RunState(id=run_id, prompt="p", tasks={t.id: t for t in tasks.values()},
                     agents={me.id: me})
    return supervisor.store.create(state), me, tasks


def _blocked(needs: str, waiting_for: str = "") -> AgentTurn:
    return AgentTurn(agent_id="agt_t", claimed_status=AgentStatus.BLOCKED, needs=needs,
                     waiting_for=waiting_for, blocked_on="the page has no #board yet")


async def test_the_turn_carries_what_it_needs(supervisor: Supervisor) -> None:
    session, me, _ = _plan(supervisor, "run_Turn")
    turn = await supervisor.supervision._record_turn(session, me, {
        "output": "o", "status": "blocked", "needs": " Work ", "waiting_for": " Build the page "})
    assert (turn.needs, turn.waiting_for) == ("work", "Build the page")


@pytest.mark.parametrize(("needs", "waiting_for"), [
    ("owner", ""), ("owner", "Build the page"), ("work", "Build the page"), ("", ""),
])
async def test_without_the_coordinator_a_stop_goes_to_the_owner(
    supervisor: Supervisor, needs: str, waiting_for: str,
) -> None:
    session, me, tasks = _plan(supervisor, f"run_O{needs}{len(waiting_for)}")
    supervisor.config.policy.coordinator = False
    directive = Directive(agent_id=me.id, kind=DirectiveKind.ESCALATE,
                          rationale="blocked")
    await supervisor.supervision._park_task(session, me, _blocked(needs, waiting_for), directive)

    (escalation,) = session.state.open_escalations()
    assert escalation.task_id == tasks["Write the browser tests"].id
    assert tasks["Write the browser tests"].status is TaskStatus.BLOCKED


# -- a failed task is followed up at once, by the agent that did the work -------------------


def test_the_follow_up_says_what_the_check_found() -> None:
    task = ExecutionTask(title="t", dod=[
        DoDCriterion(statement="npm test passes",
                     evidence="reopened for attempt 2; attempt 1 recorded fail: 2 failed: "
                              "timer stays at 0"),
        DoDCriterion(statement="builds", evidence="reopened for attempt 2; attempt 1 recorded "
                                                  "pass: exit 0"),
    ])
    text = build_follow_up(task)
    assert "- npm test passes\n  What the check found: 2 failed: timer stays at 0" in text
    assert "builds" not in text and "Fix what they found" in text


class FailsOnce(NativeFake):
    """Its first verifier fails every criterion; every later one passes them."""

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if "verdict" in {t["name"] for t in request.tools or []}:
            self.verdict = "fail" if not self.verifier else "pass"
        return await super().complete(request)


async def test_a_failed_check_goes_back_to_the_same_conversation_at_once(
    supervisor: Supervisor,
) -> None:
    fake = FailsOnce(_calls(READ), _calls(DONE), _calls(DONE))
    response = await _conversing(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE,
                                                       auto_approve=True)
    state = supervisor.store.load_state(response.run_id)
    (task,) = state.tasks.values()

    assert task.status is TaskStatus.VERIFIED and task.attempts == 2
    (sent,) = [a for c in state.coordination for a in c.actions]
    assert (sent["do"], sent["task"], sent["attempt"]) == ("send_back", task.id, "1")
    assert any(n.text == "followed up in the same conversation" for n in state.notes)
    second = next(r for r in fake.implementer
                  if any("Your work on this task was checked" in m.content
                         for m in r.messages if m.role == "user"))
    assert second.messages[0].content.startswith("# Your task:"), "the first brief, kept"
    assert any(m.role == "tool" and "account_key = request.form" in m.content
               for m in second.messages), "what it read on attempt 1 is still in front of it"
    assert state.checkpoint_iteration <= 1, "no failing checkpoint was needed to send it back"


async def test_off_the_checkpoint_is_the_only_way_back(supervisor: Supervisor) -> None:
    supervisor.config.policy.follow_up = False
    supervisor.config.policy.coordinator = False
    fake = FailsOnce(_calls(READ), _calls(DONE), _calls(DONE))
    response = await _conversing(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE,
                                                       auto_approve=True)
    state = supervisor.store.load_state(response.run_id)
    assert not any("followed up at once" in n.text for n in state.notes)


async def test_without_the_conversation_the_brief_carries_the_follow_up(
    supervisor: Supervisor,
) -> None:
    session, _, tasks = _plan(supervisor, "run_Brief")
    task = tasks["Write the browser tests"]
    task.status = TaskStatus.FAILED
    task.dod = [DoDCriterion(statement="the e2e suite passes", status=CriterionStatus.FAIL,
                             evidence="3 failed"),
                DoDCriterion(statement="never checked")]

    assert await supervisor._follow_up(session) == 1
    assert task.status is TaskStatus.APPROVED
    assert "Follow-up from attempt 1, which did not pass:\n- the e2e suite passes" in task.action
    assert task.dod[0].status is CriterionStatus.UNVERIFIED, "reopened for the new attempt"

    task.status, task.attempts = TaskStatus.FAILED, supervisor.config.policy.max_task_attempts
    task.dod[0].status = CriterionStatus.FAIL
    assert await supervisor._follow_up(session) == 0, "out of attempts"
    task.attempts = 1
    task.dod = [DoDCriterion(statement="never checked")]
    assert await supervisor._follow_up(session) == 0, "ended unchecked, not failed by a check"
