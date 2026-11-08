"""`uninstall --purge` leaves git no dangling worktree (review finding 14)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from supervisor_harness.cli import main

from .test_fails_before import git, repository

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def test_purge_prunes_the_worktrees_the_store_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SUPERVISOR_HOME", raising=False)
    root = tmp_path / "project"
    repository(root, {"README.md": "x\n"})
    tree = root / ".supervisor" / "worktrees" / "run_1"
    git(root, "worktree", "add", "-b", "supervisor/run_1", str(tree))
    assert len(git(root, "worktree", "list").splitlines()) == 2

    assert main(["uninstall", "--purge", "-w", str(root)]) == 0

    assert not (root / ".supervisor").exists()
    assert len(git(root, "worktree", "list").splitlines()) == 1
