"""Placing a model's paths in the tree: the one place path handling reads the disk.

`core/paths.py` decides scope questions from the patterns alone, which is what
lets it be sound. Whether a path a model wrote names a file is a question about
the workspace, so it is asked here, once, where the paths come in.
"""

from __future__ import annotations

import difflib
import os
import re
from pathlib import Path

from ..models import ExecutionTask, VerifyMethod
from .dod import shell_split
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



def correct_inspection_paths(task: ExecutionTask, workspace: str | Path | None) -> list[str]:
    """Put right an inspection's file the model placed in a directory that does not exist.

    Measured in go-live run 17: "src/styles/tokens.css: --state-offline", where
    the project's file is `src/app/styles/tokens.css` -- the task changed the
    real file, and the criterion failed on every attempt because the file it
    names does not exist. When the named directory does not exist (so the task
    is not creating a file there) and exactly one file in the tree has the name,
    inside the task's scope, that file is the one meant. Returns a note per
    correction.
    """
    if workspace is None or not Path(workspace).is_dir():
        return []
    root = Path(workspace)
    notes: list[str] = []
    files: list[str] | None = None
    for crit in task.dod:
        raw, colon, needle = crit.expect.partition(":")
        path = raw.strip()
        if (crit.method is not VerifyMethod.INSPECTION or not colon or not path
                or (root / path).parent.is_dir()):
            continue
        files = _tree_files(root) if files is None else files
        name = path.rsplit("/", 1)[-1]
        same = [f for f in files if f.rsplit("/", 1)[-1] == name
                and (not task.scope.paths or matches_any(f, task.scope.paths))]
        if len(same) != 1:
            continue
        crit.expect = f"{same[0]}:{needle}"
        notes.append(f"criterion {crit.statement!r} named {path}, which is not in the tree; "
                     f"it checks {same[0]}, the one file of that name")
    return notes

#: A criterion's claim that the test it runs is there already: "the existing
#: tile-loader tests still pass". Not "still" or "existing" alone: "fetchJson
#: still throws" and "using the existing helper" are about the code, and its
#: test may be the one the task writes.
_CLAIMS_EXISTING = re.compile(
    r"\b(?:still|continues?\s+to)\s+pass(?:es)?\b"
    r"|\b(?:pre-)?existing(?:\s+[\w'-]+){0,3}?\s+(?:tests?|suites?|specs?|behaviou?rs?)\b",
    re.IGNORECASE)
#: A file name's extension, which is what tells a path from a word.
_EXTENSION = re.compile(r"\.[A-Za-z]\w{0,7}$")


def _command_paths(command: str) -> list[str]:
    """The workspace paths a criterion's command names: ``tests/x.test.ts``,
    ``tests/test_a.py::test_b`` -- at least one directory and an extension."""
    paths: list[str] = []
    for token in shell_split(command)[1:]:
        if token.startswith("-") or "=" in token or _META & set(token):
            continue
        path = token.split("::", 1)[0].replace("\\", "/").removeprefix("./")
        if ("/" not in path or not _EXTENSION.search(path.rsplit("/", 1)[-1])
                or path.startswith("/") or re.match(r"[A-Za-z]:", path)
                or ".." in path.split("/")):
            continue
        paths.append(path)
    return paths


def _nearest(path: str, files: list[str]) -> list[str]:
    """Up to three files the model may have meant: the same name elsewhere, or the
    closest paths of the same kind (``.test.ts`` for ``.test.ts``)."""
    name = path.rsplit("/", 1)[-1]
    same = [f for f in files if f.rsplit("/", 1)[-1] == name]
    if same:
        return same[:3]
    kind = "." + name.split(".", 1)[1]
    return difflib.get_close_matches(path, [f for f in files if f.endswith(kind)], n=3,
                                     cutoff=0.5)


def criteria_running_missing_files(tasks: list[ExecutionTask],
                             workspace: str | Path | None) -> list[tuple[ExecutionTask, str]]:
    """Criteria that run a file the workspace does not have, and no task will make.

    Measured in baseline 3: "The existing tile-loader 404 absent-tile behavior
    still passes after the code split", run as `npm run test --
    tests/data/tiles/loader.test.ts` -- a file that never existed, so the
    criterion failed before the change and after it, and said nothing about
    the tile loader either time. A file a criterion runs may be a test the
    plan will write, so a missing one is a problem only where the criterion
    says the test is already there, or no task's scope would let anyone create
    it. Each comes with the files nearest what was written, for the planner to
    choose from.
    """
    root = Path(workspace) if workspace else None
    if root is None or not root.is_dir():
        return []
    anywhere = any(not t.scope.paths for t in tasks)   # no paths is the whole workspace
    found: list[tuple[ExecutionTask, str]] = []
    files: list[str] | None = None
    for task in tasks:
        for crit in task.dod:
            if (crit.method not in (VerifyMethod.COMMAND, VerifyMethod.TEST,
                                    VerifyMethod.FAILS_BEFORE) or not crit.command.strip()):
                continue
            for path in _command_paths(crit.command):
                if (root / path).exists():
                    continue
                said_existing = (crit.method is not VerifyMethod.FAILS_BEFORE
                                 and bool(_CLAIMS_EXISTING.search(crit.statement)))
                creatable = anywhere or any(matches_any(path, t.scope.paths) for t in tasks)
                if creatable and not said_existing:
                    continue
                files = _tree_files(root) if files is None else files
                near = _nearest(path, files)
                why = ("it says that test is already there" if said_existing
                       else "no task's scope would let it be created")
                found.append((task, (
                    f"task {task.title!r}, criterion {crit.statement!r}: `{crit.command}` "
                    f"runs {path}, which is not in the workspace, and {why}"
                    + (f"; the nearest files that are: {', '.join(near)}" if near else ""))))
    return found


def _tree_files(root: Path) -> list[str]:
    found: list[str] = []
    for current, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _NOT_SEARCHED]
        base = Path(current).relative_to(root).as_posix()
        found.extend(name if base == "." else f"{base}/{name}" for name in names)
    return found
