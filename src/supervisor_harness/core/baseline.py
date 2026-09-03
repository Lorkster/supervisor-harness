"""The commit a run measures against.

Several agents work in one tree at the same time, so "the existing test suite
still passes" is not a question about the tree: by the time a verifier asks it,
the tree is the baseline plus every agent's work so far, and the number it gets
back depends on when it looked. A real run made this concrete -- the same
criterion, on the same suite, was reported as 87, 99 and 100 tests by three
verifiers, each of which then had to reason about why its own number was
different.

Naming one commit at the start of the run does not stop the tree moving. What it
does is give every brief the same fixed point, so a count is reported *against*
something and a failure can be attributed to the diff that caused it rather than
to whoever happened to run the suite last.

The lookup is read-only and its argv is fixed -- no part of it comes from a
model -- so it is not gated behind ``policy.allow_command_execution``, which
exists to decide whether *agents* may run commands. A workspace that is not a
git repository, or a machine with no git, simply has no baseline, and the briefs
say so rather than inventing one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# The key this module's value is published under is :data:`models.BASELINE_FACT`,
# and it is defined there rather than here on purpose: `agents.brief` reads it
# too, and importing it from `core` was the whole of the `core` <-> `agents`
# package cycle (finding Q-A1). Import it from ``models``.

# The harness's own run directory, written inside the workspace.
STORE_DIRECTORY = ".supervisor"

_TIMEOUT = 15


def _git(workspace: Path, *args: str) -> str | None:
    """One read-only git command, or ``None`` if git could not answer it."""
    try:
        # S607: `git` is looked up on PATH on purpose -- pinning an absolute
        # path would break every machine that installs it somewhere else.
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, no model input
            ["git", "-C", str(workspace), *args],  # noqa: S607 - resolved on PATH
            capture_output=True, text=True, timeout=_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _own_files_excluded(status: str) -> list[str]:
    """``git status --porcelain`` lines that are not the harness's own writing.

    The run directory is created inside the workspace before this runs, and a
    workspace that has not ignored ``.supervisor/`` would otherwise report it as
    a change that was there "before the run started" -- which is both untrue and
    exactly the kind of unexplained difference this fact exists to remove.
    """
    return [
        line for line in status.splitlines()
        if line[3:].lstrip('"').split("/")[0] != STORE_DIRECTORY
    ]


def git_baseline(workspace: Path) -> str:
    """A one-line description of the commit this run starts from, or ``""``.

    The dirty-tree note is part of it on purpose. "Baseline plus this task's
    diff" is only a complete account of the tree when the tree started clean;
    when it did not, an agent comparing against the baseline will find changes
    belonging to nobody in the run, and it is better told that up front than
    left to discover it as an anomaly.
    """
    commit = _git(Path(workspace), "rev-parse", "HEAD")
    if not commit:
        return ""

    short = commit[:12]
    branch = _git(Path(workspace), "rev-parse", "--abbrev-ref", "HEAD") or ""
    status = _git(Path(workspace), "status", "--porcelain")

    where = f"`{short}`" + (f" on `{branch}`" if branch and branch != "HEAD" else "")
    if status is None:
        return where
    changed = len(_own_files_excluded(status))
    if changed:
        return (
            f"{where}, with {changed} file(s) already modified in the working tree "
            "before the run started"
        )
    return f"{where}, working tree clean when the run started"
