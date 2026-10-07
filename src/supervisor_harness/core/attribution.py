"""Whose failures are these? A failing check, run again on the baseline commit.

Measured in a go-live run (plantsandclimate P3-18, a local model): a change
broke thirty tests, the full-suite criterion failed -- and the implementer
called the failures "pre-existing". The drift judge took its word, and stopped
the next implementer for fixing them. The harness had everything it needed to
know better: the run's baseline commit, and the command.

So when a test or command criterion the harness ran fails, the same command is
run once more, in a temporary worktree at the baseline, and the evidence says
which it was: the command passes there, so the failures are this change's; or
it fails there too, so some of them predate it. The verdict is about the
command as a whole -- it works for any runner, which a per-test comparison
(:mod:`.fails_before`, pytest only) does not.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .baseline import _git
from .dod import run_bounded, shell_split, unsafe_command
from .fails_before import _remove_worktree, baseline_environment
from .worktree import prepare


def baseline_verdict(command: str, workspace: Path, baseline: str, timeout: float) -> str:
    """A line for the evidence of a failed check: does ``command`` pass on ``baseline``?

    Empty when it cannot be told -- no baseline, a command the harness will not
    run, a worktree that could not be made -- because a failed check's evidence
    is no place for a guess.
    """
    if not baseline or unsafe_command(command) is not None:
        return ""
    argv = shell_split(command)
    runner = shutil.which(argv[0]) if argv else None
    if runner is None:
        return ""
    with tempfile.TemporaryDirectory(prefix="supervisor-attribution-",
                                     ignore_cleanup_errors=True) as scratch:
        tree = Path(scratch) / "baseline"
        if _git(workspace, "worktree", "add", "--detach", str(tree), baseline,
                timeout=timeout) is None:
            return ""
        try:
            prepare(tree)
            done = run_bounded([runner, *argv[1:]], tree, timeout,
                               extra_env=baseline_environment(tree))
        except (OSError, subprocess.TimeoutExpired):
            return ""
        finally:
            _remove_worktree(workspace, tree)
    short = baseline[:12]
    if done.returncode == 0:
        return (f"[supervisor] `{command}` passes on the baseline commit {short}: these "
                "failures were introduced by this run's changes, not inherited.")
    return (f"[supervisor] `{command}` also fails on the baseline commit {short} (exit "
            f"{done.returncode}), so some of these failures predate this run; compare "
            "them against the baseline before calling any of them this change's.")
