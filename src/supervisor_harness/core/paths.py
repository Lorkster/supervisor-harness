"""Path normalisation and scope matching.

Shared by the drift heuristics and the agent toolbox so that "is this file in
scope?" is answered the same way in both places. It previously was not: drift
compared raw reported paths against relative globs, and the toolbox fell back to
a bare string prefix. The first meant an agent reporting absolute paths scored as
entirely out of scope; the second meant a scope of ``src/auth/*`` also authorised
writes to ``src/authority.py``.

Not every reported path can be placed. ``scope_relative`` says so explicitly:
when the caller's workspace is not the one the path was rooted in, there is no
relative form to compare, and "unclassifiable" is the only honest answer.
"""

from __future__ import annotations

import fnmatch
import re

# Suffixes that mean "and everything beneath this directory".
_DIR_SUFFIXES = ("/**/*", "/**", "/*", "/")

# A path that names an absolute location: POSIX root, UNC share, or drive letter.
_ROOTED = re.compile(r"^(?:/|[A-Za-z]:/)")


def _strip_workspace(path: str, workspace: str) -> str | None:
    """``path`` relative to ``workspace``, or ``None`` if it is not beneath it.

    ``path`` is already separator-normalised.
    """
    root = str(workspace).replace("\\", "/").rstrip("/")
    if not root:
        return None
    # Windows differs in case, including the drive letter; compare loosely.
    if path.lower().startswith(root.lower() + "/"):
        return path[len(root) + 1:]
    return None


def normalise_path(raw: str, workspace: str = "") -> str:
    """Reduce a reported path to the workspace-relative form scope patterns use.

    Agents report paths in whatever form their tools produced -- absolute,
    Windows-separated, or ``./``-prefixed -- while scope patterns are always
    workspace-relative globs.
    """
    path = str(raw).strip().replace("\\", "/")
    if not path:
        return ""

    if workspace:
        stripped = _strip_workspace(path, workspace)
        if stripped is not None:
            path = stripped

    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def scope_relative(raw: str, workspace: str = "") -> str | None:
    """The form scope patterns compare against, or ``None`` when there is none.

    A rooted path that does not sit under ``workspace`` has no workspace-relative
    form at all: stripping its root would invent a relative path naming a
    different file, and comparing it against relative globs can only ever say
    "outside scope". Callers judging scope must treat ``None`` as unclassifiable
    rather than as a violation -- the supervisor's workspace need not be the one
    the agent's tools reported against.
    """
    path = str(raw).strip().replace("\\", "/")
    if not path:
        return None

    if _ROOTED.match(path):
        stripped = _strip_workspace(path, workspace) if workspace else None
        if stripped is None:
            return None
        path = stripped

    return normalise_path(path) or None


def path_matches(path: str, pattern: str) -> bool:
    """Whether a workspace-relative path falls under a scope pattern.

    Directory patterns match only on a path boundary, so ``src/auth/**`` covers
    ``src/auth/login.py`` but never ``src/authority.py``.
    """
    pat = str(pattern).strip().replace("\\", "/")
    while pat.startswith("./"):
        pat = pat[2:]
    if not pat or not path:
        return False

    if fnmatch.fnmatch(path, pat):
        return True

    for suffix in _DIR_SUFFIXES:
        if pat.endswith(suffix):
            base = pat[: -len(suffix)]
            return bool(base) and (path == base or path.startswith(base + "/"))
    return False


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(path_matches(path, pattern) for pattern in patterns)
