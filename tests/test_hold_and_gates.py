"""Two from Minesweeper run 12.

A tester's test that fails where its writer reported the product wrong waits for
the product, rather than failing the tester three times for keeping it. And each
task's files are held to the project's own static checks once it has them:
Playwright runs a spec without type-checking it, and in runs 8 and 12 a spec's
type errors broke `npm run check` and the build with every task's checks green.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    BASELINE_FACT,
    AcceptanceTests,
    AgentKind,
    AgentSpec,
    AgentStatus,
    CriterionStatus,
    DoDCriterion,
    ExecutionTask,
    GoalExpectation,
    RunState,
    Scope,
    TaskStatus,
    VerifyMethod,
)
from supervisor_harness.store.runstore import RunSession

from .test_charge_the_owner import project  # noqa: F401 - the fixture
from .test_fails_before import git, repository, write

# -- a test that fails where its writer reported the product wrong -------------------------

E2E = "python check.py e2e"
REPORTED = "e2e/spec.ts(3,1): expected cell (1,1) revealed after Space, found hidden\n"


def _reported(supervisor: Supervisor, run_id: str, *, finding: bool = True,
              attempt: int = 1) -> tuple[RunSession, ExecutionTask, ExecutionTask]:
    page = ExecutionTask(title="Build the page", scope=Scope(paths=["src/engine.ts"]),
                         status=TaskStatus.APPROVED, attempts=1)
    tests = ExecutionTask(title="Write the browser tests", scope=Scope(paths=["e2e/**"]),
                          status=TaskStatus.AWAITING_VERIFICATION, attempts=1,
                          acceptance=[AcceptanceTests(["R1"], E2E)],
                          dod=[DoDCriterion(statement="the browser tests pass",
                                            method=VerifyMethod.COMMAND, command=E2E)])
    tester = AgentSpec(id="agt_t", kind=AgentKind.EXECUTION, task_id=tests.id,
                       attempt=attempt, status=AgentStatus.DONE)
    goal = [GoalExpectation(statement="Space acts on another cell", task_id=page.id,
                            source="agent agt_t")] if finding else []
    state = RunState(id=run_id, prompt="p", tasks={t.id: t for t in (page, tests)},
                     agents={tester.id: tester}, goal=goal)
    return supervisor.store.create(state), page, tests


def _break(root: Path, output: str = REPORTED) -> None:
    (root / "broken.txt").write_text(output, encoding="utf-8")


def test_a_test_failing_where_its_writer_reported_the_product_waits_for_it(
    supervisor: Supervisor, project: Path,  # noqa: F811
) -> None:
    session, page, tests = _reported(supervisor, "run_Rep")
    _break(project)

    supervisor._verify_mechanically(session)

    (crit,) = tests.dod
    assert crit.status is CriterionStatus.UNVERIFIED and crit.held_for == [page.id]
    assert crit.evidence.startswith("held for 'Build the page': its tests fail where this "
                                    "task reported their work wrong")
    assert page.dod == [], "the finding is on it already; its tests are not"
    supervisor._settle_tasks(session)
    assert tests.status is TaskStatus.AWAITING_VERIFICATION, "not failed for keeping it"


@pytest.mark.parametrize("case", ["no finding", "an earlier attempt's finding",
                                  "not its tests", "the product cannot change"])
def test_otherwise_the_failure_is_the_testers(
    supervisor: Supervisor, project: Path, case: str,  # noqa: F811
) -> None:
    session, page, tests = _reported(supervisor, f"run_T{len(case)}",
                                     finding=case != "no finding",
                                     attempt=2 if case == "an earlier attempt's finding" else 1)
    if case == "not its tests":
        tests.acceptance = [AcceptanceTests(["R1"], "python check.py other")]
    if case == "the product cannot change":
        page.status, page.attempts = TaskStatus.FAILED, supervisor.config.policy.max_task_attempts
    _break(project)

    supervisor._verify_mechanically(session)

    assert tests.dod[0].status is CriterionStatus.FAIL, case


# -- the project's own static checks ------------------------------------------------------

pytestmark_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _package(scripts: dict[str, str]) -> str:
    return json.dumps({"name": "game", "scripts": scripts})


def _gated(supervisor: Supervisor, root: Path, run_id: str,
           suite: bool = True) -> tuple[RunSession, ExecutionTask]:
    task = ExecutionTask(title="Write the browser tests", status=TaskStatus.AWAITING_VERIFICATION,
                         attempts=1, dod=[DoDCriterion(statement="tests", method=VerifyMethod.TEST,
                                                       command="npm test", suite=suite)])
    state = RunState(id=run_id, prompt="p", tasks={task.id: task})
    state.facts[BASELINE_FACT] = f"`{git(root, 'rev-parse', 'HEAD')}` on `main`"
    return supervisor.store.create(state), task


@pytestmark_git
def test_a_task_is_held_to_the_projects_own_checks(
    supervisor: Supervisor, tmp_path: Path,
) -> None:
    root = tmp_path / "game"
    repository(root, {"README.md": "a game\n"})
    write(root, {"package.json": _package({"test": "vitest run", "typecheck": "tsc --noEmit",
                                           "lint": "eslint ."})})
    session, task = _gated(supervisor, root, "run_G")

    supervisor._find_the_gates(session, task, root)

    gates = {c.command: c for c in task.dod if c.command.startswith("npm run ")}
    assert set(gates) == {"npm run typecheck", "npm run lint"}
    assert all(c.mandatory and c.method is VerifyMethod.COMMAND for c in gates.values()), (
        "this run made them: a failure is its own")
    assert any("held to the project's own checks" in n.text for n in session.state.notes)
    supervisor._find_the_gates(session, task, root)
    assert len(task.dod) == 3, "once"


@pytestmark_git
def test_a_check_the_project_had_before_is_reported_not_required(
    supervisor: Supervisor, tmp_path: Path,
) -> None:
    root = tmp_path / "game"
    repository(root, {"package.json": _package({"lint": "eslint ."})})
    write(root, {"package.json": _package({"lint": "eslint .", "typecheck": "tsc --noEmit"})})
    session, task = _gated(supervisor, root, "run_Before")

    supervisor._find_the_gates(session, task, root)

    by = {c.command: c.mandatory for c in task.dod if c.command.startswith("npm run ")}
    assert by == {"npm run typecheck": True, "npm run lint": False}


@pytestmark_git
def test_not_without_the_harness_test_bar_nor_without_the_scripts(
    supervisor: Supervisor, tmp_path: Path,
) -> None:
    root = tmp_path / "game"
    repository(root, {"README.md": "a game\n"})
    write(root, {"package.json": _package({"typecheck": "tsc --noEmit"})})
    session, task = _gated(supervisor, root, "run_No", suite=False)
    supervisor._find_the_gates(session, task, root)
    assert len(task.dod) == 1, "not a task the harness's bars are on"

    write(root, {"package.json": _package({"test": "vitest run"})})
    session, task = _gated(supervisor, root, "run_None")
    supervisor._find_the_gates(session, task, root)
    assert len(task.dod) == 1, "no such scripts"


def test_verification_finds_them_when_the_task_is_done(supervisor: Supervisor) -> None:
    supervisor.config.policy.allow_command_execution = False   # found, not run, here
    (supervisor.workspace / "package.json").write_text(
        _package({"test": "vitest run", "typecheck": "tsc --noEmit"}), encoding="utf-8")
    task = ExecutionTask(title="Write the browser tests", status=TaskStatus.AWAITING_VERIFICATION,
                         attempts=1, dod=[DoDCriterion(statement="tests", method=VerifyMethod.TEST,
                                                       command="npm test", suite=True)])
    session = supervisor.store.create(RunState(id="run_Done", prompt="p",
                                               tasks={task.id: task}))

    supervisor._verify_mechanically(session)

    assert [c.command for c in task.dod] == ["npm test", "npm run typecheck"]
