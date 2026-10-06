"""A run's own branch: execution happens in a worktree, never in the owner's tree.

Before this, an execute-mode run wrote straight into the tree its owner was
working in. Whatever the agents did -- a half-finished change, a task that
failed verification, an edit the owner would have rejected -- was in their
working copy, mixed with their own uncommitted work. "Reversible until merged"
was true only if the owner happened to start on a clean tree and knew to look.

So an execute-mode run on the autonomous backend now works in a git worktree on
a branch named for the run, ``supervisor/<run id>``, started from the commit the
run measured as its baseline. Execution, verification and the harness's own
checks all happen there. At the end the run's changes are committed on that
branch and the worktree is removed, leaving the branch: something to diff,
review, merge or delete. **Nothing here pushes.** Sending a branch anywhere is
an outward-facing act, and it is the owner's.

What this does not do:

* **It is one worktree per run, not per agent.** Agents in a run still share a
  tree, as `core/baseline.py` explains; separating them is a different decision,
  and still declined.
* **The owner's uncommitted changes are not carried over.** The branch starts at
  the baseline commit. That protects work in progress from the agents, at the
  cost that a task depending on uncommitted code will not find it -- which the
  baseline fact already tells every agent, and the run's notes repeat.
* **A host-delegated run is not isolated yet.** The harness cannot fence a host's
  own tools, so pointing them at a worktree is a promise it could not keep.

Every command here has a fixed argv built by the harness; nothing comes from a
model.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .baseline import _git

#: Branches the harness creates are all under here, so they are easy to find,
#: list and delete, and never collide with the owner's own naming.
BRANCH_PREFIX = "supervisor/"

#: How long a checkout or commit may take. A large repository on Windows can
#: take a while to check out; a lookup's 15 seconds is not enough.
TIMEOUT = 300


def branch_for(run_id: str) -> str:
    return f"{BRANCH_PREFIX}{run_id}"


def create(workspace: Path, tree: Path, branch: str, base: str) -> str | None:
    """Add a worktree at ``tree`` on a new ``branch`` from ``base``; the error, or ``None``."""
    if tree.exists():
        return f"{tree} already exists"
    tree.parent.mkdir(parents=True, exist_ok=True)
    added = _git(workspace, "worktree", "add", "-b", branch, str(tree), base, timeout=TIMEOUT)
    if added is None:
        return (f"git could not add a worktree on a new branch {branch!r} from {base}; "
                "the branch may already exist, or the commit may not")
    return None


@dataclass
class Closed:
    """What closing a worktree left behind."""

    commit: str = ""
    diffstat: str = ""
    removed: bool = False
    note: str = ""


def close(workspace: Path, tree: Path, base: str, message: str) -> Closed:
    """Commit what the run changed on its branch, then remove the worktree.

    The worktree is removed only once its changes are safely in a commit. When
    committing fails -- most often because git has no identity configured --
    the worktree is left where it is, changes and all, and the note says why:
    deleting it would delete the run's work.
    """
    if not tree.is_dir():
        return Closed(note=f"the worktree at {tree} is gone; nothing to commit")
    if _git(tree, "add", "-A", timeout=TIMEOUT) is None:
        return Closed(note="git could not stage the run's changes; the worktree is kept")
    staged = _git(tree, "diff", "--cached", "--stat", base, timeout=TIMEOUT) or ""
    if not staged.strip():
        removed = _remove(workspace, tree)
        return Closed(removed=removed, note="the run changed nothing; the branch is at "
                                            "its starting commit")
    if _git(tree, "commit", "-q", "-m", message, timeout=TIMEOUT) is None:
        return Closed(diffstat=staged, note=(
            "git could not commit the run's changes (is user.name and user.email "
            f"set?); they are uncommitted in the worktree at {tree}, which is kept"))
    commit = _git(tree, "rev-parse", "HEAD") or ""
    return Closed(commit=commit, diffstat=staged, removed=_remove(workspace, tree))


def _remove(workspace: Path, tree: Path) -> bool:
    """Take the worktree down and unregister it; the branch stays."""
    if _git(workspace, "worktree", "remove", "--force", str(tree), timeout=TIMEOUT) is None:
        shutil.rmtree(tree, ignore_errors=True)
        _git(workspace, "worktree", "prune")
    return not tree.exists()
