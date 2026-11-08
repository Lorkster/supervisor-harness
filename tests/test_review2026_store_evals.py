"""The eval harness: verdicts, containment and cleanup (2026-11 review, C5)."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness.evals import cli as eval_cli
from supervisor_harness.evals import fixtures, runner, score
from supervisor_harness.evals.cases import Case, CaseError, Fixture
from supervisor_harness.models import RunState
from supervisor_harness.store.runstore import RunStore


def _case(fixture: Fixture) -> Case:
    return Case(id="c1", role="planner", request="r", fixture=fixture)


# -- the verdict -----------------------------------------------------------------


def test_a_run_that_left_no_tree_is_not_accepted() -> None:
    card = score.scorecard(RunState(id="run_S"), ["true"])
    assert card["accepted"] is False
    assert card["acceptance"][0]["passed"] is False


def test_the_score_command_fails_when_nothing_could_be_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    store = RunStore(tmp_path / ".supervisor")
    store.create(RunState(id="run_S", prompt="p"))
    args = argparse.Namespace(workspace=str(tmp_path), run="run_S", accept=["true"],
                              setup=[], tree="", json=True)
    # The store discovered from the workspace is the one under tmp_path.
    assert eval_cli.cmd_eval_score(args) == 1
    capsys.readouterr()


def test_the_score_command_without_acceptance_commands_only_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    store = RunStore(tmp_path / ".supervisor")
    store.create(RunState(id="run_S", prompt="p"))
    args = argparse.Namespace(workspace=str(tmp_path), run="run_S", accept=[],
                              setup=[], tree="", json=False)
    assert eval_cli.cmd_eval_score(args) == 0
    capsys.readouterr()


def test_a_timed_out_acceptance_command_takes_its_children_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shell dies at the timeout; the program it started used to keep the pipes open."""
    monkeypatch.setattr(score, "ACCEPT_TIMEOUT", 1.0)
    child = "import time; time.sleep(60)"
    parent = (f"import subprocess, sys, time; "
              f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)")
    command = f'"{sys.executable}" -c "{parent}"'

    started = time.monotonic()
    result = score._run(command, tmp_path)
    elapsed = time.monotonic() - started

    assert result["passed"] is False and "timed out" in result["tail"]
    assert elapsed < 20, f"returned after {elapsed:.0f}s; the child held the pipe"


def test_an_ordinary_acceptance_command_still_reports_its_exit_and_output(
    tmp_path: Path,
) -> None:
    ok = score._run(f'"{sys.executable}" -c "print(1)"', tmp_path)
    bad = score._run(f'"{sys.executable}" -c "import sys; sys.exit(3)"', tmp_path)
    assert ok["passed"] and ok["tail"] == "1"
    assert bad["passed"] is False and bad["exit"] == 3


# -- fixtures --------------------------------------------------------------------


@pytest.mark.parametrize("key", ["../escape.txt", "a/../../escape.txt", "/abs/escape.txt",
                                 "C:/escape.txt"])
def test_fixture_files_cannot_leave_the_workspace(key: str) -> None:
    case = _case(Fixture(kind="files", files={key: "x"}))
    if key == "C:/escape.txt" and sys.platform != "win32":
        pytest.skip("a drive path is an ordinary relative name on POSIX")
    with pytest.raises(CaseError), fixtures.materialise(case, Path(tempfile.gettempdir())):
        pass


def test_fixture_files_inside_the_workspace_still_work() -> None:
    case = _case(Fixture(kind="files", files={"src/a.txt": "hello", "b.txt": "x"}))
    with fixtures.materialise(case, Path(tempfile.gettempdir())) as workspace:
        assert (workspace / "src" / "a.txt").read_text(encoding="utf-8") == "hello"
        root = workspace.parent
    assert not root.exists(), "the temporary workspace is removed"


def test_clone_separates_the_repository_from_options(monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path: Path) -> None:
    seen: list[tuple[str, ...]] = []

    def fake(*args: str, cwd: Path | None = None) -> str:
        seen.append(args)
        return ""

    monkeypatch.setattr(fixtures, "_git", fake)
    fixtures._repository("--upload-pack=evil", tmp_path / "cache")
    assert seen[0][:3] == ("clone", "--quiet", "--")
    assert seen[0][3] == "--upload-pack=evil"


def test_a_git_timeout_is_a_failed_case_not_a_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="git", timeout=600)

    monkeypatch.setattr(fixtures.subprocess, "run", slow)
    with pytest.raises(CaseError, match="timed out"):
        fixtures._git("status")


def test_a_commit_that_looks_like_an_option_is_refused() -> None:
    case = _case(Fixture(kind="git", repo="/nonexistent", commit="--detach"))
    with pytest.raises(CaseError), fixtures.materialise(case, Path(tempfile.gettempdir())):
        pass


# -- cleanup ---------------------------------------------------------------------


async def test_an_evaluation_removes_its_temporary_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    made: list[Path] = []
    real = tempfile.mkdtemp

    def spy(*args: Any, **kwargs: Any) -> str:
        path = real(*args, **kwargs)
        if kwargs.get("prefix") == "eval-store-":
            made.append(Path(path))
        return path

    monkeypatch.setattr(runner.tempfile, "mkdtemp", spy)
    records = await runner.evaluate([], [], 1, tmp_path / "out.jsonl")
    assert records == []
    assert len(made) == 1 and not made[0].exists()


def test_scoring_a_branch_leaves_no_score_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from supervisor_harness.models import RunWorktree

    state = RunState(id="run_S", workspace=str(tmp_path))
    state.worktree = RunWorktree(branch="nope", commit="", removed=True)
    made: list[Path] = []
    real = tempfile.mkdtemp

    def spy(*args: Any, **kwargs: Any) -> str:
        path = real(*args, **kwargs)
        if kwargs.get("prefix") == "score-":
            made.append(Path(path))
        return path

    monkeypatch.setattr(score.tempfile, "mkdtemp", spy)
    with score._tree(state, None) as tree:
        assert tree is None            # `tmp_path` is not a repository
    assert len(made) == 1 and not made[0].exists()
