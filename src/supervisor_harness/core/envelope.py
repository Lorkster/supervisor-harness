"""The run's scope envelope, and the attenuation of every scope to it.

`core/tools.py` fences an agent's writes and commands against its `Scope`, and
an unconditional floor holds whatever a scope says. What neither asked is where
the scope came from. Every `Scope` in a run is proposed by a model -- the
planner's for an analysis lens, the synthesis model's for an execution task --
and until this module existed no two of them were ever compared. The synthesis
model drew the fence its own tasks then ran inside.

This is the same principle batch 7 settled for configuration, applied to the
other half: the subject of a judgement may not set the terms of it. There the
rule was that a repository may tune how much work the harness does, not how
sceptical it is about work done on it. Here it is that a model may narrow what
a run may touch, never widen it.

Two functions carry that:

* :func:`establish` builds the run's envelope. Configuration is the floor, the
  plan may narrow it, and nothing may widen it.
* :func:`attenuate` narrows one agent's scope to every ceiling above it, and
  says which ceiling bit. It narrows rather than refuses, because a model
  proposing too much is ordinary and losing the task to it is not.

## Whether the user's approval may widen the envelope

It may not, and this is the recorded answer rather than an omission.

A `scope_paths` modification at approval is clamped like any other scope, and
the clamp is recorded on the run for the user to see. The reason is that a
per-task approval is a decision about *that task*: if it could move a
run-level bound, the bound would only ever be as strong as the most permissive
task anyone approved, which is not a bound. It would also be invisible -- the
envelope would have to be reconstructed afterwards from the union of every
per-task edit, which is exactly the "authority that was never recorded cannot
be audited" problem the envelope exists to close.

The cost is real and is not hidden: if the plan draws the envelope too
narrowly, every task is clamped and the user cannot widen it from the approval
prompt. What they can do is start the run with a wider envelope -- in
configuration, or by having the plan say so -- which is visible from the
beginning and applies to everything, rather than being assembled from edits.
The alternative, a distinct `widen_envelope` act at approval, is defensible and
was rejected on that ground: it buys back a restart at the cost of making the
run's grant something that changes shape midway through the run.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..ids import age_days, older_than
from ..models import Scope, ScopeEnvelope
from .paths import NOTHING, globs_within, matches_any, minimal_globs, narrow_globs, pattern_within


@dataclass(frozen=True)
class Ceiling:
    """One bound on a scope, and what to call it in the run's own words."""

    label: str
    paths: list[str]
    forbidden_paths: list[str]

    @classmethod
    def of(cls, label: str, source: Scope | ScopeEnvelope | None) -> Ceiling | None:
        if source is None:
            return None
        return cls(label, list(source.paths), list(source.forbidden_paths))


def effective(envelope: ScopeEnvelope | None) -> ScopeEnvelope:
    """The envelope to enforce, for a run that may predate envelopes at all.

    A run with no envelope is not treated as a run entitled to nothing: it is
    treated as it was when it was written, bounded by the floor in
    `core/tools.py` and nothing else. Refusing everything instead would make
    resuming an older run impossible, which is a worse answer than the one that
    build already gave.
    """
    return envelope or ScopeEnvelope(source="none established")


def establish(
    configured: ScopeEnvelope | None = None,
    proposed_paths: list[str] | None = None,
    proposed_forbidden: list[str] | None = None,
    source: str = "",
) -> tuple[ScopeEnvelope, list[str]]:
    """The run's envelope, plus the lines explaining anything that was refused.

    ``configured`` is the floor and is trusted; ``proposed_*`` comes from the
    planning model and is not. Allowed paths intersect and forbidden paths
    accumulate -- both are the narrowing direction -- so a proposal can only
    ever reduce what the run may touch. A proposal that names something outside
    the configured envelope does not lose the whole plan: the part that is
    inside stands, and the part that is not is reported.
    """
    base = effective(configured) if configured is not None else ScopeEnvelope()
    proposed = [str(p) for p in (proposed_paths or []) if str(p).strip()]
    forbidden = list(base.forbidden_paths)
    for pattern in (str(p) for p in (proposed_forbidden or [])):
        if pattern.strip() and pattern not in forbidden:
            forbidden.append(pattern)

    notes: list[str] = []
    if not proposed:
        return ScopeEnvelope(
            paths=list(base.paths), forbidden_paths=forbidden,
            source=source or base.source,
        ), notes

    paths = narrow_globs(proposed, base.paths)
    if not globs_within(proposed, base.paths):
        notes.append(
            f"the plan proposed an envelope of {render(proposed)}, which the "
            f"configured envelope {render(base.paths)} does not contain; the run "
            f"is bounded by {render(paths)}"
        )
    return ScopeEnvelope(paths=paths, forbidden_paths=forbidden, source=source), notes


def widen_within(
    envelope: ScopeEnvelope, ceiling: Ceiling, wanted: list[str]
) -> tuple[ScopeEnvelope, list[str], list[str]]:
    """The run envelope widened by those of ``wanted`` the owner's own grant covers.

    Returns the envelope, the paths a task may now have -- already inside the
    envelope, or added to it -- and the paths refused.

    The plan narrows the owner's grant to what it expects the work to need,
    and it guesses short: in four go-live runs a Playwright task was sent to
    the owner because the plan's envelope left out `e2e/`, though the owner had
    granted the whole workspace. Only the owner widens -- beyond their grant.
    Within it, the plan's narrowing was a model's guess about itself, and a
    task that needs more of what the owner granted gets it. A path the grant
    does not cover, or that the grant or the plan forbids, is refused: those
    still go to the owner.
    """
    allowed = list(envelope.paths)
    granted: list[str] = []
    refused: list[str] = []
    for path in dict.fromkeys(p for p in wanted if p.strip()):
        if (matches_any(path, ceiling.forbidden_paths)
                or matches_any(path, envelope.forbidden_paths)
                or (ceiling.paths and not globs_within([path], ceiling.paths))):
            refused.append(path)
            continue
        if allowed and not globs_within([path], allowed):
            allowed.append(path)
        granted.append(path)
    return replace(envelope, paths=allowed), granted, refused


def attenuate(scope: Scope, ceilings: list[Ceiling | None]) -> tuple[Scope, list[str]]:
    """``scope`` narrowed to every ceiling above it, and what each one took.

    Returns a new :class:`Scope`; the input is not mutated. The notes are the
    point as much as the narrowing is -- an agent quietly given less authority
    than its brief describes is a confusing agent, and a narrowing nobody
    recorded is the thing this module exists to stop happening.
    """
    paths = list(scope.paths)
    forbidden = list(scope.forbidden_paths)
    notes: list[str] = []

    for ceiling in ceilings:
        if ceiling is None:
            continue
        narrowed = narrow_globs(paths, ceiling.paths)
        # Compared by what they cover: baseline 2 recorded `src/`, `e2e/` as
        # narrowed to `src/`, `e2e/offline.spec.ts`, `e2e/` -- nothing taken, the
        # envelope having named a file under `e2e/` as well -- and held the task.
        if set(minimal_globs(narrowed)) != set(minimal_globs(paths)):
            # Two different facts, and the run should not report them in the
            # same words. A scope that proposed paths has had some taken away;
            # a scope that proposed none was never narrowed at all, it was
            # handed its ceiling as its fence -- which is the more important of
            # the two, since an empty scope is the whole workspace.
            notes.append(
                f"scope narrowed to the {ceiling.label}: "
                f"{render(paths)} -> {render(narrowed)}"
                if paths else
                f"scope taken from the {ceiling.label}, having declared none: "
                f"{render(narrowed)}"
            )
            paths = narrowed
        added = [p for p in ceiling.forbidden_paths if p and p not in forbidden]
        if added:
            forbidden.extend(added)
            notes.append(
                f"{INHERITED} the {ceiling.label}: {render(added)}"
            )
            # The fence is the run's, and inheriting it takes nothing a task
            # asked for -- unless the task named a path wholly inside it.
            # Baseline 2: every task of a run was held for the owner because the
            # plan had forbidden README.md and the lint configs, which none of
            # them named.
            cut = [p for p in paths if any(pattern_within(p, f) for f in added)]
            if cut:
                notes.append(f"scope narrowed by the {ceiling.label}'s forbidden paths: "
                             f"{render(cut)}")

    return replace(scope, paths=paths, forbidden_paths=forbidden), notes


#: How the note recording a fence handed down begins; `what_was_taken` leaves it out.
INHERITED = "forbidden paths inherited from"


def what_was_taken(notes: list[str]) -> list[str]:
    """The notes of an `attenuate` that record something taken from the scope.

    Not the fence it inherited: that note is for the agent's brief, and read as
    a narrowing it sent a task to the owner for asking nothing beyond its run.
    """
    return [n for n in notes if not n.startswith(INHERITED)]


def stale_reason(
    envelope: ScopeEnvelope | None, created_at: str, max_age_days: int
) -> str | None:
    """Why this run's grant is too old to execute against, or ``None``.

    Extent and duration are different mechanisms, and almost everything about
    duration was already handled: `Budget` bounds an agent's turns, tokens,
    seconds and tool calls, the abandonment bounds catch a silent host agent,
    and a phase transition ends the agents of the phase it leaves. What none of
    them bounds is the *grant* -- a run resumed months later, on the same host
    and in the same directory, produces no divergence at all today and executes
    against an envelope the user approved in a context that has since moved on.

    Measured from when the envelope was granted, falling back to the run's own
    creation time for an envelope recorded before the field existed. An
    unreadable timestamp is not stale: a bug in date parsing should not be able
    to block a resume.
    """
    if max_age_days <= 0:
        return None
    stamp = (envelope.granted_at if envelope else "") or created_at
    if not older_than(stamp, max_age_days):
        return None
    age = age_days(stamp)
    days = f"{age:.0f}" if age is not None else "?"
    return (
        f"this run's scope envelope was granted {days} days ago, past the "
        f"{max_age_days}-day limit. It still describes "
        f"{render(envelope.paths if envelope else [])}, "
        "but the workspace it was drawn against has had that long to change"
    )


def render(patterns: list[str]) -> str:
    """A pattern list as the run should say it out loud."""
    if not patterns:
        return "the whole workspace"
    if list(patterns) == [NOTHING]:
        return "no path at all"
    return ", ".join(f"`{p}`" for p in patterns)
