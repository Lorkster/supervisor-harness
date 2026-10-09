"""A scorecard for a whole run: what the harness recorded, and what is true.

The two diverged in both directions in the go-live runs -- a branch that passed
every check the project has while the harness verified none of its tasks, and
verified tasks on a branch whose browser tests failed. So the scorecard carries
both: counts from the run's own record, and the project's acceptance commands
run on the tree the run left.

The acceptance commands are the owner's -- the request's definition of done,
given on the command line -- never a model's, and they run through the shell
because that is how a person would type them (``npm`` on Windows is a ``.cmd``).
"""

from __future__ import annotations

import contextlib
import subprocess
import tempfile
from collections import Counter
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.dod import child_environment
from ..models import AgentKind, AgentStatus, RunState

ACCEPT_TIMEOUT = 1800.0


def record_counts(state: RunState) -> dict[str, Any]:
    """What the run's own record says."""
    tasks = list(state.tasks.values())
    envelope = set((state.envelope.paths if state.envelope else []) or [])
    notes = [n.text for n in state.notes]
    stamps = sorted(n.ts for n in state.notes if n.ts)
    minutes = 0.0
    if len(stamps) >= 2:
        span = datetime.fromisoformat(stamps[-1].replace("Z", "+00:00")) - \
            datetime.fromisoformat(stamps[0].replace("Z", "+00:00"))
        minutes = round(span.total_seconds() / 60, 1)
    return {
        "run_id": state.id,
        "phase": str(state.phase),
        "minutes": minutes,
        "tasks": len(tasks),
        "tasks_ran": sum(1 for t in tasks if t.attempts > 0),
        "tasks_verified": sum(1 for t in tasks if str(t.status) == "verified"),
        "task_status": dict(Counter(str(t.status) for t in tasks)),
        "escalations": dict(Counter(str(e.reason) for e in state.escalations.values())),
        "implementers_stopped": sum(
            1 for a in state.agents.values()
            if a.kind is AgentKind.EXECUTION and a.status is AgentStatus.STOPPED),
        "criteria": sum(len(t.dod) for t in tasks),
        "scopes_equal_to_envelope": sum(
            1 for t in tasks if envelope and set(t.scope.paths) == envelope),
        "synthesis_sent_back": any("synthesis sent back" in n for n in notes),
        "revised_after_veto": sum(1 for n in notes if "revised after a" in n),
    }


@contextlib.contextmanager
def _tree(state: RunState, workspace: Path | None) -> Iterator[Path | None]:
    """The tree the run left: given, its live worktree, or its branch checked out."""
    if workspace is not None:
        yield workspace
        return
    wt = state.worktree
    if wt is not None and wt.path and not wt.removed and Path(wt.path).is_dir():
        yield Path(wt.path)
        return
    ref = (wt.commit or wt.branch) if wt is not None else ""
    if not ref:
        yield None
        return
    repo = Path(state.workspace)
    temp = Path(tempfile.mkdtemp(prefix="score-")) / "tree"
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell, no model input
        ["git", "-C", str(repo), "worktree", "add", "--detach",  # noqa: S607 - on PATH
         str(temp), ref], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        yield None
        return
    try:
        yield temp
    finally:
        subprocess.run(  # noqa: S603 - fixed argv, no shell, no model input
            ["git", "-C", str(repo), "worktree", "remove", "--force",  # noqa: S607
             str(temp)], capture_output=True, check=False)


def _run(command: str, cwd: Path) -> dict[str, Any]:
    try:
        # The owner's own acceptance command, typed on the command line, never a
        # model's: run as they would run it -- in the environment the harness gives
        # its own checks, so the scorer's SUPERVISOR_HOME is not the project's.
        done = subprocess.run(command, cwd=cwd, shell=True,  # noqa: S602
                              env=child_environment(), capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=ACCEPT_TIMEOUT, check=False)
        tail = (done.stdout + done.stderr).strip()[-800:]
        return {"command": command, "passed": done.returncode == 0,
                "exit": done.returncode, "tail": tail}
    except subprocess.TimeoutExpired:
        return {"command": command, "passed": False, "exit": None,
                "tail": f"timed out after {ACCEPT_TIMEOUT:.0f}s"}


def scorecard(state: RunState, accept: list[str], setup: list[str] | None = None,
              workspace: Path | None = None) -> dict[str, Any]:
    """The run's record, and its acceptance commands run on the tree it left."""
    card: dict[str, Any] = {"record": record_counts(state), "setup": [], "acceptance": []}
    if not accept:
        return card
    with _tree(state, workspace) as tree:
        if tree is None:
            card["acceptance"] = [{"command": c, "passed": False, "exit": None,
                                   "tail": "the run left no tree to check"} for c in accept]
            return card
        card["tree"] = str(tree)
        card["setup"] = [_run(c, tree) for c in setup or []]
        card["acceptance"] = [_run(c, tree) for c in accept]
    card["accepted"] = all(a["passed"] for a in card["acceptance"])
    return card


def render(card: dict[str, Any]) -> str:
    r = card["record"]
    lines = [
        f"# Scorecard {r['run_id']}",
        "",
        f"- phase {r['phase']}, {r['minutes']} min",
        f"- tasks: {r['tasks']} proposed, {r['tasks_ran']} ran, {r['tasks_verified']} verified "
        f"{r['task_status']}",
        f"- escalations: {r['escalations'] or 'none'}",
        f"- implementers stopped: {r['implementers_stopped']}",
        f"- criteria: {r['criteria']}; task scopes equal to the envelope: "
        f"{r['scopes_equal_to_envelope']}/{r['tasks']}",
        f"- synthesis sent back: {r['synthesis_sent_back']}; revised after a veto: "
        f"{r['revised_after_veto']}",
    ]
    for label in ("setup", "acceptance"):
        if card[label]:
            lines += ["", f"## {label.capitalize()}"]
            lines += [f"- {'PASS' if a['passed'] else 'FAIL'} `{a['command']}`"
                      + ("" if a["passed"] else f" -- {a['tail'][-200:]}")
                      for a in card[label]]
    if "accepted" in card:
        lines += ["", f"**Accepted: {'yes' if card['accepted'] else 'no'}**"]
    return "\n".join(lines)
