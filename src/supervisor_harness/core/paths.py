"""Path normalisation and scope matching.

Shared by the drift heuristics and the agent toolbox so that "is this file in
scope?" is answered the same way in both places. It previously was not: drift
compared raw reported paths against relative globs, and the toolbox fell back to
a bare string prefix. The first meant an agent reporting absolute paths scored as
entirely out of scope; the second meant a scope of ``src/auth/*`` also authorised
writes to ``src/authority.py``.
"""

from __future__ import annotations

import fnmatch

# Suffixes that mean "and everything beneath this directory".
_DIR_SUFFIXES = ("/**/*", "/**", "/*", "/")


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
        root = str(workspace).replace("\\", "/").rstrip("/")
        # Windows differs in case, including the drive letter; compare loosely.
        if root and path.lower().startswith(root.lower() + "/"):
            path = path[len(root) + 1:]

    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


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
