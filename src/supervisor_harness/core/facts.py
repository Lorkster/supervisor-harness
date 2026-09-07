"""Whether a fact one agent established is still true when another reads it.

A run's agents write into one tree at the same time. A lens establishes "the
counters live in `src/cache.py`" during `analyzing`; a verifier inherits it
during `verifying`, three agents and a hundred edits later, and nothing in the
brief tells it which of those it is reading. The claim is rendered the same
whether the file is untouched, rewritten, or gone.

`core/baseline.py` already fixed the version of this problem that had a number
attached -- three verifiers counting 87, 99 and 100 tests. This is the same
problem without one: a *statement* about a moving tree, read later as though the
tree had stood still.

## What it borrows, and from where

NOOA's memory subsystem resolves a stored reference to LIVE or DANGLING, and
renders a dangling one as the write-time snapshot **clearly stamped as such** --
their reasoning being that the honest semantic for cross-agent recall is that
reader B sees writer A's snapshot, labelled. That is exactly what is missing
here, with one state added: their targets vanish, and ours mostly get *edited*,
so a fact whose file still exists but has changed is its own answer.

Their security rule is taken verbatim, because it applies verbatim: resolution
is **strict path lookup, never eval**. A fact's anchor is written by a model, on
a blackboard other agents read, so an anchor that could be evaluated would be an
injection primitive with an audience. It names a file, and nothing else, and
nothing here runs anything.

## What is checked, and what is not

Only that the anchor's *bytes* are the ones the fact was recorded against. That
is a coarse signal on purpose: a fact about a function is stale if someone
rewrote a different function in the same file, which over-reports. The
alternative -- tracking a symbol, or a line range -- under-reports precisely
when it matters, because the edit that invalidates "the counters live here" is
usually the one that moved them somewhere else in the same file.

Over-reporting costs a sentence in a brief and asks an agent to look again.
Under-reporting hands a verifier a false statement in the voice of a peer. The
same asymmetry decides `contested_keys` one module over, and it decides the same
way here.

## Who actually sees a stamp

An agent briefed **after** the change. Not one that already holds a brief: a
brief is rendered once and reused, deliberately, because it is the fixed text
drift is scored against (`core/packets.py`), and rewriting it mid-run would move
the ruler while measuring. So a lens that is part-way through its turns keeps
the context it started with, and its continuation carries a directive rather
than a re-rendered brief.

That is the right split rather than a gap, because of who the readers are. The
agent this exists to protect is the one that inherits a claim it had no part in
making and cannot easily check: an **execution agent**, briefed during
`executing`, long after the analysis that established the fact and after other
agents have been writing into the tree. Its brief is rendered fresh, and it
carries the stamp. An agent that established the fact itself, or was working
beside the agent that did, is the reader who least needs telling.

A **verifier** never sees this, and that is not an oversight in either
direction: `build_verification_brief` carries no shared context at all, because
a verifier inheriting its peers' readings is a verifier checking the story
rather than the tree. It is given the task, the criteria and the baseline, and
it is expected to establish the rest itself. A stale-fact stamp would be an
improvement to a section that should not exist there.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path

from ..models import Fact
from .paths import scope_relative

#: Anchors bigger than this are recorded without a digest rather than read into
#: memory on every brief. A fact anchored to a two-megabyte file is anchored to
#: something nobody edits by hand anyway.
MAX_ANCHOR_BYTES = 2_000_000

#: How much of the digest is kept. A fact is not a security boundary and the
#: comparison is against a value written minutes earlier in the same run; the
#: full digest would be noise on every event payload.
DIGEST_CHARS = 16


class Liveness(StrEnum):
    """Whether the thing a fact is about still looks the way it did."""

    UNANCHORED = "unanchored"   #: the fact names no file; nothing to check
    LIVE = "live"               #: the anchor is byte-identical to when recorded
    CHANGED = "changed"         #: the anchor exists and has been edited since
    GONE = "gone"               #: the anchor is no longer there
    UNREADABLE = "unreadable"   #: it is there and could not be read


#: Characters a path in a repository does not contain, and a shell, a template
#: or an expression does. An anchor carrying one of these is not a filename that
#: happens to look odd -- it is something written to be interpreted, and the
#: answer to it is that nothing here interprets anything.
_NOT_IN_A_PATH = frozenset("\"'`$(){}[]|;<>\n\r\t*?")


def resolve_anchor(raw: str, workspace: Path | str) -> str:
    """A fact's anchor as a workspace-relative path, or ``""`` if it is not one.

    Strict path resolution and nothing else -- no globbing, no searching, and
    emphatically no evaluation. Three gates, and a candidate has to pass all of
    them:

    1. it contains no character a repository path does not contain, so an anchor
       written as an expression is refused as an anchor rather than normalised
       into a plausible-looking one;
    2. it has a workspace-relative form at all, which rules out a rooted path
       somewhere else on the machine;
    3. resolving it against the workspace lands *inside* the workspace, which is
       the gate that actually holds -- ``../`` climbs, symlinks and Windows
       short names all fail here rather than at the syntax check above.

    A refused anchor becomes no anchor. That reads as "not checkable", which is
    the honest answer, rather than as a path to a file that was never named.
    """
    text = str(raw).strip()
    if not text or _NOT_IN_A_PATH & set(text):
        return ""
    relative = scope_relative(text, str(workspace))
    if not relative:
        return ""
    root = Path(workspace).resolve()
    try:
        candidate = (root / relative).resolve()
    except (OSError, ValueError):
        return ""
    return relative if root == candidate or root in candidate.parents else ""


def anchor_from_evidence(evidence: str) -> str:
    """The file an evidence string cites, if it cites one.

    Every brief asks for evidence as "file:line, or real output", so most facts
    already name their anchor -- informally, in a field written for a reader.
    Reading it here means the liveness check works on facts produced before
    anchors existed, and on models that ignore the field. A first token that
    looks like a path with an extension is taken; anything else is not guessed
    at, because a wrong anchor is worse than none.
    """
    first = str(evidence).strip().split()[0] if str(evidence).strip() else ""
    candidate = first.replace("\\", "/").split(":")[0].rstrip(",;")
    if "/" not in candidate and "." not in candidate:
        return ""
    stem = candidate.rsplit("/", 1)[-1]
    return candidate if "." in stem and not stem.startswith(".") else ""


def digest_anchor(anchor: str, workspace: Path | str) -> str:
    """A short digest of the anchor's current bytes, or ``""`` if unreadable."""
    if not anchor:
        return ""
    path = Path(workspace) / anchor
    try:
        if not path.is_file() or path.stat().st_size > MAX_ANCHOR_BYTES:
            return ""
        return hashlib.sha256(path.read_bytes()).hexdigest()[:DIGEST_CHARS]
    except OSError:
        return ""


def anchor_fact(fact: Fact, workspace: Path | str) -> Fact:
    """Stamp a fact with what it is about and how that looked when recorded.

    Mutates and returns the fact, which is how the rest of the parse path works.
    A fact whose anchor cannot be digested keeps the anchor and loses the digest:
    knowing which file a claim is about is useful even when its liveness cannot
    be judged, and an empty digest reads as "not checkable" rather than as a
    match.
    """
    anchor = resolve_anchor(fact.anchor, workspace) if fact.anchor else ""
    if not anchor:
        anchor = resolve_anchor(anchor_from_evidence(fact.evidence), workspace)
    fact.anchor = anchor
    fact.anchor_digest = digest_anchor(anchor, workspace) if anchor else ""
    return fact


def liveness(fact: Fact, workspace: Path | str) -> Liveness:
    """Whether this fact's anchor still looks the way it did when recorded."""
    if not fact.anchor:
        return Liveness.UNANCHORED
    path = Path(workspace) / fact.anchor
    try:
        if not path.is_file():
            return Liveness.GONE
    except OSError:
        return Liveness.UNREADABLE
    if not fact.anchor_digest:
        # Recorded without a digest: too large, or unreadable at the time. The
        # anchor is still worth naming and its liveness is still unknown, and
        # those are different from "unchanged".
        return Liveness.UNREADABLE
    current = digest_anchor(fact.anchor, workspace)
    if not current:
        return Liveness.UNREADABLE
    return Liveness.LIVE if current == fact.anchor_digest else Liveness.CHANGED


#: What a reader is told for each state. Only the states that change how a
#: statement should be read say anything: an unanchored or live fact reads as it
#: always did, because adding "(still true)" to every line would train a reader
#: to skip the line that says otherwise.
_NOTES: dict[Liveness, str] = {
    Liveness.CHANGED: (
        "`{anchor}` has been edited since this was recorded at {ts}. Treat the "
        "statement as a snapshot, not as current: check it before relying on it"
    ),
    Liveness.GONE: (
        "`{anchor}` no longer exists. This was true of a file that has since "
        "been removed or moved"
    ),
    Liveness.UNREADABLE: (
        "`{anchor}` could not be checked, so whether this is still true is "
        "unknown"
    ),
}


def stamp(fact: Fact, state: Liveness) -> str:
    """The sentence appended to a fact whose anchor has moved, or ``""``."""
    template = _NOTES.get(state)
    if template is None:
        return ""
    return template.format(anchor=fact.anchor, ts=fact.ts[:19].replace("T", " "))


__all__ = [
    "DIGEST_CHARS",
    "MAX_ANCHOR_BYTES",
    "Liveness",
    "anchor_fact",
    "anchor_from_evidence",
    "digest_anchor",
    "liveness",
    "resolve_anchor",
    "stamp",
]
