"""The final gates' checks, run again on a clean checkout of what the run commits.

Minesweeper run 20's final gate passed `npm run check` in the run's tree and
failed it on a clean checkout: a test asserted `dist/index.html` exists, `check`
runs the tests before the build, and the tree still had an earlier build's
`dist/`. Every test here builds a real repository.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from supervisor_harness.core.clean import check_clean, snapshot
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    CriterionStatus,
    DoDCriterion,
    ExecutionTask,
    RunState,
    TaskStatus,
    VerifyMethod,
)

from .test_fails_before import git, repository, write

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

PYTHON = Path(sys.executable).name
#: Passes only where the build output is: a check that leans on what the tree has.
NEEDS_BUILD = ("import pathlib, sys\n"
               "sys.exit(0 if pathlib.Path('dist/index.html').exists() else 1)\n")
FINE = "print('ok')\n"


def _project(root: Path) -> None:
    repository(root, {".gitignore": "dist/\n", "README.md": "a game\n"})
    write(root, {"needs_build.py": NEEDS_BUILD, "fine.py": FINE,
                 "dist/index.html": "<!doctype html>\n"})    # left by an earlier build


def _check(name: str, script: str) -> DoDCriterion:
    return DoDCriterion(statement=name, method=VerifyMethod.COMMAND,
                        command=f"{PYTHON} {script}", status=CriterionStatus.PASS,
                        verified_by="harness")


def test_a_check_that_leans_on_ignored_output_fails_on_a_clean_checkout(tmp_path: Path) -> None:
    root = tmp_path / "game"
    _project(root)
    leaning, fine = _check("the build is there", "needs_build.py"), _check("fine", "fine.py")
    head, status = git(root, "rev-parse", "HEAD"), git(root, "status", "--porcelain")

    results = check_clean(root, [leaning, fine], timeout=120)

    assert not isinstance(results, str), results
    assert results[leaning.id].status is CriterionStatus.FAIL
    assert results[fine.id].status is CriterionStatus.PASS, "uncommitted files are in it"
    assert git(root, "rev-parse", "HEAD") == head, "the branch did not move"
    assert git(root, "status", "--porcelain") == status, "nor did the index"
    assert len(git(root, "worktree", "list").splitlines()) == 1, "the scratch worktree is gone"


def test_without_git_it_says_why_and_judges_nothing(tmp_path: Path) -> None:
    (tmp_path / "fine.py").write_text(FINE, encoding="utf-8")
    assert snapshot(tmp_path) == ""
    reason = check_clean(tmp_path, [_check("fine", "fine.py")], timeout=60)
    assert isinstance(reason, str) and "no clean checkout" in reason


def _gated(supervisor: Supervisor, run_id: str) -> tuple[object, ExecutionTask, ExecutionTask]:
    _project(supervisor.workspace)
    supervisor.config.policy.allow_command_execution = True
    gate = ExecutionTask(title="Final gate", status=TaskStatus.VERIFIED, attempts=1,
                         dod=[_check("the build is there", "needs_build.py"),
                              _check("fine", "fine.py")])
    page = ExecutionTask(title="Page", status=TaskStatus.VERIFIED, attempts=1,
                         dod=[_check("the build is there", "needs_build.py")])
    gate.depends_on = [page.id]
    session = supervisor.store.create(RunState(id=run_id, prompt="p",
                                               tasks={t.id: t for t in (page, gate)}))
    return session, gate, page


async def test_the_final_gate_is_sent_back_for_it(supervisor: Supervisor) -> None:
    session, gate, page = _gated(supervisor, "run_Clean")

    assert await supervisor._check_clean(session) == 1  # type: ignore[arg-type]

    leaning, fine = gate.dod
    assert gate.status is TaskStatus.FAILED and leaning.status is CriterionStatus.FAIL
    assert leaning.evidence.startswith("passes in the run's tree, but fails on a clean "
                                       "checkout of what the run would commit")
    assert fine.status is CriterionStatus.PASS
    assert page.status is TaskStatus.VERIFIED, "a task another waits on is not a final gate"
    assert any("1 of 2 check(s) failed there" in n.text
               for n in session.state.notes)  # type: ignore[attr-defined]
    assert await supervisor._follow_up(session) == 1, "and goes back"  # type: ignore[arg-type]


async def test_once_per_attempt_and_not_when_turned_off(supervisor: Supervisor) -> None:
    session, gate, _ = _gated(supervisor, "run_Once")
    gate.dod = [_check("fine", "fine.py")]
    assert await supervisor._check_clean(session) == 0  # type: ignore[arg-type]
    assert sum("checked on a clean checkout" in n.text
               for n in session.state.notes) == 1  # type: ignore[attr-defined]
    assert await supervisor._check_clean(session) == 0  # type: ignore[arg-type]
    assert sum("checked on a clean checkout" in n.text
               for n in session.state.notes) == 1, "not again for the same attempt"  # type: ignore[attr-defined]

    session, gate, _ = _gated(supervisor, "run_Off")
    supervisor.config.policy.clean_check = False
    assert await supervisor._check_clean(session) == 0  # type: ignore[arg-type]
    assert gate.status is TaskStatus.VERIFIED


async def test_verification_checks_it_before_the_run_is_judged(supervisor: Supervisor) -> None:
    from supervisor_harness.models import Phase

    session, gate, _ = _gated(supervisor, "run_Wired")
    session.state.phase = Phase.VERIFYING  # type: ignore[attr-defined]

    assert await supervisor._continue_verification(session) is None  # type: ignore[arg-type]

    assert gate.status is TaskStatus.APPROVED, "sent back, not left for the checkpoint"
    assert session.state.phase is Phase.EXECUTING  # type: ignore[attr-defined]
