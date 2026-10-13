"""Batch K, part 1: agents in the conversation loop can tell the supervisor things again.

The blackboard routes agents' messages through the supervisor -- question,
finding, warning, contradiction, handoff -- and delivers each agent's inbox with
its next directive. That traffic was carried in the turn contract's JSON; batch
I's conversation loop offered file and command tools and `report`, so in the
first Minesweeper run to reach its last task no agent sent anything, and the
one that found a bug in an earlier task's file could only report itself blocked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from supervisor_harness.config import Policy
from supervisor_harness.core.conversation import (
    IMPLEMENTER_SYSTEM,
    MESSAGE_TOOL,
    Stint,
    native_tool_specs,
    stint_payload,
)
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import AgentKind, AgentSpec, MessageKind, RunMode
from supervisor_harness.providers.base import ToolCall

from .test_implementer_conversation import DONE, NativeFake, _calls, _conversing
from .test_send_back_criteria import PROMPT

TELL = ToolCall("message", {
    "to": "supervisor", "kind": "warning", "subject": "earlier task's bug",
    "content": "src/auth/login.py:3 never sets data-state on the first render"})


def test_implementers_and_verifiers_may_send_and_lenses_may_not() -> None:
    def names(kind: AgentKind) -> list[str]:
        return [t["name"] for t in native_tool_specs(AgentSpec(kind=kind), Policy())]

    assert "message" in names(AgentKind.EXECUTION)
    assert "message" in names(AgentKind.VERIFICATION)
    assert "message" not in names(AgentKind.ANALYSIS), "the planner and lenses answer otherwise"
    assert names(AgentKind.EXECUTION)[-1] == "report", "the finish call stays last"
    assert MESSAGE_TOOL["parameters"]["required"] == ["to", "kind", "content"]


def test_a_stretch_keeps_what_it_sent_for_its_turn() -> None:
    stint = Stint()
    assert stint.send({"to": "", "kind": "question", "content": "which locale wins?"}).startswith(
        "Sent.")
    assert stint.send({"to": "agt_x", "kind": "finding", "content": "  "}).startswith("Not sent")

    assert stint.messages == [{"recipient": "supervisor", "kind": "question", "subject": "",
                               "content": "which locale wins?"}], "no recipient: the supervisor"
    assert stint_payload(stint, None)["messages"] == stint.messages
    assert stint_payload(stint, {"status": "done", "summary": "s"})["messages"] == stint.messages


def test_the_rules_say_when_to_use_it() -> None:
    assert "tell the supervisor with message" in " ".join(IMPLEMENTER_SYSTEM.split())


async def test_a_message_reaches_the_blackboard_and_the_work_goes_on(
    supervisor: Supervisor, workspace: Path,
) -> None:
    fake = NativeFake(_calls(TELL), _calls(DONE))
    response = await _conversing(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE,
                                                       auto_approve=True)
    state = supervisor.store.load_state(response.run_id)

    (message,) = [m for m in state.messages if m.kind is MessageKind.WARNING]
    implementer = next(a for a in state.agents.values() if a.kind is AgentKind.EXECUTION)
    assert (message.sender, message.recipient) == (implementer.id, "supervisor")
    assert "never sets data-state" in message.content
    replies = [m.content for m in fake.implementer[1].messages if m.role == "tool"]
    assert any(r.startswith("Sent. The supervisor routes it") for r in replies), replies
    (task,) = state.tasks.values()
    assert task.status.value == "verified", "sending is not finishing, or failing"


async def test_a_verifier_may_send_too(supervisor: Supervisor) -> None:
    from supervisor_harness.providers.base import CompletionRequest, CompletionResponse

    class TellingVerifier(NativeFake):
        told = False

        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            names = {t["name"] for t in request.tools or []}
            if "verdict" in names and not self.told:
                self.told = True
                return _calls(ToolCall("message", {"to": "*", "kind": "finding",
                                                   "content": "the board has one column"}))(request)
            return await super().complete(request)

    fake = TellingVerifier(_calls(DONE))
    response = await _conversing(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE,
                                                       auto_approve=True)
    state = supervisor.store.load_state(response.run_id)

    (message,) = [m for m in state.messages if m.content == "the board has one column"]
    verifier = next(a for a in state.agents.values() if a.kind is AgentKind.VERIFICATION)
    assert (message.sender, message.kind) == (verifier.id, MessageKind.FINDING)


# -- a finding about another task's work goes to that task ---------------------------------


def _two_tasks(supervisor: Supervisor, page_status: str = "verified", attempts: int = 1):  # type: ignore[no-untyped-def]
    from supervisor_harness.models import ExecutionTask, RunState, Scope, TaskStatus

    (supervisor.workspace / "src" / "page.ts").write_text("render()\n", encoding="utf-8")
    page = ExecutionTask(title="Build the page", scope=Scope(paths=["src/page.ts"]),
                         status=TaskStatus(page_status), attempts=attempts,
                         decision=None)
    e2e = ExecutionTask(title="Write the e2e tests", scope=Scope(paths=["tests/**"]),
                        status=TaskStatus.IN_PROGRESS, attempts=1)
    from supervisor_harness.models import Decision
    page.decision = e2e.decision = Decision.APPROVE
    me = AgentSpec(id="agt_me", kind=AgentKind.EXECUTION, task_id=e2e.id)
    session = supervisor.store.create(RunState(id="run_F", prompt="Build a browser game",
                                               tasks={page.id: page, e2e.id: e2e},
                                               agents={me.id: me}))
    return session, me, page, e2e


def _turn(*messages: tuple[str, str, str]):  # type: ignore[no-untyped-def]
    from supervisor_harness.models import AgentTurn, Message

    return AgentTurn(agent_id="agt_me", messages=[
        Message(recipient=to, kind=MessageKind(kind), content=content)
        for to, kind, content in messages])


async def test_a_finding_reopens_the_verified_task_whose_work_it_names(
    supervisor: Supervisor,
) -> None:
    from supervisor_harness.models import CriterionStatus, DoDCriterion, TaskStatus

    session, me, page, _ = _two_tasks(supervisor)
    page.dod.append(DoDCriterion(statement="renders", status=CriterionStatus.PASS))

    await supervisor._route_findings(session, me, _turn(
        ("supervisor", "warning", "src/page.ts:1 never sets data-state on the first render")))

    assert page.status is TaskStatus.APPROVED, "reopened for the task that did the work"
    assert page.dod[0].status is CriterionStatus.UNVERIFIED, "its verdicts reopened too"
    (expectation,) = session.state.goal
    assert expectation.task_id == page.id and expectation.source == "agent agt_me"
    assert any(c.goal_id == expectation.id and c.mandatory for c in page.dod)
    assert "Found by agt_me, working on 'Write the e2e tests'" in page.action
    assert any("which was reopened" in n.text for n in session.state.notes)
    (supervisor.store.runs_dir / "run_F" / "state.json").unlink()   # the log alone
    reloaded = supervisor.store.load_state("run_F")
    assert [e.statement for e in reloaded.goal] == [expectation.statement], "on the log"


async def test_the_check_asks_whether_the_problem_is_there(supervisor: Supervisor) -> None:
    """Minesweeper run 5: routed as "would whoever asked find this true", a report
    that another task's test file was wrong was judged false, and failed the task."""
    session, me, page, _ = _two_tasks(supervisor)
    await supervisor._route_findings(session, me, _turn(
        ("supervisor", "finding", "src/page.ts:1 never sets data-state")))

    (check,) = [c for c in page.dod if c.goal_id]
    assert check.statement.startswith("Reported by another agent, and checked:")
    assert "Pass if the problem it describes is not there" in check.rubric
    assert "fail only if it is still there in this task's work" in check.rubric
    assert "would find this true" not in check.rubric


async def test_a_finding_goes_to_every_task_whose_files_it_names(
    supervisor: Supervisor,
) -> None:
    """Run 5's finding was about a test file and named the engine beside it; only the
    engine, the first task to match, was reopened."""
    from supervisor_harness.models import Decision, ExecutionTask, Scope, TaskStatus

    session, me, page, _ = _two_tasks(supervisor)
    (supervisor.workspace / "src" / "page.test.ts").write_text("", encoding="utf-8")
    tester = ExecutionTask(title="Test the page", scope=Scope(paths=["src/page.test.ts"]),
                           status=TaskStatus.VERIFIED, attempts=1, decision=Decision.APPROVE)
    session.state.tasks[tester.id] = tester

    await supervisor._route_findings(session, me, _turn(
        ("supervisor", "finding",
         "src/page.test.ts expects the wrong text; src/page.ts is right")))

    assert page.status is TaskStatus.APPROVED and tester.status is TaskStatus.APPROVED
    assert sorted(e.task_id for e in session.state.goal) == sorted([page.id, tester.id])


async def test_a_task_not_yet_done_just_gains_the_check(supervisor: Supervisor) -> None:
    from supervisor_harness.models import TaskStatus

    session, me, page, _ = _two_tasks(supervisor, page_status="approved")
    await supervisor._route_findings(session, me, _turn(
        ("supervisor", "finding", "src/page.ts has no column layout")))

    assert page.status is TaskStatus.APPROVED and any(c.goal_id for c in page.dod)
    assert not any("reopened" in n.text for n in session.state.notes)


async def test_out_of_attempts_the_check_is_added_but_nothing_reopens(
    supervisor: Supervisor,
) -> None:
    from supervisor_harness.models import TaskStatus

    most = supervisor.config.policy.max_task_attempts
    session, me, page, _ = _two_tasks(supervisor, attempts=most)
    await supervisor._route_findings(session, me, _turn(
        ("supervisor", "contradiction", "src/page.ts contradicts the spec")))

    assert page.status is TaskStatus.VERIFIED and any(c.goal_id for c in page.dod)


async def test_what_is_not_routed(supervisor: Supervisor) -> None:
    session, me, page, e2e = _two_tasks(supervisor)
    (supervisor.workspace / "tests" / "e2e.spec.ts").write_text("", encoding="utf-8")

    await supervisor._route_findings(session, me, _turn(
        ("supervisor", "question", "src/page.ts: which locale wins?"),      # a question
        ("agt_other", "warning", "src/page.ts is wrong"),                    # not to the supervisor
        ("supervisor", "warning", "tests/e2e.spec.ts is flaky"),             # its own task's file
        ("supervisor", "warning", "the board looks wrong"),                  # names no file
    ))

    assert session.state.goal == [] and not any(c.goal_id for c in page.dod + e2e.dod)


async def test_a_run_routes_only_so_many_findings(supervisor: Supervisor) -> None:
    from supervisor_harness.core.supervisor import MAX_ROUTED_FINDINGS

    session, me, _, _ = _two_tasks(supervisor, page_status="approved")
    warnings = [("supervisor", "warning", f"src/page.ts:{i} is wrong")
                for i in range(MAX_ROUTED_FINDINGS + 3)]
    await supervisor._route_findings(session, me, _turn(*warnings))

    assert len(session.state.goal) == MAX_ROUTED_FINDINGS


async def test_a_deferred_task_is_not_routed_to(supervisor: Supervisor) -> None:
    """Deferred is the owner's decision that it waits: a finding does not undo it."""
    session, me, page, _ = _two_tasks(supervisor, page_status="deferred")
    await supervisor._route_findings(session, me, _turn(
        ("supervisor", "warning", "src/page.ts:1 is wrong")))

    assert session.state.goal == [] and page.status.value == "deferred"


async def test_every_conversation_turn_is_routed(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    routed: list[str] = []
    real = Supervisor._route_findings

    async def spy(self: Supervisor, session: object, agent: AgentSpec, turn: object) -> None:
        routed.append(agent.kind.value)
        await real(self, session, agent, turn)  # type: ignore[arg-type]

    monkeypatch.setattr(Supervisor, "_route_findings", spy)
    fake = NativeFake(_calls(DONE))
    await _conversing(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)

    assert "execution" in routed and "verification" in routed
