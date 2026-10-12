"""A criterion that runs a test file the workspace does not have, caught at planning.

Baseline 3: "The existing tile-loader 404 absent-tile behavior still passes
after the code split", run as `npm run test -- tests/data/tiles/loader.test.ts`
-- a file that never existed. It failed before the change and after it, and said
nothing about the tile loader either time. In two earlier runs a task's criterion
ran `e2e/offline.spec.ts` in a plan where no task's scope reached `e2e/`, and
both tasks ended deferred. Each is a question about the plan and the tree, so it
is asked before anyone works.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness.config import Policy
from supervisor_harness.core import phases
from supervisor_harness.core.placement import criteria_running_missing_files
from supervisor_harness.core.planner import MISSING_FILE_FIX, send_back
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    DoDCriterion,
    ExecutionTask,
    RunMode,
    Scope,
    VerifyMethod,
)
from supervisor_harness.providers.base import CompletionRequest

from .test_planner_conversation import Planner, _planning
from .test_send_back_criteria import PROMPT

TILES = "The existing tile-loader 404 absent-tile behavior still passes after the code split"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for path in ("tests/data/places.test.ts", "tests/data/realTiles.test.ts",
                 "tests/data/catalogue.test.ts", "e2e/smoke.spec.ts", "tests/test_login.py"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("", encoding="utf-8")
    return tmp_path


def _task(statement: str, command: str, paths: list[str],
          method: VerifyMethod = VerifyMethod.TEST, title: str = "t") -> ExecutionTask:
    return ExecutionTask(title=title, scope=Scope(paths=paths), dod=[DoDCriterion(
        statement=statement, method=method, command=command, mandatory=False)])


def _flagged(tasks: list[ExecutionTask], tree: Path) -> list[str]:
    return [line for _, line in criteria_running_missing_files(tasks, tree)]


# -- what is caught ---------------------------------------------------------------------


def test_a_test_said_to_exist_that_does_not_is_caught_with_the_nearest_files(tree: Path) -> None:
    """Baseline 3's criterion, in a scope that could have created the file."""
    (line,) = _flagged([_task(TILES, "npm run test -- tests/data/tiles/loader.test.ts",
                              ["tests/data/"])], tree)

    assert "tests/data/tiles/loader.test.ts, which is not in the workspace" in line
    assert "says that test is already there" in line
    assert "tests/data/realTiles.test.ts" in line, "the planner is told what is there"


def test_a_new_test_no_task_could_write_is_caught(tree: Path) -> None:
    """Two earlier runs: the plan's scopes never reached `e2e/`, and both tasks deferred."""
    tasks = [_task("The offline spec passes", "npx playwright test e2e/offline.spec.ts",
                   ["src/", "tests/"], title="Write the offline spec"),
             _task("the unit tests pass", "pytest tests/test_login.py", ["src/"])]

    (line,) = _flagged(tasks, tree)

    assert "task 'Write the offline spec'" in line and "no task's scope" in line
    assert "e2e/smoke.spec.ts" in line


def test_the_same_name_elsewhere_is_the_nearest_file(tree: Path) -> None:
    (tree / "tests" / "auth").mkdir()
    (tree / "tests" / "auth" / "test_logout.py").write_text("", encoding="utf-8")  # closer, wrong
    (line,) = _flagged([_task("the existing login tests still pass",
                              "pytest tests/auth/test_login.py", ["src/"])], tree)

    assert line.endswith("the nearest files that are: tests/test_login.py")


@pytest.mark.parametrize("statement", [
    "The login lockout handling still passes after the refactor",   # no "existing"
    "Pre-existing lockout tests are green",
])
def test_either_wording_says_the_test_is_there(tree: Path, statement: str) -> None:
    (line,) = _flagged([_task(statement, "pytest tests/auth/test_lockout.py::test_lock -q",
                              ["tests/"])], tree)

    assert "runs tests/auth/test_lockout.py," in line, "the file of a node id"


# -- what is not ------------------------------------------------------------------------


@pytest.mark.parametrize("paths", [
    ["tests/data/", "e2e/"],                            # directories it may write in
    ["tests/data/http.test.ts", "e2e/offline.spec.ts"],  # the files themselves
    [],                                                 # no paths is the whole workspace
])
def test_a_new_test_a_task_will_write_is_left_alone(tree: Path, paths: list[str]) -> None:
    task = _task("A unit test asserts an offline fetch yields 'network-unavailable'",
                 "npx vitest run tests/data/http.test.ts e2e/offline.spec.ts", paths)

    assert _flagged([task], tree) == []


def test_another_tasks_scope_can_write_it(tree: Path) -> None:
    tasks = [_task("The offline spec passes", "npx playwright test e2e/offline.spec.ts",
                   ["src/"]),
             _task("Write the spec", "npx playwright test e2e/smoke.spec.ts", ["e2e/"])]

    assert _flagged(tasks, tree) == []


@pytest.mark.parametrize("statement", [
    "A new function in core/timing.py using the existing _union_seconds helper",
    "The component shows the existing error state, not the offline one",
    "fetchJson still throws 'fetch-failed' for a non-2xx response",
])
def test_words_about_the_code_do_not_say_the_test_exists(tree: Path, statement: str) -> None:
    assert _flagged([_task(statement, "pytest tests/test_new.py", ["tests/"])], tree) == []


def test_a_test_that_must_fail_first_is_new_by_definition(tree: Path) -> None:
    task = _task("the existing behaviour test still passes once fixed",
                 "pytest tests/test_new.py", ["tests/"], method=VerifyMethod.FAILS_BEFORE)

    assert _flagged([task], tree) == []


@pytest.mark.parametrize("command", [
    "pytest tests/test_login.py::test_lockout -q",       # a node id, of a file that is there
    "pytest -q --cov=src/missing/pkg.py tests/test_login.py",   # flags and values
    "pytest tests/missing/*.py",                         # a glob says nothing
    "pytest /abs/tests/test_x.py ../elsewhere/test_y.py",   # not the workspace's
    "npm run check",                                     # no path at all
])
def test_what_is_not_a_missing_workspace_file(tree: Path, command: str) -> None:
    assert _flagged([_task("the existing tests still pass", command, ["src/"])], tree) == []


def test_a_criterion_with_nothing_to_run_or_no_workspace_is_not_judged(tree: Path) -> None:
    review = _task(TILES, "", ["src/"], method=VerifyMethod.REVIEW)
    review.dod[0].command = "pytest tests/missing.py"
    assert _flagged([review], tree) == []
    command = _task(TILES, "pytest tests/missing/test_x.py", ["src/"])
    assert criteria_running_missing_files([command], None) == []
    assert criteria_running_missing_files([command], tree / "nowhere") == []


# -- where it goes ----------------------------------------------------------------------


def test_the_planner_is_sent_back_for_it_and_the_owner_sees_what_stayed(tree: Path) -> None:
    tasks = [_task(TILES, "npm run test -- tests/data/tiles/loader.test.ts", ["tests/data/"])]

    def missing(workspace: Path | None) -> list[str]:
        return [line for line in phases.unenforceable_criteria(tasks, Policy(), workspace)
                if "which is not in the workspace" in line]

    assert missing(None) == [], "no workspace, no judgement"
    (weak,) = missing(tree)
    assert "tests/data/tiles/loader.test.ts" in weak

    _, notes = phases.prepare_tasks(tasks, Policy(), tree)
    assert any("criterion runs a missing file" in n and "loader.test.ts" in n
               for n in notes[tasks[0].id])

    assert MISSING_FILE_FIX in send_back([weak])
    assert MISSING_FILE_FIX in phases.revision_prompt("plan it", [weak])


class MissingFirst(Planner):
    """Proposes a criterion on a test file that is not there, until it is told."""

    def plan(self, weak: bool) -> dict[str, Any]:
        data = copy.deepcopy(self._synthesis(CompletionRequest()))
        if weak:
            data["tasks"][0]["dod"].append({
                "statement": "The existing login tests still pass",
                "method": "test", "command": "pytest tests/auth/test_login.py -q",
                "expect": "", "mandatory": True})
        return data


async def test_the_planner_conversation_hears_about_it_and_the_revision_stands(
    supervisor: Supervisor,
) -> None:
    (supervisor.workspace / "tests" / "test_login.py").write_text("", encoding="utf-8")
    fake = MissingFirst(weak_first=True)

    response = await _planning(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE,
                                                     auto_approve=True)
    state = supervisor.store.load_state(response.run_id)
    (task,) = state.tasks.values()

    sent = [m.content for m in fake.planner[-1].messages
            if m.role == "user" and "cannot be checked by the harness" in m.content]
    assert sent and "tests/auth/test_login.py" in sent[0]
    assert "the nearest files that are: tests/test_login.py" in sent[0]
    assert all("tests/auth/test_login.py" not in c.command for c in task.dod), (
        "the revision stood")
