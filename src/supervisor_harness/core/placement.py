"""Placing a model's paths in the tree: the one place path handling reads the disk.

`core/paths.py` decides scope questions from the patterns alone, which is what
lets it be sound. Whether a path a model wrote names a file is a question about
the workspace, so it is asked here, once, where the paths come in.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..models import ExecutionTask, VerifyMethod
from .paths import _META, NOTHING, matches_any

#: Directories a search for "the file a model meant" never descends into:
#: version control, the harness's own store, dependencies and build output.
_NOT_SEARCHED = frozenset({
    ".git", ".hg", ".svn", ".supervisor", "node_modules", ".venv", "venv",
    "__pycache__", "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
})

def placed_in_tree(patterns: list[str], workspace: str | Path) -> tuple[list[str], list[str]]:
    """Model-written paths that name no file, placed where they must have meant.

    Measured on a local model planning a change to this repository: it drew
    the run's envelope as `core/timing.py` for `src/supervisor_harness/core/
    timing.py`. Every pattern was relative and plausible, none named a file,
    and an agent fenced to them could not have written the code the task was
    about. A literal path that does not exist is replaced only when exactly
    one file in the tree ends with it; otherwise it is kept -- it may be a file
    the task will create -- and a note says it names nothing. Globs pass
    through: their emptiness is not evidence of anything.

    Returns the patterns and a note for each one repaired or left unresolved.
    """
    root = Path(workspace)
    if not root.is_dir():
        return list(patterns), []
    out: list[str] = []
    notes: list[str] = []
    files: list[str] | None = None
    for pat in patterns:
        if not pat or _META & set(pat) or pat == NOTHING or (root / pat).exists():
            out.append(pat)
            continue
        if files is None:
            files = _tree_files(root)
        suffix = "/" + pat.strip("/")
        matches = [f for f in files if f.endswith(suffix)]
        if len(matches) == 1:
            out.append(matches[0])
            notes.append(f"`{pat}` names no file in the workspace; placed at "
                         f"`{matches[0]}`, the only file it can mean")
            continue
        out.append(pat)
        if matches:
            notes.append(f"`{pat}` names no file in the workspace and could mean any of "
                         f"{len(matches)}; left as written")
        elif not (root / pat).parent.exists():
            notes.append(f"`{pat}` names no file, in a directory that does not exist")
    return out, notes


#: A path written in prose: at least one directory, and a file extension.
_PATH_IN_PROSE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z]\w{0,7})\b")

#: A source file named with no directory: "the ledger line in reporting.py".
#: Counted only when exactly one file in the tree has that name -- a bare
#: word that merely looks like a file name names nothing, and is dropped.
_BARE_FILE_IN_PROSE = re.compile(
    r"(?<![\w/.-])([\w-]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|go|rs|rb|java|kt|cs|cpp|c|h"
    r"|vue|svelte|css|scss))\b")

#: Words just before a path that say the task reads it rather than changes it.
_READ_FROM = re.compile(
    r"\b(using|uses?|read\w*|from|via|see|mirror\w*|like|following|import\w*|calls?)\b",
    re.IGNORECASE,
)


def named_outside_scope(task: ExecutionTask, workspace: str | Path) -> list[str]:
    """Files the task's own words say it changes, that its scope does not cover.

    Measured on a local model: the plan's envelope named the two files the
    prompt mentioned and missed the one it described by name ("Reporting.
    ledger"), and a task whose action read "In Reporting.ledger (core/
    reporting.py:202-260), append ..." was approved within that envelope. Its
    agent could not write the file the task was about; it spent ten turns and
    three rounds of remediation finding out. Only the owner widens -- so the
    task goes to them before anyone works on it, not after.

    The words are the task's title and action, and the file each inspection
    criterion checks: the files the task must produce. Only writes are fenced,
    so a path the action reads from -- "using the client in src/cache.py" --
    is not counted. A file that does not exist yet counts where its directory
    does, because creating it is a write the scope forbids just the same.
    """
    root = Path(workspace)
    if not task.scope.paths or not root.is_dir():
        return []  # no paths is the whole workspace
    prose = f"{task.title} {task.action}"
    named = [m.group(1) for m in _PATH_IN_PROSE.finditer(prose)
             if not _READ_FROM.search(" ".join(prose[:m.start()].split()[-5:]))]
    named += [m.group(1) for c in task.dod if c.method is VerifyMethod.INSPECTION
              for m in _PATH_IN_PROSE.finditer(c.expect.partition(":")[0])]
    placed, _ = placed_in_tree(list(dict.fromkeys(named)), root)
    bare = [m.group(1) for m in _BARE_FILE_IN_PROSE.finditer(prose)
            if not _READ_FROM.search(" ".join(prose[:m.start()].split()[-5:]))]
    resolved, _ = placed_in_tree(list(dict.fromkeys(bare)), root)
    placed += [p for p in resolved if "/" in p and (root / p).is_file()]
    return [p for p in dict.fromkeys(placed)
            if ((root / p).is_file() or (root / p).parent.is_dir())
            and not matches_any(p, task.scope.paths)]


def _tree_files(root: Path) -> list[str]:
    found: list[str] = []
    for current, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _NOT_SEARCHED]
        base = Path(current).relative_to(root).as_posix()
        found.extend(name if base == "." else f"{base}/{name}" for name in names)
    return found
