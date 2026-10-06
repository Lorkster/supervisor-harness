"""Placing a model's paths in the tree: the one place path handling reads the disk.

`core/paths.py` decides scope questions from the patterns alone, which is what
lets it be sound. Whether a path a model wrote names a file is a question about
the workspace, so it is asked here, once, where the paths come in.
"""

from __future__ import annotations

import os
from pathlib import Path

from .paths import _META, NOTHING

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


def _tree_files(root: Path) -> list[str]:
    found: list[str] = []
    for current, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _NOT_SEARCHED]
        base = Path(current).relative_to(root).as_posix()
        found.extend(name if base == "." else f"{base}/{name}" for name in names)
    return found
