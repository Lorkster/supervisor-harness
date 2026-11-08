"""What git does in a repository the harness did not write, and what a run's state may delete.

Findings 2 and 5 of the 2026-11 review. A repository can plant `core.fsmonitor` in
`.git/config`, hooks in `.git/hooks`, a `git.exe` in the directory the harness runs
from, and a `state.json` under `.supervisor/runs/` naming any directory to remove.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from supervisor_harness.core import baseline, worktree
from supervisor_harness.core.audit import scan_unclaimed_changes
from supervisor_harness.core.baseline import git_argv, git_baseline, git_environment
from supervisor_harness.core.processes import alive, stop_processes_in
from supervisor_harness.models import BASELINE_FACT, RunState

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def make_repository(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "user.name", "Owner")
    git(root, "config", "user.email", "owner@localhost")
    git(root, "config", "commit.gpgsign", "false")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "baseline")
    return git(root, "rev-parse", "HEAD")


def plant_script(path: Path, marker: Path) -> None:
    """A script git would run (as a hook or as an fsmonitor) that leaves a marker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    marker_path = marker.as_posix()
    path.write_text(f"#!/bin/sh\necho ran >> '{marker_path}'\nexit 0\n", encoding="utf-8",
                    newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


# -- 5. git on a repository that is not the owner's -------------------------------------------


@needs_git
def test_a_planted_fsmonitor_is_not_run_by_the_baseline_lookup(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repository(repo)
    marker = tmp_path / "fsmonitor-ran"
    script = repo / ".git" / "fsmonitor-hook"
    plant_script(script, marker)
    git(repo, "config", "core.fsmonitor", script.as_posix())
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    assert "already modified" in git_baseline(repo)  # git did answer
    assert not marker.exists(), "git ran the repository's fsmonitor"


@needs_git
def test_planted_hooks_are_not_run_when_a_worktree_is_made_and_committed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = make_repository(repo)
    marker = tmp_path / "hook-ran"
    for hook in ("post-checkout", "pre-commit", "commit-msg", "post-commit",
                 "reference-transaction"):
        plant_script(repo / ".git" / "hooks" / hook, marker)
    tree = tmp_path / "run" / "worktree"
    assert worktree.create(repo, tree, "supervisor/run_x", base) is None
    (tree / "b.txt").write_text("new\n", encoding="utf-8")
    closed = worktree.close(repo, tree, base, "run_x")
    assert closed.commit, closed.note
    assert not marker.exists(), "git ran one of the repository's hooks"


@needs_git
def test_a_commit_that_a_hook_would_refuse_is_made_anyway(tmp_path: Path) -> None:
    """--no-verify, as well as an empty hooks path: a refusing hook cannot hold a run's work."""
    repo = tmp_path / "repo"
    base = make_repository(repo)
    refuse = repo / ".git" / "hooks" / "pre-commit"
    refuse.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    refuse.chmod(refuse.stat().st_mode | stat.S_IEXEC)
    tree = tmp_path / "run" / "worktree"
    assert worktree.create(repo, tree, "supervisor/run_y", base) is None
    (tree / "b.txt").write_text("new\n", encoding="utf-8")
    assert worktree.close(repo, tree, base, "run_y").commit


@needs_git
def test_the_audits_diff_does_not_run_a_planted_fsmonitor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    head = make_repository(repo)
    marker = tmp_path / "audit-fsmonitor-ran"
    script = repo / ".git" / "fsmonitor-hook"
    plant_script(script, marker)
    git(repo, "config", "core.fsmonitor", script.as_posix())
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    state = RunState(id="run_x", workspace=str(repo),
                     facts={BASELINE_FACT: f"`{head[:12]}` on `main`, working tree clean"})
    observations, skipped = scan_unclaimed_changes(state, repo)
    assert not skipped and observations, (observations, skipped)
    assert not marker.exists(), "the audit's git ran the repository's fsmonitor"


@needs_git
def test_every_harness_git_command_carries_the_hardening(tmp_path: Path) -> None:
    argv = git_argv("status", workspace=tmp_path)
    assert argv is not None
    assert Path(argv[0]).is_absolute() and Path(argv[0]).is_file()
    assert "--no-optional-locks" in argv
    config = [argv[i + 1] for i, word in enumerate(argv) if word == "-c"]
    assert "core.fsmonitor=false" in config
    hooks = next(c for c in config if c.startswith("core.hooksPath="))
    empty = Path(hooks.partition("=")[2])
    assert empty.is_dir() and not any(empty.iterdir())
    assert argv[-1] == "status" and ["-C", str(tmp_path)] == argv[argv.index("-C"):][:2]


@needs_git
def test_git_is_not_taken_from_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = shutil.which("git")
    assert real is not None
    planted = tmp_path / "cwd"
    planted.mkdir()
    for name in ("git.exe", "git.bat", "git.cmd", "git"):
        (planted / name).write_text("echo planted\n", encoding="utf-8")
        (planted / name).chmod(0o755)
    monkeypatch.chdir(planted)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", str(planted), str(Path(real).parent)]))
    baseline._git_program.cache_clear()
    try:
        argv = git_argv("status")
        assert argv is not None
        assert Path(argv[0]).resolve().parent != planted.resolve()
        assert Path(argv[0]).name.lower() in ("git", "git.exe")
    finally:
        baseline._git_program.cache_clear()


def test_no_git_means_no_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    baseline._git_program.cache_clear()
    try:
        assert git_argv("status") is None
        assert baseline._git(Path.cwd(), "status") is None
    finally:
        baseline._git_program.cache_clear()


def test_git_runs_in_the_scrubbed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("GITHUB_PAT", "ghp_secret")
    monkeypatch.setenv("SUPERVISOR_HOME", "/elsewhere")
    env = {name.upper() for name in git_environment()}
    assert not {"ANTHROPIC_API_KEY", "GITHUB_PAT", "SUPERVISOR_HOME"} & env
    assert "PATH" in env
    assert git_environment()["GIT_TERMINAL_PROMPT"] == "0"


# -- 2. a run's state.json is not to be believed about what to delete -------------------------


def _released_run(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    """A repository, the store's runs directory, and a real worktree where the store puts it."""
    repo = tmp_path / "repo"
    base = make_repository(repo)
    runs = tmp_path / "store" / "runs"
    tree = runs / "run_abc" / "worktree"
    assert worktree.create(repo, tree, worktree.branch_for("run_abc"), base) is None
    return repo, runs, tree, base


@needs_git
def test_a_run_releases_the_worktree_and_branch_the_store_made(tmp_path: Path) -> None:
    repo, runs, tree, _ = _released_run(tmp_path)
    note = worktree.release(repo, str(tree), "supervisor/run_abc", unmerged=True,
                            run_id="run_abc", runs_dir=runs)
    assert not tree.exists()
    assert "deleted branch supervisor/run_abc" in note
    assert "supervisor/run_abc" not in git(repo, "branch", "--list", "supervisor/*")


@needs_git
@pytest.mark.parametrize("where", ["outside", "sibling_run", "parent_of_store", "dotdot"])
def test_a_planted_state_cannot_name_another_directory_for_removal(
    tmp_path: Path, where: str
) -> None:
    repo, runs, tree, _ = _released_run(tmp_path)
    victim = {"outside": tmp_path / "precious", "sibling_run": runs / "run_other" / "worktree",
              "parent_of_store": runs.parent,
              "dotdot": tree / ".." / ".." / ".." / ".." / "precious"}[where]
    precious = tmp_path / "precious"
    precious.mkdir(exist_ok=True)
    (precious / "keep.txt").write_text("irreplaceable", encoding="utf-8")
    victim.mkdir(parents=True, exist_ok=True)
    note = worktree.release(repo, str(victim), "supervisor/run_abc", unmerged=True,
                            run_id="run_abc", runs_dir=runs)
    assert (precious / "keep.txt").read_text(encoding="utf-8") == "irreplaceable"
    assert victim.exists() and "refused" in note
    assert tree.exists(), "the real worktree is untouched too: the state was not believed"


@needs_git
def test_a_planted_state_cannot_name_another_branch_for_deletion(tmp_path: Path) -> None:
    repo, runs, tree, _ = _released_run(tmp_path)
    git(repo, "branch", "supervisor/someone-elses-run")
    note = worktree.release(repo, str(tree), "supervisor/someone-elses-run", unmerged=True,
                            run_id="run_abc", runs_dir=runs)
    assert "refused" in note
    assert "supervisor/someone-elses-run" in git(repo, "branch", "--list", "supervisor/*")
    assert tree.exists()


@needs_git
def test_a_branch_the_harness_did_not_make_is_left_alone_without_a_fuss(tmp_path: Path) -> None:
    repo, runs, tree, _ = _released_run(tmp_path)
    git(repo, "branch", "feature")
    note = worktree.release(repo, str(tree), "feature", unmerged=True,
                            run_id="run_abc", runs_dir=runs)
    assert "refused" not in note and not tree.exists()
    assert "feature" in git(repo, "branch", "--list", "feature")


@needs_git
def test_release_refuses_without_the_store_and_the_run(tmp_path: Path) -> None:
    repo, runs, tree, _ = _released_run(tmp_path)
    assert "refused" in worktree.release(repo, str(tree), "supervisor/run_abc", unmerged=True)
    assert "refused" in worktree.release(repo, str(tree), "supervisor/run_abc", unmerged=True,
                                         run_id="run_abc")
    assert "refused" in worktree.release(repo, str(tree), "supervisor/run_abc", unmerged=True,
                                         runs_dir=runs)
    assert tree.exists()
    assert "supervisor/run_abc" in git(repo, "branch", "--list", "supervisor/*")


@needs_git
@pytest.mark.parametrize("run_id", ["", "..", "a/b", "a\\b", "run_abc/.."])
def test_release_refuses_a_run_id_that_is_not_one_name(tmp_path: Path, run_id: str) -> None:
    repo, runs, tree, _ = _released_run(tmp_path)
    note = worktree.release(repo, str(tree), worktree.branch_for(run_id), unmerged=True,
                            run_id=run_id, runs_dir=runs)
    assert "refused" in note and tree.exists()


needs_a_process_table = pytest.mark.skipif(
    sys.platform not in ("win32", "linux"),
    reason="reading another process's working directory is implemented for Windows and Linux",
)


def test_stopping_processes_refuses_a_root_or_the_home_directory() -> None:
    assert stop_processes_in(Path(Path.cwd().anchor)) == []
    assert stop_processes_in(Path.home()) == []
    assert stop_processes_in(Path.home().parent) == []


@needs_a_process_table
def test_stopping_processes_stays_inside_the_place_it_was_told_to(tmp_path: Path) -> None:
    held = tmp_path / "held"
    held.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], cwd=held)
    try:
        time.sleep(1.0)
        assert stop_processes_in(held, within=other) == []
        assert alive(proc.pid), "a process outside `within` was stopped"
        stopped = stop_processes_in(held, within=tmp_path)
        assert any(f"pid {proc.pid}" in line for line in stopped), stopped
    finally:
        proc.kill()
        proc.wait()
