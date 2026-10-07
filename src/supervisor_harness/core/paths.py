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

:func:`pattern_within` and its callers answer a different question -- not "is
this path in scope?" but "is this scope inside that one?" -- which is what a
run-level envelope needs and what nothing here previously asked. It is decided
without touching the filesystem, so it is sound rather than complete: see its
docstring for the three cases it settles and the two it refuses.
"""

from __future__ import annotations

import fnmatch
import re

# Suffixes that mean "and everything beneath this directory".
_DIR_SUFFIXES = ("/**/*", "/**", "/*", "/")

# A path that names an absolute location: POSIX root, UNC share, or drive letter.
_ROOTED = re.compile(r"^(?:/|[A-Za-z]:/)")

# The characters that make a pattern a glob rather than a literal path.
_META = frozenset("*?[")

# Patterns that match every path there is. ``fnmatch``'s ``*`` crosses ``/``,
# so both of these do. They are named because containment against them has no
# directory base to reason about: without this, an envelope written as ``**``
# -- the natural way to say "everywhere" -- would contain nothing at all.
_UNIVERSAL = frozenset({"*", "**"})

#: A pattern that matches no path at all.
#:
#: An empty pattern list means "the whole workspace" everywhere downstream --
#: the toolbox reads ``if scope.paths and not matches_any(...)`` -- so an
#: intersection that comes out empty cannot be written as ``[]``. That would
#: widen the very scope it was computed to narrow, which is the worst failure
#: this module could have. It is written as ``[NOTHING]`` instead, which
#: :func:`path_matches` refuses for every path, including a path that happens
#: to be spelled the same way.
NOTHING = "<nothing>"


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


def relative_patterns(patterns: list[str], workspace: str) -> list[str]:
    """Scope patterns as a model wrote them, in the workspace-relative form they are compared in.

    A planning model is shown the workspace's absolute path, and sometimes hands
    it back as a lens's or a task's scope. Every comparison downstream -- drift,
    audit, the toolbox's write fence, the envelope -- is against
    workspace-relative paths, so an absolute pattern matched nothing: every file
    an agent read was "outside the declared scope", and every agent scoped that
    way scored as drifting on its first turn.

    So, once, where the patterns come in:

    * the workspace itself becomes ``**``, the whole workspace -- not ``[]``,
      which also means the whole workspace but reads as "unscoped" to the
      toolbox;
    * a path beneath it becomes the relative path, globs and all;
    * a rooted path anywhere else becomes :data:`NOTHING`. It names no file in
      this workspace, and dropping it could empty the list and widen the scope.

    Relative patterns pass through unchanged.
    """
    root = str(workspace).replace("\\", "/").rstrip("/")
    out: list[str] = []
    for raw in patterns:
        pat = normalise_pattern(raw)
        if not pat or not _ROOTED.match(pat):
            if pat:
                out.append(pat)
            continue
        bare = pat.rstrip("/")
        if root and bare.lower() == root.lower():
            out.append("**")
            continue
        stripped = _strip_workspace(pat, root) if root else None
        out.append(stripped if stripped else NOTHING)
    return out


def normalise_pattern(raw: str) -> str:
    """A scope pattern in the one form the comparisons here expect.

    Shared by matching and by containment so the two cannot drift apart: a
    pattern that ``path_matches`` reads as ``src/auth/**`` must not be a
    different pattern to ``pattern_within``.
    """
    pat = str(raw).strip().replace("\\", "/")
    while pat.startswith("./"):
        pat = pat[2:]
    return pat


def _directory_base(pattern: str) -> str | None:
    """``pattern``'s base when it means "this directory and everything under it".

    ``None`` when it means anything else, including a bare ``**`` or ``/``,
    whose base would be empty and so would claim the whole workspace from a
    pattern that never named a directory.
    """
    for suffix in _DIR_SUFFIXES:
        if pattern.endswith(suffix):
            return pattern[: -len(suffix)] or None
    return None


def _literal_prefix(pattern: str) -> str:
    """The leading run of ``pattern`` before its first metacharacter.

    Every path the pattern can match starts with this, which is what makes a
    containment test possible without enumerating a filesystem.
    """
    for index, char in enumerate(pattern):
        if char in _META:
            return pattern[:index]
    return pattern


def path_matches(path: str, pattern: str) -> bool:
    """Whether a workspace-relative path falls under a scope pattern.

    Directory patterns match only on a path boundary, so ``src/auth/**`` covers
    ``src/auth/login.py`` but never ``src/authority.py``.
    """
    pat = normalise_pattern(pattern)
    if not pat or not path:
        return False
    # Not left to fnmatch: a file really named ``<nothing>`` would otherwise
    # match the pattern that exists to match nothing.
    if pat == NOTHING:
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


# --------------------------------------------------------------------------
# Containment: is one glob set inside another?
# --------------------------------------------------------------------------


def pattern_within(inner: str, outer: str) -> bool:
    """Whether every path ``inner`` can match is a path ``outer`` matches too.

    Sound but not complete. A ``True`` answer is a proof; a ``False`` answer
    means only "not provably contained", which is the safe direction for the
    callers here, since they narrow on ``False``.

    Three cases are decided:

    * ``inner`` is :data:`NOTHING`, which matches no path, so it is inside
      anything; or ``outer`` is ``*`` or ``**``, which match every path, so
      everything is inside it.
    * ``inner`` names a concrete path -- no metacharacters -- and the question
      collapses to :func:`path_matches` on that one path, which settles it
      exactly.
    * ``outer`` is a directory pattern (``src/**``, ``src/*``, ``src/``) and
      ``inner``'s literal prefix already lies at or under its base. Every path
      ``inner`` matches begins with that prefix, and everything under the base
      is in ``outer``, so containment follows from the patterns alone. The
      prefix is compared on a path boundary, so ``src/auth/*`` is inside
      ``src/**`` and ``srcfoo/*`` is not.

    Two are deliberately refused, and answer ``False`` where the truth may be
    ``True``:

    * containment that would need two wildcards compared. ``src/auth/*.py`` is
      inside ``src/**/*.py``, and this says no: ``outer`` is not a directory
      pattern, so there is nothing to reason about but the globs themselves.
    * containment in a *union* rather than in a single member. ``src/*`` is
      inside ``{src/a*, src/b*}`` on a tree whose ``src`` holds only those two
      entries -- but that depends on the filesystem, not on the patterns, and
      a fence that changes meaning when a directory is created is not a fence.
    """
    left = normalise_pattern(inner)
    right = normalise_pattern(outer)
    if not left or not right:
        return False
    if left == NOTHING:
        return True
    if right == NOTHING:
        return False
    if left == right:
        return True
    if right in _UNIVERSAL:
        return True

    if not (_META & set(left)):
        return path_matches(left, right)

    base = _directory_base(right)
    if base is None:
        return False
    prefix = _literal_prefix(left)
    return prefix == base or prefix.startswith(base + "/")


def globs_within(inner: list[str], outer: list[str]) -> bool:
    """Whether the path set ``inner`` describes lies inside ``outer``'s.

    An empty list is the whole workspace -- the meaning the toolbox already
    gives it -- so an empty ``outer`` contains everything, and an empty
    ``inner`` is inside a non-empty ``outer`` only if the workspace is, which
    it is not.
    """
    left = _clean(inner)
    right = _clean(outer)
    if not right:
        return True
    if not left:
        return False
    return all(any(pattern_within(i, o) for o in right) for i in left)


def narrow_globs(inner: list[str], outer: list[str]) -> list[str]:
    """The largest path set this module can *prove* lies inside both.

    For each pair it keeps whichever pattern is contained in the other, and
    drops the pair when neither containment is decidable -- so the result is
    sound in the direction that matters: everything it returns is inside both
    inputs, and it may return less than the true intersection rather than more.

    Never ``[]`` when ``outer`` is non-empty. An empty list means the whole
    workspace downstream, so an intersection that vanishes is returned as
    ``[NOTHING]``.
    """
    left = _clean(inner)
    right = _clean(outer)
    if not right:
        return left
    if not left:
        return right

    kept: list[str] = []
    for i in left:
        for o in right:
            if pattern_within(i, o):
                pick = i
            elif pattern_within(o, i):
                pick = o
            else:
                continue
            if pick not in kept:
                kept.append(pick)
    return kept or [NOTHING]


def globs_may_overlap(left: list[str], right: list[str]) -> bool:
    """Whether two path sets could name a file in common.

    Sound in the other direction from :func:`pattern_within`: ``False`` is a
    proof that no path is in both, and anything undecided answers ``True``.
    Its caller keeps two writers apart on ``True``, so an undecided pair costs
    parallelism and never a collision. An empty list is the whole workspace.
    """
    a, b = _clean(left), _clean(right)
    if not a or not b:
        return True
    return any(_pattern_may_overlap(x, y) for x in a for y in b)


def _pattern_may_overlap(x: str, y: str) -> bool:
    if NOTHING in (x, y):
        return False
    if pattern_within(x, y) or pattern_within(y, x):
        return True
    # A single file is settled by now: `pattern_within` matched it exactly
    # against the other pattern, and it was not in it.
    if any(not (_META & set(p)) and _directory_base(p) is None for p in (x, y)):
        return False
    # Two globs, or a glob and a directory: disjoint only where what each must
    # begin with has already diverged. Compared as strings, not on a path
    # boundary, so `src` against `srcfoo/*` answers "may overlap" -- the safe
    # way to be wrong.
    px = _directory_base(x) or _literal_prefix(x)
    py = _directory_base(y) or _literal_prefix(y)
    return px.startswith(py) or py.startswith(px)


def _clean(patterns: list[str]) -> list[str]:
    """``patterns`` normalised, with the empty ones dropped and order kept."""
    out: list[str] = []
    for raw in patterns or []:
        pat = normalise_pattern(raw)
        if pat and pat not in out:
            out.append(pat)
    return out
