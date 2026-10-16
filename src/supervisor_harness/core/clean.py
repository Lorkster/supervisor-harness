"""The run's result, checked the way it would be cloned.

A run's checks run in its own working tree, and a working tree holds more than
the run will commit: build output, caches, files the project ignores. A check
can pass there for a reason that is not in the commit. Minesweeper run 20's
final gate passed `npm run check` in the run's tree and failed it on a clean
checkout: a test asserted that `dist/index.html` exists, `check` runs the tests
before the build, and the tree still had the `dist/` an earlier build had left.

So once nothing more can run, the final gates' checks are run once more in a
scratch worktree of what the run would commit: the tree as `git add -A` sees it
(ignored files left out), committed with a scratch index so neither the run's
branch nor its index moves, its dependencies installed the way CI would
(:func:`.worktree.prepare`).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path

from ..models import DoDCriterion
from .baseline import _git
from .dod import VerificationOutcome, verify_command
from .fails_before import _remove_worktree
from .worktree import prepare

#: Who the scratch commit is by: it is never pushed, merged or kept.
_AUTHOR = {"GIT_AUTHOR_NAME": "supervisor", "GIT_AUTHOR_EMAIL": "supervisor@localhost",
           "GIT_COMMITTER_NAME": "supervisor", "GIT_COMMITTER_EMAIL": "supervisor@localhost"}


def _git_with(tree: Path, env: dict[str, str], *args: str, timeout: float) -> str | None:
    """One git command with extra environment, or ``None`` if git could not answer it."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell, no model input
            ["git", "-C", str(tree), *args],  # noqa: S607 - resolved on PATH
            capture_output=True, text=True, timeout=timeout, check=False,
            env={**os.environ, **env})
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def snapshot(tree: Path, timeout: float = 60) -> str:
    """A commit of ``tree`` as `git add -A` sees it, off its HEAD; empty if git cannot make one."""
    head = _git(tree, "rev-parse", "HEAD")
    if not head:
        return ""
    with tempfile.TemporaryDirectory(prefix="supervisor-clean-index-",
                                     ignore_cleanup_errors=True) as scratch:
        env = {**_AUTHOR, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        if _git_with(tree, env, "add", "-A", timeout=timeout) is None:
            return ""
        written = _git_with(tree, env, "write-tree", timeout=timeout)
        if not written:
            return ""
        return _git_with(tree, env, "commit-tree", written, "-p", head, "-m",
                         "supervisor: the run's tree, as it would be committed",
                         timeout=timeout) or ""


def check_clean(tree: Path, criteria: Iterable[DoDCriterion],
                timeout: float) -> dict[str, VerificationOutcome] | str:
    """Each criterion run again on a clean checkout of ``tree``; or why that could not be done.

    A reason, not a result, when no clean checkout could be made or its
    dependencies installed: a check that cannot run there tells nothing about
    the work.
    """
    commit = snapshot(tree)
    if not commit:
        return "no clean checkout could be made: the run's tree is not a git checkout"
    results: dict[str, VerificationOutcome] = {}
    with tempfile.TemporaryDirectory(prefix="supervisor-clean-",
                                     ignore_cleanup_errors=True) as scratch:
        clean = Path(scratch) / "clean"
        if _git(tree, "worktree", "add", "--detach", str(clean), commit,
                timeout=timeout) is None:
            return "no clean checkout could be made: git refused the worktree"
        try:
            installed = prepare(clean)
            if (clean / "package-lock.json").is_file() and not (clean / "node_modules").exists():
                return f"the clean checkout's dependencies could not be installed: {installed}"
            for crit in criteria:
                results[crit.id] = verify_command(crit, clean, int(timeout))
        finally:
            _remove_worktree(tree, clean)
    return results
