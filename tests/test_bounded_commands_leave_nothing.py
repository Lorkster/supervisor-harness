"""A bounded command leaves nothing running: not at its timeout, not when it returns.

Measured in Minesweeper run 5: an implementer's probe script started
``npx vite preview`` through ``child_process``, and the harness's
``run_command`` ran the probe. Two ``node`` processes -- ``npx-cli.js`` and the
``vite.js`` it started -- were still serving ports 4173 and 4199 forty-five
minutes after the run ended. They held files under the worktree's
``node_modules``, so closing the worktree removed it only partly, and the
scorer scored the husk.

The kill missed them because they were no longer in the tree it walked.
``taskkill /T`` follows parent ids from the process the harness started; the
probe's own timeout killed the ``cmd.exe`` between it and ``npx``, so ``npx``
and ``vite`` had a dead parent and nothing led to them. And a command that
returned normally was never followed by any kill at all: whatever it left
behind was simply left. The shape here is that one, without node: a script that
starts a never-ending child through a shim, loses the shim, and then either
hangs or returns.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from supervisor_harness.core import worktree
from supervisor_harness.core.dod import run_bounded
from supervisor_harness.core.processes import alive, stop_processes_in

#: Writes its pid where it is told to, then runs for longer than any test.
LEAF = """
import os, sys, time
with open(sys.argv[1] + ".tmp", "w") as f:
    f.write(str(os.getpid()))
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
time.sleep(120)
"""

#: Starts the leaf through ``middle`` with its own stdio -- so it holds none of
#: the harness's pipes -- waits until it runs, kills ``middle`` (what Node's
#: ``execSync`` timeout and ``child.kill()`` do to the shell they started) when
#: told to, then hangs or returns.
ROOT = """
import os, subprocess, sys, time
middle, pidfile, kill_middle, then = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
child = subprocess.Popen([middle, pidfile], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
deadline = time.monotonic() + 30
while not os.path.exists(pidfile) and time.monotonic() < deadline:
    time.sleep(0.05)
if kill_middle == "kill":
    child.kill()
    child.wait()
if then == "hang":
    time.sleep(120)
"""


def _shim(tmp_path: Path) -> Path:
    """What ``npx`` is on Windows: a script the system runs through a shell."""
    leaf = tmp_path / "leaf.py"
    leaf.write_text(LEAF, encoding="utf-8")
    if os.name == "nt":
        shim = tmp_path / "serve.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{leaf}" %*\r\n', encoding="utf-8")
    else:
        shim = tmp_path / "serve"
        shim.write_text(f'#!/bin/sh\n"{sys.executable}" "{leaf}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
    return shim


def _root(tmp_path: Path, kill_middle: bool, then: str) -> tuple[list[str], Path]:
    root = tmp_path / "root.py"
    root.write_text(ROOT, encoding="utf-8")
    pidfile = tmp_path / "leaf.pid"
    argv = [sys.executable, str(root), str(_shim(tmp_path)), str(pidfile),
            "kill" if kill_middle else "keep", then]
    return argv, pidfile


def _leaf(pidfile: Path) -> int:
    assert pidfile.is_file(), "the leaf never started; the test proves nothing"
    return int(pidfile.read_text(encoding="utf-8"))


def _gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


@pytest.fixture
def reap() -> list[int]:
    """Pids a failing test would otherwise leave running on the machine."""
    pids: list[int] = []
    yield pids
    for pid in pids:
        if alive(pid):
            _kill(pid)


def _kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, check=False)
    else:
        try:  # noqa: SIM105
            os.kill(pid, 9)
        except OSError:
            pass


def test_an_orphan_is_stopped_at_the_timeout(tmp_path: Path, reap: list[int]) -> None:
    """Run 5's shape: the shim between the command and the server is already dead,
    so the server's parent id leads nowhere when the timeout comes."""
    argv, pidfile = _root(tmp_path, kill_middle=True, then="hang")
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded(argv, tmp_path, timeout=8)
    leaf = _leaf(pidfile)
    reap.append(leaf)
    assert _gone(leaf), "the orphaned server outlived the command's timeout"


def test_a_command_that_returns_leaves_nothing_behind(tmp_path: Path, reap: list[int]) -> None:
    """A command that exits on its own is followed by the same kill: what it
    started and did not stop is stopped."""
    argv, pidfile = _root(tmp_path, kill_middle=False, then="return")
    done = run_bounded(argv, tmp_path, timeout=30)
    assert done.returncode == 0, done.stderr
    leaf = _leaf(pidfile)
    reap.append(leaf)
    assert _gone(leaf), "the server the command started outlived it"


def test_an_orphan_of_a_returning_command_is_stopped_too(
    tmp_path: Path, reap: list[int]
) -> None:
    argv, pidfile = _root(tmp_path, kill_middle=True, then="return")
    run_bounded(argv, tmp_path, timeout=30)
    leaf = _leaf(pidfile)
    reap.append(leaf)
    assert _gone(leaf)


def test_a_bounded_command_still_reports_its_output_and_exit(tmp_path: Path) -> None:
    done = run_bounded([sys.executable, "-c", "import sys; print('out'); "
                        "print('err', file=sys.stderr); sys.exit(3)"], tmp_path, timeout=30)
    assert (done.returncode, done.stdout.strip(), done.stderr.strip()) == (3, "out", "err")


# -- the worktree ---------------------------------------------------------------


def _detached(argv: list[str], cwd: Path) -> subprocess.Popen[bytes]:
    """A process that left every group the harness could have tracked."""
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        return subprocess.Popen(argv, cwd=cwd, creationflags=flags,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    return subprocess.Popen(argv, cwd=cwd, start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _sleeper(tmp_path: Path, cwd: Path, name: str) -> tuple[subprocess.Popen[bytes], int]:
    leaf = tmp_path / "leaf.py"
    leaf.write_text(LEAF, encoding="utf-8")
    pidfile = tmp_path / f"{name}.pid"
    proc = _detached([sys.executable, str(leaf), str(pidfile)], cwd)
    deadline = time.monotonic() + 30
    while not pidfile.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    return proc, _leaf(pidfile)


needs_a_process_table = pytest.mark.skipif(
    sys.platform not in ("win32", "linux"),
    reason="reading another process's working directory is implemented for Windows and Linux",
)


@needs_a_process_table
def test_a_process_working_in_a_tree_is_stopped_and_named(
    tmp_path: Path, reap: list[int]
) -> None:
    tree = tmp_path / "worktree"
    (tree / "node_modules").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    inside, inside_pid = _sleeper(tmp_path, tree / "node_modules", "inside")
    outside, outside_pid = _sleeper(tmp_path, elsewhere, "outside")
    reap.extend([inside_pid, outside_pid])
    try:
        stopped = stop_processes_in(tree)
        assert _gone(inside_pid), "a process working inside the tree was left running"
        assert alive(outside_pid), "a process working elsewhere was stopped"
        assert any(f"pid {inside_pid}" in line for line in stopped), stopped
    finally:
        for proc in (inside, outside):
            proc.kill()
            proc.wait()


@needs_a_process_table
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_closing_a_worktree_stops_what_holds_it_and_removes_it(
    tmp_path: Path, reap: list[int]
) -> None:
    """On Windows a running process's working directory cannot be deleted: run 5's
    worktree came down only partly, and the scorer scored what was left."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.name", "Owner"],
                 ["config", "user.email", "owner@localhost"],
                 ["config", "commit.gpgsign", "false"],
                 ["commit", "-q", "--allow-empty", "-m", "baseline"]):
        subprocess.run(["git", *args], cwd=repo, check=True,
                       capture_output=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()
    tree = tmp_path / "run" / "worktree"
    assert worktree.create(repo, tree, "supervisor/run_x", base) is None
    (tree / "node_modules").mkdir()
    holder, pid = _sleeper(tmp_path, tree / "node_modules", "holder")
    reap.append(pid)
    try:
        closed = worktree.close(repo, tree, base, "run_x")
        assert closed.removed and not tree.exists(), closed.note
        assert f"pid {pid}" in closed.note, closed.note
    finally:
        holder.kill()
        holder.wait()


@needs_a_process_table
def test_the_harness_never_stops_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Working inside the tree being cleared does not make the harness a target."""
    monkeypatch.chdir(tmp_path)
    assert all(f"pid {os.getpid()} " not in f"{line} " for line in stop_processes_in(tmp_path))
