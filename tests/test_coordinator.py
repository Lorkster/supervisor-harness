"""Batch O: the coordinator picks what happens after a report; the harness checks each pick.

The cases are the shapes Minesweeper runs hit, each of which once needed a rule
of its own (#115, #116, #118, #120, #125, #126, #127): a check that fails only on
another task's file, a finding about a verified task on its last attempt, a stop
that waits on a task waiting on it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from supervisor_harness.core import coordinator
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    SUPERVISOR,
    AgentKind,
    AgentSpec,
    AgentStatus,
    AgentTurn,
    Backend,
    Checkpoint,
    CriterionStatus,
    Directive,
    DirectiveKind,
    DoDCriterion,
    ExecutionTask,
    Message,
    MessageKind,
    Phase,
    RunState,
    Scope,
    TaskStatus,
    VerifyMethod,
)
from supervisor_harness.providers.base import (
    CompletionRequest,
    CompletionResponse,
    ToolCall,
    Usage,
)

from .conftest import FakeProvider

Answer = dict[str, Any] | Callable[[CompletionRequest], dict[str, Any]]
TYPE_ERROR = "e2e/minesweeper.spec.ts(12,5): error TS2345: Argument of type 'string'"


class Coordinator(FakeProvider):
    """Answers each `decide` request with the next scripted pick list."""

    native_tools = True

    def __init__(self, *answers: Answer) -> None:
        super().__init__()
        self.answers = list(answers)
        self.asked: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if "decide" not in {t["name"] for t in request.tools or []}:
            return await super().complete(request)
        self.asked.append(request)
        answer = self.answers.pop(0) if self.answers else {"actions": []}
        args = answer(request) if callable(answer) else answer
        return CompletionResponse(text="", usage=Usage(input_tokens=10),
                                  tool_calls=[ToolCall("decide", args)])


def _passed(statement: str, command: str = "") -> DoDCriterion:
    return DoDCriterion(statement=statement, method=VerifyMethod.COMMAND if command
                        else VerifyMethod.REVIEW, command=command,
                        status=CriterionStatus.PASS, verified_by="harness")


def _writer(task: ExecutionTask, attempt: int, *files: str) -> tuple[AgentSpec, AgentTurn]:
    agent = AgentSpec(id=f"agt_{task.title.split()[0].lower()}_{attempt}",
                      kind=AgentKind.EXECUTION, task_id=task.id, attempt=attempt,
                      status=AgentStatus.DONE)
    return agent, AgentTurn(agent_id=agent.id, seq=1, files_touched=list(files))


def _run(supervisor: Supervisor, run_id: str, tasks: list[ExecutionTask],
         writes: list[tuple[AgentSpec, AgentTurn]], messages: list[Message] | None = None,
         provider: FakeProvider | None = None) -> Any:
    supervisor.config.policy.coordinator = True
    for rel in {f for _, turn in writes for f in turn.files_touched}:
        path = supervisor.workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// written by the run\n", encoding="utf-8")
    if provider is not None:
        supervisor.router.register("fake", provider)
    return supervisor.store.create(RunState(
        id=run_id, prompt="Build Minesweeper.", backend=Backend.AUTONOMOUS,
        phase=Phase.VERIFYING, tasks={t.id: t for t in tasks},
        agents={a.id: a for a, _ in writes}, turns=[t for _, t in writes],
        messages=messages or []))


def _engine_and_tests() -> tuple[ExecutionTask, ExecutionTask, ExecutionTask]:
    scaffold = ExecutionTask(title="Scaffold the project", status=TaskStatus.VERIFIED,
                             attempts=1, scope=Scope(paths=["package.json"]),
                             dod=[_passed("npm install works", "npm install")])
    engine = ExecutionTask(title="Engine", status=TaskStatus.FAILED, attempts=1,
                           scope=Scope(paths=["src/game/**"]), depends_on=[scaffold.id],
                           dod=[_passed("the engine's tests pass", "npm test"),
                                DoDCriterion(statement="the project type-checks",
                                             method=VerifyMethod.COMMAND,
                                             command="npm run typecheck",
                                             status=CriterionStatus.FAIL,
                                             evidence=f"exit 2\n{TYPE_ERROR}",
                                             verified_by="harness")])
    e2e = ExecutionTask(title="Browser tests", status=TaskStatus.VERIFIED, attempts=1,
                        scope=Scope(paths=["e2e/**"]), depends_on=[scaffold.id],
                        dod=[_passed("the browser tests pass", "npx playwright test")])
    return scaffold, engine, e2e


async def test_a_check_failed_on_another_tasks_file_moves_there(supervisor: Supervisor) -> None:
    """Run 8: the engine's type check failed only on the browser tests' spec."""
    scaffold, engine, e2e = _engine_and_tests()
    fake = Coordinator({"actions": [{"do": "move_check", "task": "T2", "criterion": "C1",
                                     "to_task": "T3",
                                     "message": "Your spec has a type error at line 12."}],
                        "summary": "the spec is the browser tests' work"})
    session = _run(supervisor, "run_Move", [scaffold, engine, e2e],
                   [_writer(e2e, 1, "e2e/minesweeper.spec.ts")], provider=fake)

    assert await supervisor._coordinate(session) is True

    state = session.state
    engine, e2e = state.tasks[engine.id], state.tasks[e2e.id]
    assert engine.status is TaskStatus.VERIFIED, "judged on the rest of its checks"
    assert all(c.command != "npm run typecheck" for c in engine.dod)
    assert e2e.status is TaskStatus.APPROVED, "the browser tests go again"
    moved = next(c for c in e2e.dod if c.command == "npm run typecheck")
    assert moved.status is CriterionStatus.UNVERIFIED
    assert "type error at line 12" in e2e.follow_up_note
    shown = fake.asked[0].messages[0].content
    assert "`e2e/minesweeper.spec.ts`: written by T3 (attempt 1); in the scope of T3" in shown
    assert TYPE_ERROR in shown
    [record] = state.coordination
    assert record.matters == [f"failed:{engine.id}:1"]
    assert record.actions[0]["do"] == "move_check" and not record.fallback
    assert coordinator.open_matters(state) == [], "each matter is put to it once"


async def test_a_refused_pick_goes_back_once_with_the_reason(supervisor: Supervisor) -> None:
    """A pick the shell cannot carry out is refused with why; a name off the list never
    gets this far -- the tool's schema refuses it in the conversation."""
    scaffold, engine, e2e = _engine_and_tests()
    fake = Coordinator({"actions": [{"do": "as_it_stands", "task": "T2"}]},
                       {"actions": [{"do": "send_back", "task": "T2",
                                     "message": "Fix the type check."}]})
    session = _run(supervisor, "run_Again", [scaffold, engine, e2e], [], provider=fake)

    await supervisor._coordinate(session)

    assert len(fake.asked) == 2
    told = fake.asked[1].messages[-1].content
    assert "as_it_stands T2: only a task that stopped is judged as it stands" in told
    assert "T2 (failed) has no pick yet" in told
    engine = session.state.tasks[engine.id]
    assert engine.status is TaskStatus.APPROVED
    assert engine.follow_up_note == "Fix the type check."
    assert "Did not pass:\n- the project type-checks" in engine.action
    assert session.state.coordination[0].refused


async def test_without_a_pick_the_harness_follows_up_as_before(supervisor: Supervisor) -> None:
    """No model that takes tools: a failed task goes back as itself, a stop to the owner."""
    scaffold, engine, e2e = _engine_and_tests()
    page = ExecutionTask(title="Page", status=TaskStatus.BLOCKED, attempts=1,
                         scope=Scope(paths=["src/main.ts"]))
    agent, turn = _writer(page, 1)
    turn.blocked_on, turn.needs = "I need the owner to choose a colour scheme", "owner"
    session = _run(supervisor, "run_Fallback", [scaffold, engine, e2e, page], [(agent, turn)])

    await supervisor._coordinate(session)

    state = session.state
    assert state.tasks[engine.id].status is TaskStatus.APPROVED
    assert state.tasks[page.id].status is TaskStatus.BLOCKED
    [escalation] = state.open_escalations()
    assert escalation.task_id == page.id and "colour scheme" in escalation.detail
    record = state.coordination[0]
    assert record.fallback and all(a.get("by") == "harness" for a in record.actions)
    assert "takes no tools natively" in record.summary


async def test_a_wait_that_would_never_end_is_refused(supervisor: Supervisor) -> None:
    """Run 17: the engine waited for the unit tests, which waited for the engine."""
    engine = ExecutionTask(title="Engine", status=TaskStatus.BLOCKED, attempts=2,
                           scope=Scope(paths=["src/game/**"]))
    tests = ExecutionTask(title="Unit tests", status=TaskStatus.APPROVED,
                          scope=Scope(paths=["tests/**"]), depends_on=[engine.id])
    agent, turn = _writer(engine, 2)
    turn.blocked_on = "lint fails in tests/engine.test.ts, which I cannot change"
    fake = Coordinator({"actions": [{"do": "wait", "task": "T1", "to_task": "T2"}]},
                       {"actions": [{"do": "as_it_stands", "task": "T1"}]})
    session = _run(supervisor, "run_Loop", [engine, tests], [(agent, turn)], provider=fake)

    await supervisor._coordinate(session)

    assert "T2 waits on this task, so it would wait for ever" in \
        fake.asked[1].messages[-1].content
    assert session.state.tasks[engine.id].status is TaskStatus.AWAITING_VERIFICATION
    assert tests.id not in session.state.tasks[engine.id].depends_on


async def test_a_finding_reopens_a_verified_task_with_a_check(supervisor: Supervisor) -> None:
    """Runs 15 and 16: a finding about a task verified on its last attempt."""
    scaffold = ExecutionTask(title="Scaffold", status=TaskStatus.VERIFIED, attempts=1,
                             scope=Scope(paths=["src/main.ts"]))
    page = ExecutionTask(title="Page", status=TaskStatus.VERIFIED, attempts=3,
                         scope=Scope(paths=["src/**"]), dod=[_passed("the page renders")])
    e2e = ExecutionTask(title="Browser tests", status=TaskStatus.IN_PROGRESS, attempts=1,
                        scope=Scope(paths=["e2e/**"]))
    tester, turn = _writer(e2e, 1)
    finding = Message(sender=tester.id, recipient=SUPERVISOR, kind=MessageKind.FINDING,
                      subject="the page ignores ?board=",
                      content="src/main.ts never reads the board parameter")
    fake = Coordinator({"actions": [{"do": "send_back", "task": "T2",
                                     "message": "Read ?board= and ?seed= in src/main.ts.",
                                     "check": "The page reads ?board= and ?seed="}]})
    writes = [_writer(scaffold, 1, "src/main.ts"), _writer(page, 3, "src/main.ts"),
              (tester, turn)]
    session = _run(supervisor, "run_Finding", [scaffold, page, e2e], writes, [finding], fake)

    await supervisor._coordinate(session)

    page = session.state.tasks[page.id]
    assert page.status is TaskStatus.APPROVED and page.finding_reopens == 1
    assert page.dod[-1].statement == "For whoever asked: The page reads ?board= and ?seed="
    assert session.state.tasks[scaffold.id].status is TaskStatus.VERIFIED
    shown = fake.asked[0].messages[0].content
    assert "written by T1 (attempt 1), then T2 (attempt 3)" in shown
    assert "A finding from T3 'Browser tests''s agent" in shown


async def test_past_the_budget_it_is_not_asked(supervisor: Supervisor) -> None:
    scaffold, engine, e2e = _engine_and_tests()
    fake = Coordinator()
    session = _run(supervisor, "run_Budget", [scaffold, engine, e2e], [], provider=fake)
    supervisor.config.policy.max_coordinator_decisions = 0

    await supervisor._coordinate(session)

    assert fake.asked == []
    assert session.state.tasks[engine.id].status is TaskStatus.APPROVED
    assert "consultations are spent" in session.state.coordination[0].summary


async def test_left_failed_is_not_sent_back_by_the_checkpoint(supervisor: Supervisor) -> None:
    scaffold, engine, e2e = _engine_and_tests()
    fake = Coordinator({"actions": [{"do": "leave_failed", "task": "T2",
                                     "message": "the approach cannot work"}]})
    session = _run(supervisor, "run_Left", [scaffold, engine, e2e], [], provider=fake)

    await supervisor._coordinate(session)

    assert session.state.tasks[engine.id].status is TaskStatus.FAILED
    assert coordinator.left_failed(session.state) == {engine.id}
    failing = Checkpoint(run_id=session.state.id, passed=False, remediation=["fix the engine"])
    assert await supervisor._remediate(session, failing) == 0, "the checkpoint leaves it"
    assert session.state.tasks[engine.id].status is TaskStatus.FAILED


async def test_a_task_out_of_attempts_is_not_sent_back(supervisor: Supervisor) -> None:
    scaffold, engine, e2e = _engine_and_tests()
    engine.attempts = supervisor.config.policy.max_task_attempts
    fake = Coordinator({"actions": [{"do": "send_back", "task": "T2", "message": "again"}]},
                       {"actions": [{"do": "leave_failed", "task": "T2"}]})
    session = _run(supervisor, "run_Spent", [scaffold, engine, e2e], [], provider=fake)

    await supervisor._coordinate(session)

    assert "has had all 3 attempts at its own work" in fake.asked[1].messages[-1].content
    assert session.state.tasks[engine.id].status is TaskStatus.FAILED


async def test_a_stop_is_put_to_the_coordinator_not_the_owner(supervisor: Supervisor) -> None:
    page = ExecutionTask(title="Page", status=TaskStatus.IN_PROGRESS, attempts=1)
    agent, turn = _writer(page, 1)
    agent.status = AgentStatus.RUNNING
    turn.blocked_on, turn.needs = "lint fails in tests/engine.test.ts", "owner"
    session = _run(supervisor, "run_Stop", [page], [(agent, turn)])

    await supervisor.supervision._park_task(
        session, agent, turn, Directive(agent_id=agent.id, kind=DirectiveKind.ESCALATE))

    assert session.state.tasks[page.id].status is TaskStatus.BLOCKED
    assert session.state.open_escalations() == []
    assert [m.kind for m in coordinator.open_matters(session.state)] == ["stopped"]


async def test_verification_consults_it_before_moving_on(supervisor: Supervisor) -> None:
    scaffold, engine, e2e = _engine_and_tests()
    fake = Coordinator({"actions": [{"do": "send_back", "task": "T2", "message": "fix it"}]})
    session = _run(supervisor, "run_Settle", [scaffold, engine, e2e], [], provider=fake)

    assert await supervisor._continue_verification(session) is None

    assert len(fake.asked) == 1
    assert session.state.tasks[engine.id].status is TaskStatus.APPROVED
    assert session.state.phase is Phase.EXECUTING


async def test_the_files_a_whole_output_names_are_kept_in_the_evidence(
    supervisor: Supervisor,
) -> None:
    """A type checker names its files first; the evidence keeps the output's tail."""
    import sys
    from pathlib import Path

    root = supervisor.workspace
    (root / "e2e").mkdir()
    (root / "e2e" / "spec.ts").write_text("x\n", encoding="utf-8")
    (root / "check.py").write_text(
        "import sys\nprint('e2e/spec.ts(3,1): error TS2345')\n"
        "for n in range(4000):\n    print('noise', n)\nsys.exit(1)\n", encoding="utf-8")
    supervisor.config.policy.allow_command_execution = True
    task = ExecutionTask(title="Engine", status=TaskStatus.AWAITING_VERIFICATION, attempts=1,
                         dod=[DoDCriterion(statement="it type-checks",
                                           method=VerifyMethod.COMMAND,
                                           command=f"{Path(sys.executable).name} check.py")])
    session = supervisor.store.create(RunState(id="run_Names", prompt="p",
                                               tasks={task.id: task}))

    supervisor._verify_mechanically(session)

    (crit,) = session.state.tasks[task.id].dod
    assert crit.status is CriterionStatus.FAIL
    assert "e2e/spec.ts(3,1)" not in crit.evidence.split("Files the whole output")[0][-3000:]
    assert "Files the whole output names: e2e/spec.ts" in crit.evidence


async def test_asked_again_a_task_it_already_settled_is_not_picked_twice(
    supervisor: Supervisor,
) -> None:
    """Run 22: the second ask was told a stop it had sent back was waiting for the owner."""
    scaffold, engine, e2e = _engine_and_tests()
    tests = ExecutionTask(title="Browser tests 2", status=TaskStatus.BLOCKED, attempts=3)
    agent, turn = _writer(tests, 3)
    turn.blocked_on = "the page never staggers its ripple"
    fake = Coordinator({"actions": [{"do": "send_back", "task": "T4", "message": "fix the case"},
                                    {"do": "as_it_stands", "task": "T2"}]},
                       {"actions": [{"do": "send_back", "task": "T4"},
                                    {"do": "send_back", "task": "T2", "message": "fix it"}]})
    session = _run(supervisor, "run_Twice", [scaffold, engine, e2e, tests], [(agent, turn)],
                   provider=fake)

    await supervisor._coordinate(session)

    refused = session.state.coordination[0].refused
    assert "send_back T4: T4 already has a pick in this decision" in refused
    assert not any("waiting for the owner" in r for r in refused)
    assert session.state.tasks[tests.id].follow_up_note == "fix the case"
    assert session.state.tasks[engine.id].status is TaskStatus.APPROVED


def test_it_is_told_what_running_showed_outweighs_the_code() -> None:
    flat = " ".join(coordinator.SYSTEM.split())
    assert ("What a test saw the running product do outweighs what you read in the "
            "product's code") in flat
    assert "never ask it to make a test check less" in flat
