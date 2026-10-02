"""Findings from a security review of the harness, each pinned by a test.

Every fix here was confirmed against the code before it changed: a run id that
deleted a directory outside the store, a command timeout that did not stop a
`.cmd` shim's child, credentials handed to the project's own test scripts, a
criterion command that could carry any program as `python -c`, a search a
model could make run for hours, and a workspace under a `build` directory that
showed no files at all.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from supervisor_harness.config import Policy
from supervisor_harness.core import tools as tools_module
from supervisor_harness.core.dod import child_environment, run_bounded, unsafe_command
from supervisor_harness.core.tools import Toolbox
from supervisor_harness.store.runstore import RunStore, checked_run_id

# -- run ids -----------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../../victim", "..", ".", "", "a/b", "a\\b", "C:/x",
                                 "/etc", "run..x", " run"])
def test_a_run_id_is_one_name_inside_the_store(bad: str) -> None:
    with pytest.raises(ValueError):
        checked_run_id(bad)


def test_deleting_a_run_never_leaves_the_store(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "important.txt").write_text("keep", encoding="utf-8")
    store = RunStore(tmp_path / "store")

    with pytest.raises(ValueError):
        store.delete_run("../../victim")
    with pytest.raises(ValueError):
        store.delete_run(".")
    assert (victim / "important.txt").read_text(encoding="utf-8") == "keep"
    assert store.runs_dir.is_dir(), "'.' would have been the whole runs directory"


def test_reading_a_result_cannot_create_directories_elsewhere(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "store")
    with pytest.raises(ValueError):
        store.read_result("../../escaped", "x.json")
    assert not (tmp_path / "escaped").exists()
    assert store.exists("../../escaped") is False
    assert checked_run_id("run_01M3XXJDATSRBB") == "run_01M3XXJDATSRBB"


# -- commands the harness runs ---------------------------------------------------


def test_a_criterion_command_may_not_carry_its_own_program() -> None:
    assert unsafe_command('python -c "import os; os.remove(\'x\')"') is not None
    assert unsafe_command("node -e 1") is not None
    assert unsafe_command("python -m pytest -q") is None


GRANDCHILD = """
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
time.sleep(20)
"""


def test_a_timeout_stops_the_whole_process_tree(tmp_path: Path) -> None:
    """The child's child inherits the pipes; killing only the child left the call
    blocked until the grandchild finished (a `.cmd` shim is this shape on Windows)."""
    script = tmp_path / "tree.py"
    script.write_text(GRANDCHILD, encoding="utf-8")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded([sys.executable, str(script)], tmp_path, timeout=1)
    assert time.monotonic() - started < 10, "the timeout was not a bound"


def test_commands_run_without_the_users_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "AWS_SECRET_ACCESS_KEY",
                 "AWS_SESSION_TOKEN", "GITHUB_TOKEN", "MY_SERVICE_SECRET"):
        monkeypatch.setenv(name, "s3cret-value")
    monkeypatch.setenv("KEEP_ME", "1")
    env = child_environment()
    assert "KEEP_ME" in env and "PATH" in {k.upper() for k in env}
    assert not any(v == "s3cret-value" for v in env.values())

    done = run_bounded([sys.executable, "-c",
                        "import os; print(os.environ.get('ANTHROPIC_API_KEY', 'absent'))"],
                       tmp_path, timeout=30)
    assert done.stdout.strip() == "absent"


# -- the search tool ---------------------------------------------------------------


def _box(tree: Path) -> Toolbox:
    return Toolbox(tree, Policy())


@pytest.mark.parametrize("pattern", ["(a+)+$", "(a*)*b", r"(\w+\s?)*$", "(x{1,9})+"])
def test_a_search_pattern_with_nested_repetition_is_refused(tmp_path: Path,
                                                           pattern: str) -> None:
    (tmp_path / "a.txt").write_text("a" * 40 + "!", encoding="utf-8")
    result = _box(tmp_path).search(pattern)
    assert not result.ok and "exponential" in result.output


def test_ordinary_patterns_still_search(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def login(user):\n    return user\n", encoding="utf-8")
    result = _box(tmp_path).search(r"def \w+\(")
    assert result.ok and "a.py:1" in result.output
    assert not _box(tmp_path).search("x" * 400).ok, "an overlong pattern is refused"


def test_a_search_stops_at_its_time_budget_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(3):
        (tmp_path / f"f{n}.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(tools_module, "SEARCH_SECONDS", -1.0)
    result = _box(tmp_path).search("needle")
    assert "search stopped after" in result.output


def test_files_too_large_to_read_whole_are_refused_or_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools_module, "MAX_FILE_BYTES", 100)
    (tmp_path / "big.log").write_text("needle\n" * 100, encoding="utf-8")
    (tmp_path / "small.txt").write_text("needle\n", encoding="utf-8")
    box = _box(tmp_path)
    assert not box.read_file("big.log").ok
    found = box.search("needle").output
    assert "small.txt" in found and "big.log" not in found


def test_a_workspace_under_a_build_directory_still_shows_its_files(tmp_path: Path) -> None:
    workspace = tmp_path / "build" / "project"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "dep.js").write_text("", encoding="utf-8")
    listed = _box(workspace).list_files().output
    assert "src/app.py" in listed and "dep.js" not in listed


@pytest.mark.skipif(os.name != "nt", reason="the .cmd shim case is Windows-only")
def test_a_cmd_shim_is_stopped_at_its_timeout(tmp_path: Path) -> None:
    shim = tmp_path / "slow.cmd"
    shim.write_text("@echo off\r\nping -n 15 127.0.0.1 > nul\r\n", encoding="utf-8")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded([str(shim)], tmp_path, timeout=1)
    assert time.monotonic() - started < 8
