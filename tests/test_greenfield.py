"""A project built from nothing: the harness's test bar, before the project has tests.

Found by a plan-only run on the Minesweeper brief -- an empty repository holding
only a spec. The harness adds its own test bar, "Automated tests cover the
behaviour changed by this task, ... and the suite passes", with the command the
project uses for its suite. A project built from nothing has none when the plan
is made, so the bar went on with no command, and three things followed: under
envelope approval the setup task and the page task were held for the owner as
"could not be enforced"; had they run, a criterion with no command is blocked,
never passed; and every file the plan would create was noted as naming no file.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness.agents.brief import _dod_block
from supervisor_harness.config import HarnessConfig, Policy
from supervisor_harness.contracts import parse_dod
from supervisor_harness.core import autonomy
from supervisor_harness.core.dod import apply_quality_bars, validate_criteria, verify_command
from supervisor_harness.core.placement import placed_in_tree
from supervisor_harness.models import (
    CriterionStatus,
    DoDCriterion,
    ExecutionTask,
    RunMode,
    RunState,
    Scope,
    Severity,
    TaskStatus,
    VerifyMethod,
)
from supervisor_harness.store.runstore import RunStore

from .conftest import FakeProvider
from .test_run_branch import PROMPT, _supervisor, git, make_repository

SPEC_TASK = "Build the board renderer in src/board.ts"


def _code_task(**scope: Any) -> ExecutionTask:
    return ExecutionTask(
        title=SPEC_TASK, action="Write src/board.ts and its unit tests.",
        scope=Scope(paths=scope.get("paths", ["src/**", "tests/**"])),
        dod=[DoDCriterion(statement="the board renders every cell", method=VerifyMethod.INSPECTION,
                          expect="src/board.ts: role=\"gridcell\"")])


def _bar(task: ExecutionTask) -> DoDCriterion:
    (bar,) = [c for c in task.dod if c.suite]
    return bar


# -- planning: the bar goes on, waiting for the suite ---------------------------------


def test_with_no_suite_yet_the_bar_waits_for_one_and_is_not_weak(tmp_path: Path) -> None:
    task = _code_task()
    apply_quality_bars(task, Policy(require_tests=True), tmp_path)

    bar = _bar(task)
    assert bar.method is VerifyMethod.TEST and bar.command == "" and bar.mandatory
    assert not [i for i in validate_criteria(task.dod, Policy()) if i.severity is Severity.HIGH]


def test_with_a_suite_the_bar_runs_it(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}', encoding="utf-8")
    task = _code_task()
    apply_quality_bars(task, Policy(require_tests=True), tmp_path)

    assert _bar(task).command == "npm test --silent"


def test_under_envelope_approval_the_waiting_bar_holds_nothing(tmp_path: Path) -> None:
    """On main both the setup task and the page task were held for the owner by it."""
    task = _code_task()
    apply_quality_bars(task, Policy(require_tests=True), tmp_path)

    assert autonomy.gate(task, Policy(require_tests=True)) == []


def test_a_models_own_test_with_no_command_is_still_held() -> None:
    """Only the harness waits for a suite: a model cannot claim the flag."""
    (crit,) = parse_dod([{"statement": "the suite passes", "method": "test", "command": "",
                          "suite": True, "mandatory": True}])
    task = ExecutionTask(title="t", risk="low", dod=[crit])

    assert crit.suite is False
    assert any("no command given" in i.problem for i in validate_criteria(task.dod, Policy()))
    reasons = [why for _, why in autonomy.gate(task, Policy())]
    assert any("could not be enforced" in r for r in reasons)
    assert any("will not run" in r for r in reasons)


def test_the_implementer_is_told_where_the_suite_will_come_from() -> None:
    bar = DoDCriterion(statement="the suite passes", method=VerifyMethod.TEST, suite=True)

    assert "the `test` script in package.json" in _dod_block([bar])


def test_still_no_suite_when_the_task_is_done_says_what_was_looked_for(tmp_path: Path) -> None:
    outcome = verify_command(DoDCriterion(statement="the suite passes", method=VerifyMethod.TEST,
                                          suite=True), tmp_path, timeout=30)

    assert outcome.status is CriterionStatus.BLOCKED
    assert "`test` script in package.json" in outcome.evidence


# -- verification: the suite, found on the tree the task left -------------------------


def _running(config: HarnessConfig) -> HarnessConfig:
    config.policy.allow_command_execution = True
    config.policy.require_fails_before = False
    return config


async def test_while_the_task_runs_the_waiting_bar_is_not_blocked(
    workspace: Path, config: HarnessConfig, fake: FakeProvider,
) -> None:
    sup = _supervisor(workspace, _running(config), fake)
    task = _code_task()
    apply_quality_bars(task, config.policy, workspace)
    task.status = TaskStatus.IN_PROGRESS
    session = sup.store.create(RunState(id="run_GREEN", prompt=PROMPT, workspace=str(workspace),
                                        tasks={task.id: task}))

    sup._verify_mechanically(session)

    assert _bar(session.state.tasks[task.id]).status is CriterionStatus.UNVERIFIED


async def test_a_verdict_already_given_is_not_rewritten(
    workspace: Path, config: HarnessConfig, fake: FakeProvider,
) -> None:
    """Only a bar still waiting gets the suite: a recorded verdict keeps what it ran."""
    sup = _supervisor(workspace, _running(config), fake)
    task = _code_task()
    apply_quality_bars(task, config.policy, workspace)
    _bar(task).status = CriterionStatus.BLOCKED
    task.status = TaskStatus.AWAITING_VERIFICATION
    (workspace / "tests" / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    session = sup.store.create(RunState(id="run_GREEN", prompt=PROMPT, workspace=str(workspace),
                                        tasks={task.id: task}))

    sup._find_the_suite(session, session.state.tasks[task.id], workspace)

    assert _bar(session.state.tasks[task.id]).command == ""


async def test_the_suite_found_is_on_the_log_at_once(
    workspace: Path, config: HarnessConfig, fake: FakeProvider,
) -> None:
    """Recorded when found, not when the task next settles: a run stopped between has it."""
    sup = _supervisor(workspace, _running(config), fake)
    task = _code_task()
    apply_quality_bars(task, config.policy, workspace)
    task.status = TaskStatus.AWAITING_VERIFICATION
    (workspace / "tests" / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    session = sup.store.create(RunState(id="run_GREEN", prompt=PROMPT, workspace=str(workspace),
                                        tasks={task.id: task}))

    sup._find_the_suite(session, session.state.tasks[task.id], workspace)
    (sup.store.runs_dir / "run_GREEN" / "state.json").unlink()

    assert _bar(sup.store.load_state("run_GREEN").tasks[task.id]).command == "pytest -q"


FIRST_TESTS_TASK = {
    "title": "Add rate limiting to the login endpoint",
    "action": "Add a limit in src/auth/login.py, and the project's first tests.",
    "motivation": "Credential stuffing is unconstrained.",
    "rationale_refs": [], "risk": "low", "effort": "small",
    "scope_paths": ["src/auth/**", "tests/**"],
    "dod": [{"statement": "The limiter keys on the account", "method": "inspection",
             "expect": "src/auth/login.py: account_key", "mandatory": True}],
}


class FirstTests(FakeProvider):
    """Plans a code task with no test of its own; its implementer writes the first test."""

    def _synthesis(self, request: Any) -> dict[str, Any]:
        data = super()._synthesis(request)
        data["tasks"] = [dict(FIRST_TESTS_TASK)]
        return data

    def _execution(self, request: Any) -> dict[str, Any]:
        if "Tool results" in request.messages[-1].content or len(request.messages) > 1:
            return super()._execution(request)
        return {"tool_calls": [
            {"tool": "write_file", "args": {"path": "tests/test_limit.py",
                                            "content": "def test_limit():\n    assert 10 > 9\n"}},
            {"tool": "write_file", "args": {"path": "src/auth/login.py",
                                            "content": "RATE_LIMIT = 10\naccount_key = 'email'\n"}},
        ]}


@pytest.mark.skipif(shutil.which("git") is None or shutil.which("pytest") is None,
                    reason="needs git and pytest on PATH")
async def test_the_suite_the_task_wrote_is_the_one_its_bar_runs(
    workspace: Path, config: HarnessConfig,
) -> None:
    """End to end: planned with no tests anywhere, verified by the tests it wrote."""
    make_repository(workspace)
    assert not list((workspace / "tests").glob("test_*.py")), "no suite when it is planned"
    sup = _supervisor(workspace, _running(config), FirstTests())

    response = await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = sup.store.load_state(response.run_id)
    (task,) = state.tasks.values()
    bar = _bar(task)

    assert bar.command == "pytest -q", bar
    assert bar.status is CriterionStatus.PASS, bar.evidence
    assert bar.evidence.startswith("$ pytest -q\nexit=0")
    assert any("had no test suite when task" in n.text for n in state.notes)
    store = RunStore(workspace / ".supervisor")
    (store.runs_dir / response.run_id / "state.json").unlink()   # the log alone, folded again
    reloaded = store.load_state(response.run_id)
    assert _bar(next(iter(reloaded.tasks.values()))).command == "pytest -q", "recorded, not held"
    assert git(workspace, "status", "--porcelain") == "", "the owner's tree stays clean"


# -- placement: a new kind of file is new, not misplaced ------------------------------


def test_files_of_a_kind_the_project_has_none_of_are_not_noted(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "SPEC.md").write_text("# brief\n", encoding="utf-8")
    paths = ["src/game/engine.ts", "tests/e2e/start.spec.ts", "src/styles.css", "package.json"]

    placed, notes = placed_in_tree(paths, tmp_path)

    assert placed == paths and notes == []


def test_a_path_to_a_kind_it_has_is_still_noted(tmp_path: Path) -> None:
    """Go-live run 17's shape: `src/styles/tokens.css` for `src/app/styles/tokens.css`."""
    (tmp_path / "src" / "app" / "styles").mkdir(parents=True)
    # The same kind of file in any case: `.CSS` is a stylesheet as `.css` is.
    (tmp_path / "src" / "app" / "styles" / "base.CSS").write_text("", encoding="utf-8")

    _, notes = placed_in_tree(["src/styles/tokens.css"], tmp_path)

    assert notes == ["`src/styles/tokens.css` names no file, in a directory that does not exist"]
