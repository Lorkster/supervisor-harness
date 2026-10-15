"""An agent stopped only for another task's files is checked as it stands.

Minesweeper run 17: the page and the engine both stopped because the project's
lint failed on one line of the unit tests' file, which neither could change. The
page said it needed the owner; the engine said it waited for the unit tests,
which were waiting for the engine. Both went to the owner, and the run with
them. The harness holds such a check for the file's task (#115), so neither had
reason to stop.
"""

from __future__ import annotations

import pytest

from supervisor_harness.core.conversation import IMPLEMENTER_SYSTEM
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    AgentStatus,
    AgentTurn,
    Directive,
    DirectiveKind,
    ExecutionTask,
    RunState,
    Scope,
    TaskStatus,
)
from supervisor_harness.store.runstore import RunSession


def _run(supervisor: Supervisor,
         run_id: str) -> tuple[RunSession, AgentSpec, dict[str, ExecutionTask]]:
    root = supervisor.workspace
    for rel in ("tests/engine.test.ts", "src/page.ts", "notes.txt"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x\n", encoding="utf-8")
    page = ExecutionTask(title="Build the page", scope=Scope(paths=["src/**"]),
                         status=TaskStatus.IN_PROGRESS, attempts=1)
    tests = ExecutionTask(title="Write the engine tests", scope=Scope(paths=["tests/**"]),
                          status=TaskStatus.APPROVED, attempts=1)
    me = AgentSpec(id="agt_page", kind=AgentKind.EXECUTION, task_id=page.id,
                   status=AgentStatus.RUNNING, scope=page.scope)
    state = RunState(id=run_id, prompt="p", tasks={t.id: t for t in (page, tests)},
                     agents={me.id: me})
    return supervisor.store.create(state), me, {"page": page, "tests": tests}


def _blocked(blocked_on: str, needs: str = "owner") -> AgentTurn:
    return AgentTurn(agent_id="agt_page", claimed_status=AgentStatus.BLOCKED, needs=needs,
                     blocked_on=blocked_on)


def _escalate() -> Directive:
    return Directive(agent_id="agt_page", kind=DirectiveKind.ESCALATE, rationale="blocked")


@pytest.mark.parametrize("needs", ["owner", "work", ""])
async def test_stopped_for_another_tasks_file_it_is_checked_not_escalated(
    supervisor: Supervisor, needs: str,
) -> None:
    session, me, tasks = _run(supervisor, f"run_O{len(needs)}")
    await supervisor.supervision._park_task(session, me, _blocked(
        "npm run check fails at the lint step on tests/engine.test.ts:308 (prefer-const)",
        needs), _escalate())

    assert session.state.open_escalations() == [], "nothing for the owner"
    assert tasks["page"].status is TaskStatus.IN_PROGRESS, "not parked: checked as it stands"
    assert any("stopped only for other tasks' files" in n.text
               and "'Write the engine tests'" in n.text for n in session.state.notes)


@pytest.mark.parametrize(("case", "blocked_on"), [
    ("its own file", "src/page.ts:3 and tests/engine.test.ts:308 both fail"),
    ("a file nobody owns", "notes.txt says the build needs a key"),
    ("another's and a file nobody owns", "tests/engine.test.ts:308 and notes.txt both fail"),
    ("no file", "the build needs an API key only the owner has"),
])
async def test_otherwise_it_goes_to_the_owner_as_before(
    supervisor: Supervisor, case: str, blocked_on: str,
) -> None:
    session, me, tasks = _run(supervisor, f"run_E{len(case)}")
    await supervisor.supervision._park_task(session, me, _blocked(blocked_on), _escalate())

    (escalation,) = session.state.open_escalations()
    assert escalation.task_id == tasks["page"].id and tasks["page"].status is TaskStatus.BLOCKED


async def test_not_when_that_task_cannot_change(supervisor: Supervisor) -> None:
    session, me, tasks = _run(supervisor, "run_Spent")
    tasks["tests"].status = TaskStatus.FAILED
    tasks["tests"].attempts = supervisor.config.policy.max_task_attempts
    await supervisor.supervision._park_task(session, me, _blocked(
        "lint fails on tests/engine.test.ts:308"), _escalate())
    assert len(session.state.open_escalations()) == 1


def test_the_implementer_is_told_not_to_stop_for_it() -> None:
    flat = " ".join(IMPLEMENTER_SYSTEM.split())
    assert ("A project-wide check -- the type check, the lint, the test suite -- failing only "
            "in another task's files is not yours to fix, and not a reason to stop") in flat


async def test_not_when_its_own_file_is_named_though_another_task_shares_it(
    supervisor: Supervisor,
) -> None:
    session, me, tasks = _run(supervisor, "run_Shared")
    tasks["tests"].scope.paths = ["tests/**", "src/**"]
    await supervisor.supervision._park_task(session, me, _blocked(
        "src/page.ts:3 fails the lint"), _escalate())
    assert len(session.state.open_escalations()) == 1, "it may be its own to fix"
