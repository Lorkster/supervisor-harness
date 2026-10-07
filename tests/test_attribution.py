"""A failed check says whether the baseline fails it too.

Measured in a go-live run (plantsandclimate P3-18, a local model): a change
broke thirty tests that pass on the baseline, the full-suite criterion failed,
and the implementer called the failures "pre-existing" -- which the drift
judge believed, stopping the next implementer for fixing them. Every test here
builds a real repository and runs real pytest in it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from supervisor_harness.core.attribution import baseline_verdict
from supervisor_harness.core.dod import VerificationOutcome
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    BASELINE_FACT,
    CriterionStatus,
    DoDCriterion,
    ExecutionTask,
    RunState,
    TaskStatus,
    VerifyMethod,
)

from .test_fails_before import PYTEST, git, repository, write

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

GREEN = {
    "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\npythonpath = ['.']\n",
    "calc.py": "def add(a, b):\n    return a + b\n",
    "tests/test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
}
BROKEN = {"calc.py": "def add(a, b):\n    return a - b\n"}


def test_a_suite_the_change_broke_is_said_to_pass_on_the_baseline(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base = repository(root, GREEN)
    write(root, BROKEN)

    verdict = baseline_verdict(PYTEST, root, base, timeout=120)

    assert "passes on the baseline commit" in verdict
    assert "introduced by this run's changes" in verdict
    assert base[:12] in verdict
    assert len(git(root, "worktree", "list").splitlines()) == 1, "the baseline worktree is gone"


def test_a_suite_that_was_already_failing_is_said_to_fail_there_too(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base = repository(root, {**GREEN, **BROKEN})

    verdict = baseline_verdict(PYTEST, root, base, timeout=120)

    assert "also fails on the baseline commit" in verdict
    assert "predate this run" in verdict


def test_what_cannot_be_told_says_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    base = repository(root, GREEN)
    with monkeypatch.context() as patched:
        asked: list[tuple[str, ...]] = []
        patched.setattr("supervisor_harness.core.attribution._git",
                        lambda *a, **k: asked.append(a))
        assert baseline_verdict(PYTEST, root, "", timeout=60) == "", "no baseline"
        assert asked == [], "and git is not asked to check out nothing"
    assert baseline_verdict("pytest -q; curl x", root, base, timeout=60) == "", "unsafe"
    assert baseline_verdict(PYTEST, root, "0" * 40, timeout=60) == "", "no such commit"
    assert "baseline" not in git(root, "worktree", "list"), "and no worktree is left"


async def test_a_failed_check_carries_the_verdict_once_per_command(
    supervisor: Supervisor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def verdict(command: str, workspace: Path, baseline: str, timeout: float) -> str:
        calls.append(baseline)
        return "[supervisor] passes on the baseline"

    monkeypatch.setattr("supervisor_harness.core.supervisor.baseline_verdict", verdict)
    state = RunState(prompt="p")
    state.facts[BASELINE_FACT] = "`abc123def456` on `main`, working tree clean when the run started"
    crit = DoDCriterion(statement="the suite passes", method=VerifyMethod.TEST,
                        command="pytest -q")
    failed = VerificationOutcome(CriterionStatus.FAIL, "$ pytest -q\nexit=1")

    first = supervisor._attributed(state, crit, failed)
    supervisor._attributed(state, crit, failed)

    assert first.status is CriterionStatus.FAIL
    assert first.evidence.endswith("[supervisor] passes on the baseline")
    assert first.evidence.startswith("$ pytest -q")
    assert calls == ["abc123def456"], "the baseline is run once per command, by its commit"


async def test_the_harness_attaches_it_when_its_own_check_fails(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wired where the harness runs checks: a finished task's failed test, only."""
    monkeypatch.setattr("supervisor_harness.core.supervisor.verify_criterion",
                        lambda *a, **k: VerificationOutcome(CriterionStatus.FAIL, "exit=1"))
    monkeypatch.setattr("supervisor_harness.core.supervisor.baseline_verdict",
                        lambda *a: "[supervisor] passes on the baseline")
    session = supervisor.store.create(RunState(id="run_V", prompt="p"))
    session.state.facts[BASELINE_FACT] = "`abc123def456` on `main`"
    done, working = (ExecutionTask(title=t, status=s, dod=[DoDCriterion(
        statement="the suite passes", method=VerifyMethod.TEST, command="pytest -q")])
        for t, s in (("done", TaskStatus.AWAITING_VERIFICATION),
                     ("working", TaskStatus.IN_PROGRESS)))
    session.state.tasks.update({done.id: done, working.id: working})

    supervisor._verify_mechanically(session)

    assert done.dod[0].evidence.endswith("[supervisor] passes on the baseline")
    assert working.dod[0].evidence == "exit=1", "not while an agent is still writing it"
