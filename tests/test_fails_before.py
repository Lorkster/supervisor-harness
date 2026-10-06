"""The verifier that is not a model: a task's tests must fail without the task.

Every test here builds a real git repository and runs real pytest in it, twice:
once in a worktree at the baseline commit and once in the working tree. Nothing
is mocked, because what is being proven is that the harness runs the right code
on each side -- and the one way that goes wrong (the baseline side importing the
changed code) is invisible to anything but a real run.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness.config import Policy
from supervisor_harness.contracts import VERIFY_METHODS
from supervisor_harness.core.dod import apply_quality_bars
from supervisor_harness.core.fails_before import (
    is_test_module,
    is_test_path,
    safe_relative,
    select_test_material,
    verify_fails_before,
)
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    CriterionStatus,
    DoDCriterion,
    ExecutionTask,
    RunMode,
    Scope,
    TaskStatus,
    VerifyMethod,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

#: The runner as the harness would be told it, but pinned to this interpreter so
#: the test does not depend on which `pytest` is first on PATH.
PYTEST = f'"{sys.executable}" -m pytest -q'


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@localhost",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def repository(root: Path, files: dict[str, str]) -> str:
    """A repository holding ``files`` in one commit; returns that commit."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    write(root, files)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "baseline")
    return git(root, "rev-parse", "HEAD")


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def criterion(command: str = PYTEST) -> DoDCriterion:
    return DoDCriterion(statement="tests fail before", method=VerifyMethod.FAILS_BEFORE,
                        command=command)


BUGGY = {
    # `pythonpath`, as a flat-layout project sets it, so a bare `pytest` (what
    # the harness detects and runs) can import the module under test.
    "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\npythonpath = ['.']\n",
    "calc.py": "def add(a, b):\n    return a - b\n",
    "tests/test_existing.py": "def test_nothing():\n    assert True\n",
}


DETECTING = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    return root, repository(root, BUGGY)


# -- the verdicts --------------------------------------------------------------


def test_a_test_that_detects_the_fix_passes(repo: tuple[Path, str]) -> None:
    root, base = repo
    write(root, {
        "calc.py": "def add(a, b):\n    return a + b\n",
        "tests/test_calc.py": DETECTING,
    })
    outcome = verify_fails_before(criterion(), ["tests/test_calc.py"], root, base)

    assert outcome.status is CriterionStatus.PASS, outcome.evidence
    assert "test_add" in outcome.evidence
    assert "could not be imported" not in outcome.evidence


def test_a_test_that_passes_without_the_change_fails_the_criterion(
    repo: tuple[Path, str],
) -> None:
    """The defect this exists for: a test that would have been green anyway."""
    root, base = repo
    write(root, {
        "calc.py": "def add(a, b):\n    return a + b\n",
        "tests/test_calc.py": "from calc import add\n\ndef test_add_zero():\n"
                              "    assert add(0, 0) == 0\n",
    })
    outcome = verify_fails_before(criterion(), ["tests/test_calc.py"], root, base)

    assert outcome.status is CriterionStatus.FAIL, outcome.evidence
    assert "already passes on the baseline" in outcome.evidence
    assert "test_add_zero" in outcome.evidence


def test_a_test_for_a_new_module_counts_and_says_why(repo: tuple[Path, str]) -> None:
    root, base = repo
    write(root, {
        "shapes.py": "def area(w, h):\n    return w * h\n",
        "tests/test_shapes.py": "from shapes import area\n\ndef test_area():\n"
                                "    assert area(2, 3) == 6\n",
    })
    outcome = verify_fails_before(criterion(), ["tests/test_shapes.py"], root, base)

    assert outcome.status is CriterionStatus.PASS, outcome.evidence
    assert "could not be imported" in outcome.evidence, (
        "a pass earned only by an import error must say so"
    )


def test_tests_that_fail_with_the_change_fail_the_criterion(repo: tuple[Path, str]) -> None:
    root, base = repo
    write(root, {"tests/test_calc.py": "from calc import add\n\ndef test_add():\n"
                                       "    assert add(2, 3) == 5\n"})
    outcome = verify_fails_before(criterion(), ["tests/test_calc.py"], root, base)

    assert outcome.status is CriterionStatus.FAIL
    assert "do not pass with the change" in outcome.evidence


def test_a_task_that_changed_no_test_module_fails(repo: tuple[Path, str]) -> None:
    root, base = repo
    outcome = verify_fails_before(criterion(), ["tests/conftest.py"], root, base)
    assert outcome.status is CriterionStatus.FAIL
    assert "changed no test module" in outcome.evidence


@pytest.mark.parametrize(("command", "baseline", "expected"), [
    ("npm test", "HEAD", "not a pytest invocation"),
    ("pytest -q; rm -rf /", "HEAD", "metacharacter"),
    (PYTEST, "", "no baseline commit"),
])
def test_what_it_cannot_run_it_leaves_blocked_for_the_verifier(
    repo: tuple[Path, str], command: str, baseline: str, expected: str,
) -> None:
    root, base = repo
    write(root, {"tests/test_calc.py": "def test_x():\n    assert True\n"})
    outcome = verify_fails_before(criterion(command), ["tests/test_calc.py"], root,
                                  base if baseline == "HEAD" else baseline)
    assert outcome.status is CriterionStatus.BLOCKED
    assert expected in outcome.evidence


def test_the_worktree_is_gone_afterwards(repo: tuple[Path, str]) -> None:
    root, base = repo
    write(root, {
        "calc.py": "def add(a, b):\n    return a + b\n",
        "tests/test_calc.py": DETECTING,
    })
    verify_fails_before(criterion(), ["tests/test_calc.py"], root, base)
    assert len(git(root, "worktree", "list").splitlines()) == 1


def test_the_baseline_side_imports_the_baseline_code_not_the_installed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An editable install points every interpreter at the working tree.

    Simulated with ``PYTHONPATH`` naming the working tree's ``src``, which is
    what an editable install's path entry does. Without the harness setting the
    baseline run's own ``PYTHONPATH``, the baseline side would test the fixed
    code, the test would pass there, and a real fix would be failed as untested.
    """
    root = tmp_path / "repo"
    base = repository(root, {
        "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        "src/pkg/__init__.py": "def add(a, b):\n    return a - b\n",
    })
    write(root, {
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_pkg.py": "from pkg import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    })
    monkeypatch.setenv("PYTHONPATH", str(root / "src"))

    outcome = verify_fails_before(criterion(), ["tests/test_pkg.py"], root, base)
    assert outcome.status is CriterionStatus.PASS, outcome.evidence


# -- which files are the task's -------------------------------------------------


@pytest.mark.parametrize(("path", "module", "material"), [
    ("tests/test_a.py", True, True),
    ("pkg/a_test.py", True, True),
    ("tests/conftest.py", False, True),
    ("tests/fixtures/data.json", False, True),
    ("src/calc.py", False, False),
    ("src/testing_utils.py", False, False),
])
def test_what_counts_as_test_material(path: str, module: bool, material: bool) -> None:
    assert is_test_module(path) is module
    assert is_test_path(path) is material


def test_paths_that_could_reach_a_command_line_as_options_are_dropped(
    repo: tuple[Path, str],
) -> None:
    root, _ = repo
    write(root, {"tests/test_ok.py": "", "tests/-p.py": "", ".git/hooks/test_x.py": ""})
    assert safe_relative("tests/test_ok.py", root) == "tests/test_ok.py"
    assert safe_relative(str(root / "tests" / "test_ok.py"), root) == "tests/test_ok.py"
    for bad in ("tests/-p.py", "../repo/tests/test_ok.py", ".git/hooks/test_x.py",
                "tests/missing_test.py", "/etc/passwd"):
        assert safe_relative(bad, root) is None, bad
    assert select_test_material(["tests/test_ok.py", "calc.py", "tests/-p.py"], root) == [
        "tests/test_ok.py"
    ]


# -- when the bar is added -----------------------------------------------------


#: The bar is the harness's own check, so it is only added where the harness may
#: run the tests itself.
RUNS_COMMANDS = Policy(allow_command_execution=True)


def _task(title: str) -> ExecutionTask:
    return ExecutionTask(title=title, action=f"{title} in calc.py",
                         dod=[DoDCriterion(statement="x", method=VerifyMethod.INSPECTION,
                                           expect="calc.py: def")])


def _has_bar(task: ExecutionTask) -> bool:
    return any(c.method is VerifyMethod.FAILS_BEFORE for c in task.dod)


def test_the_bar_is_added_to_a_behaviour_change_in_a_git_pytest_workspace(
    repo: tuple[Path, str],
) -> None:
    root, _ = repo
    task = _task("Fix the addition bug")
    added = apply_quality_bars(task, RUNS_COMMANDS, root)
    bar = next(c for c in added if c.method is VerifyMethod.FAILS_BEFORE)
    assert bar.mandatory and bar.command and bar.rubric


@pytest.mark.parametrize("title", ["Refactor the calc module", "Update the README",
                                   "Rename add to plus", "Speed up the addition"])
def test_the_bar_is_not_added_where_no_behaviour_should_change(
    repo: tuple[Path, str], title: str,
) -> None:
    root, _ = repo
    task = _task(title)
    apply_quality_bars(task, RUNS_COMMANDS, root)
    assert not _has_bar(task)


def test_the_bar_needs_git_and_can_be_turned_off(tmp_path: Path, repo: tuple[Path, str]) -> None:
    plain = tmp_path / "plain"
    write(plain, {"pyproject.toml": "", "calc.py": "def add(a, b):\n    return a\n"})
    task = _task("Fix the addition bug")
    apply_quality_bars(task, RUNS_COMMANDS, plain)
    assert not _has_bar(task), "no git, no baseline to run the tests on"

    root, _ = repo
    task = _task("Fix the addition bug")
    apply_quality_bars(task, Policy(allow_command_execution=True, require_fails_before=False),
                       root)
    assert not _has_bar(task)


def test_the_bar_is_not_handed_to_a_model_when_the_harness_cannot_run_it(
    repo: tuple[Path, str],
) -> None:
    """Without command execution it would be a verifier agent's claim: the thing it replaces."""
    root, _ = repo
    task = _task("Fix the addition bug")
    apply_quality_bars(task, Policy(allow_command_execution=False), root)
    assert not _has_bar(task)


def test_models_are_not_offered_the_method() -> None:
    assert "fails_before" not in VERIFY_METHODS


# -- end to end ----------------------------------------------------------------


def _one_task_synthesis() -> dict[str, Any]:
    return {
        "summary": "add() subtracts.",
        "conflicts": [], "open_questions": [], "recommended_mode": "execute",
        "tasks": [{
            "title": "Fix the addition bug",
            "action": "Make add in calc.py return the sum, and test it.",
            "motivation": "add returns a - b.",
            "rationale_refs": [],
            "dod": [
                {"statement": "calc.py returns the sum", "method": "inspection",
                 "expect": "calc.py: a + b", "mandatory": True},
                {"statement": "the suite exits 0", "method": "test",
                 "command": "pytest -q", "expect": "0", "mandatory": True},
            ],
            "scope_paths": ["calc.py", "tests/**"],
            "risk": "low", "effort": "small",
        }],
    }


def _executing(supervisor: Supervisor, test_body: str):  # type: ignore[no-untyped-def]
    """An execution agent writing where a real one's tools would: the run's own tree."""
    def execute(request: Any) -> dict[str, Any]:
        state = supervisor.store.load_state(supervisor.store.latest_run_id() or "")
        wt = state.worktree
        tree = Path(wt.path) if wt is not None and wt.path else supervisor.workspace
        write(tree, {"calc.py": "def add(a, b):\n    return a + b\n",
                     "tests/test_calc.py": f"from calc import add\n\n{test_body}"})
        return {"output": "fixed add and tested it",
                "files_touched": ["calc.py", "tests/test_calc.py"],
                "commands_run": [], "criteria_progress": [], "status": "done"}
    return execute


@pytest.fixture
def git_supervisor(repo: tuple[Path, str], config, fake) -> Supervisor:  # type: ignore[no-untyped-def]
    from supervisor_harness.host.detect import HostInfo
    from supervisor_harness.providers.router import ModelRouter
    from supervisor_harness.store.runstore import RunStore

    root, _ = repo
    (root / ".gitignore").write_text(".supervisor/\n", encoding="utf-8")
    git(root, "add", ".gitignore")
    git(root, "commit", "-q", "-m", "ignore the store")
    config.policy.allow_command_execution = True
    config.policy.max_checkpoint_iterations = 1
    host = HostInfo(name="test-host", workspace=str(root), confidence=1.0)
    router = ModelRouter(config, host_name=host.name)
    router.register("fake", fake)
    fake.overrides["synthesis"] = _one_task_synthesis()
    return Supervisor(workspace=root, config=config, store=RunStore(root / ".supervisor"),
                      host=host, router=router)


@pytest.mark.skipif(shutil.which("pytest") is None, reason="the bar runs `pytest` from PATH")
async def test_a_run_proves_the_bar_itself_and_a_verifier_cannot_overturn_it(
    git_supervisor: Supervisor, fake, repo: tuple[Path, str],
) -> None:
    fake.script("execution",
                _executing(git_supervisor, "def test_add():\n    assert add(2, 3) == 5\n"))

    response = await git_supervisor.run("Fix add", mode=RunMode.EXECUTE, auto_approve=True)
    task = next(iter(git_supervisor.store.load_state(response.run_id).tasks.values()))
    bar = next(c for c in task.dod if c.method is VerifyMethod.FAILS_BEFORE)

    assert bar.status is CriterionStatus.PASS, bar.evidence
    assert bar.verified_by == "harness", "proven by running the tests, not by an agent"
    assert task.status is TaskStatus.VERIFIED


@pytest.mark.skipif(shutil.which("pytest") is None, reason="the bar runs `pytest` from PATH")
async def test_a_run_whose_tests_pass_on_the_baseline_ends_with_the_task_failed(
    git_supervisor: Supervisor, fake, repo: tuple[Path, str],
) -> None:
    """The fake verifier passes every criterion; the harness's verdict stands."""
    fake.script("execution",
                _executing(git_supervisor, "def test_zero():\n    assert add(0, 0) == 0\n"))

    response = await git_supervisor.run("Fix add", mode=RunMode.EXECUTE, auto_approve=True)
    task = next(iter(git_supervisor.store.load_state(response.run_id).tasks.values()))
    bar = next(c for c in task.dod if c.method is VerifyMethod.FAILS_BEFORE)

    assert bar.status is CriterionStatus.FAIL, bar.evidence
    assert "already passes on the baseline" in bar.evidence
    assert task.status is TaskStatus.FAILED


def test_a_workspace_config_cannot_turn_the_bar_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository that could set this would choose whether its own tests are checked."""
    from supervisor_harness.config import load_config

    monkeypatch.setenv("SUPERVISOR_HOME", str(tmp_path / "home"))
    (tmp_path / "supervisor.config.json").write_text(
        '{"policy": {"require_fails_before": false}}', encoding="utf-8",
    )
    cfg = load_config(workspace=tmp_path)
    assert cfg.policy.require_fails_before is True
    assert any("require_fails_before" in r for r in cfg.rejected_settings)


def test_it_does_not_run_while_the_task_is_still_being_written(
    supervisor: Supervisor, repo: tuple[Path, str],
) -> None:
    """Only once the task's change is complete: half a change proves nothing either way."""
    from types import SimpleNamespace

    from supervisor_harness.models import RunState

    root, base = repo
    supervisor.workspace = root
    supervisor.config.policy.allow_command_execution = True
    bar = criterion()
    task = ExecutionTask(title="Fix add", dod=[bar], status=TaskStatus.IN_PROGRESS)
    emitted: list[Any] = []
    session = SimpleNamespace(
        state=RunState(tasks={task.id: task}, facts={"baseline commit": f"`{base}`"}),
        emit=lambda *args, **kwargs: emitted.append(args),
    )

    supervisor._verify_mechanically(session)  # type: ignore[arg-type]
    assert emitted == [], "a task still in progress must not have its bar run"

    task.status = TaskStatus.AWAITING_VERIFICATION
    supervisor._verify_mechanically(session)  # type: ignore[arg-type]
    assert emitted, "the same task, finished, is checked"


# -- a task that only adds tests -------------------------------------------------


def test_tests_for_existing_behaviour_pass_when_only_tests_changed(
    repo: tuple[Path, str],
) -> None:
    """Covering what already works is meant to pass on the baseline."""
    root, base = repo
    write(root, {"tests/test_calc.py": "from calc import add\n\ndef test_add_now():\n"
                                       "    assert add(5, 3) == 2\n"})
    outcome = verify_fails_before(criterion(), ["tests/test_calc.py"], root, base,
                                  only_tests=True)
    assert outcome.status is CriterionStatus.PASS, outcome.evidence
    assert "changed only test material" in outcome.evidence

    failing = verify_fails_before(criterion(), ["tests/test_calc.py"], root, base,
                                  only_tests=False)
    assert failing.status is CriterionStatus.FAIL, "the same tests, on a behaviour change"


def _state_with_touched(base: str, task: ExecutionTask, touched: list[str]) -> Any:
    from supervisor_harness.models import AgentKind, AgentSpec, AgentTurn, RunState

    agent = AgentSpec(kind=AgentKind.EXECUTION, task_id=task.id)
    return RunState(tasks={task.id: task}, agents={agent.id: agent},
                    turns=[AgentTurn(agent_id=agent.id, files_touched=touched)],
                    facts={"baseline commit": f"`{base}`"})


def test_a_task_that_touched_source_is_compared_even_if_its_agent_said_tests_only(
    supervisor: Supervisor, repo: tuple[Path, str],
) -> None:
    """The tree is read as well as the agent's report, and it errs towards comparing."""
    root, base = repo
    write(root, {"calc.py": "def add(a, b):\n    return a - b  # unchanged behaviour\n",
                 "tests/test_calc.py": "from calc import add\n\ndef test_add_now():\n"
                                       "    assert add(5, 3) == 2\n"})
    supervisor.workspace = root
    task = ExecutionTask(title="Cover add", dod=[criterion()],
                         scope=Scope(paths=["calc.py", "tests/**"]))
    bar = task.dod[0]

    reported_tests_only = _state_with_touched(base, task, ["tests/test_calc.py"])
    outcome = supervisor._verify_fails_before(reported_tests_only, task, bar)
    assert outcome.status is CriterionStatus.FAIL, (
        "calc.py changed in the tree, so this is not a tests-only task: " + outcome.evidence
    )


def test_a_task_that_only_changed_tests_is_judged_by_them_passing(
    supervisor: Supervisor, repo: tuple[Path, str],
) -> None:
    root, base = repo
    write(root, {"tests/test_calc.py": "from calc import add\n\ndef test_add_now():\n"
                                       "    assert add(5, 3) == 2\n"})
    supervisor.workspace = root
    task = ExecutionTask(title="Cover add", dod=[criterion()])
    outcome = supervisor._verify_fails_before(
        _state_with_touched(base, task, ["tests/test_calc.py"]), task, task.dod[0])
    assert outcome.status is CriterionStatus.PASS, outcome.evidence
    assert "changed only test material" in outcome.evidence
