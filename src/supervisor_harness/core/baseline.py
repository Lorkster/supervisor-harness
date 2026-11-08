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

import atexit
import functools
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# The key this module's value is published under is :data:`models.BASELINE_FACT`,
# and it is defined there rather than here on purpose: `agents.brief` reads it
# too, and importing it from `core` was the whole of the `core` <-> `agents`
# package cycle (finding Q-A1). Import it from ``models``.

# The harness's own run directory, written inside the workspace.
STORE_DIRECTORY = ".supervisor"

_TIMEOUT = 15


@functools.lru_cache(maxsize=1)
def _empty_directory() -> str:
    """A directory with nothing in it, for git to look for hooks in."""
    directory = tempfile.mkdtemp(prefix="supervisor-nohooks-")
    atexit.register(shutil.rmtree, directory, True)
    return directory


@functools.lru_cache(maxsize=4)
def _git_program(path_env: str) -> str | None:
    """The git on ``path_env``, as an absolute path -- never one in the current directory.

    Windows looks in the current directory before ``PATH`` for a bare ``git``, and the
    harness runs where the repository is: a ``git.exe`` the repository carries would
    be started with the owner's rights. So ``PATH`` is searched here, by hand, and only
    through entries that are absolute and are not the current directory.
    """
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        return None
    names = ("git.exe",) if os.name == "nt" else ("git",)
    for entry in path_env.split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        try:
            if Path(entry).resolve() == cwd:
                continue
        except OSError:
            continue
        for name in names:
            candidate = os.path.join(entry, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def git_argv(*args: str, workspace: Path | str | None = None) -> list[str] | None:
    """A git command line for a repository that is not the owner's, or ``None`` if there is no git.

    Every git the harness runs goes through here. A repository carries its own
    configuration and hooks, and git runs what they name: ``core.fsmonitor`` in
    ``.git/config`` is a command run by ``git status``, and a hook runs on ``commit`` and
    on ``worktree add``. Read-only lookups ran them as much as writes did. So the
    config that names a program is overridden on the command line, hooks are looked for
    in an empty directory, and nothing takes an optional lock.
    """
    program = _git_program(os.environ.get("PATH", ""))
    if program is None:
        return None
    argv = [program, "--no-optional-locks", "-c", "core.fsmonitor=false",
            "-c", f"core.hooksPath={_empty_directory()}"]
    if workspace is not None:
        argv += ["-C", str(workspace)]
    return [*argv, *args]


def git_environment() -> dict[str, str]:
    """The environment git runs in: the owner's, without credentials (as for any command)."""
    from .dod import child_environment  # dod imports this module
    return {**child_environment(), "GIT_TERMINAL_PROMPT": "0"}


def _git(workspace: Path, *args: str, timeout: float = _TIMEOUT) -> str | None:
    """One git command with a fixed argv, or ``None`` if git could not answer it.

    Read-only everywhere but where a worktree is added and removed: the run's
    own (`core/worktree.py`), and the temporary ones at the baseline that
    `core/fails_before.py` and `core/attribution.py` run checks in. Those write
    under ``.git/worktrees`` and never touch the working tree. A checkout takes
    longer than a lookup, so they pass their own ``timeout``. See :func:`git_argv`
    for what is switched off.
    """
    argv = git_argv(*args, workspace=workspace)
    if argv is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, no model input
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False, env=git_environment(),
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


def commit_from_fact(fact: str) -> str:
    """The bare commit out of the sentence :func:`git_baseline` wrote.

    Lives here rather than at the reader, beside the function that formats it,
    for the reason `BASELINE_FACT` lives beside the dict it keys: a parser that
    drifts from its formatter fails silently, and the two are only safe to
    change together if they are next to each other.

    The fact is a sentence because it is written for an agent to read -- "the
    tree already had four modified files" is part of what a baseline means. A
    caller that wants to *run git against it* needs only the first token, and
    handing it the whole sentence produces `fatal: ambiguous argument`, which is
    exactly how this function came to exist.
    """
    match = re.search(r"`([0-9a-f]{7,40})`", str(fact))
    return match.group(1) if match else ""


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
