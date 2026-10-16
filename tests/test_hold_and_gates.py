"""From Minesweeper run 12: each task is held to the project's own checks.

(The other half of #121 -- a tester's test that fails where it reported the
product waited for the product -- is the coordinator's call now, batch O.)
Each task's files are held to the project's own static checks once it has them:
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
    DoDCriterion,
    ExecutionTask,
    RunState,
    TaskStatus,
    VerifyMethod,
)
from supervisor_harness.store.runstore import RunSession

from .test_fails_before import git, repository, write

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
