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

from ..models import Scope, ScopeEnvelope
from .paths import NOTHING, globs_within, narrow_globs


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
        if set(narrowed) != set(paths):
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
                f"forbidden paths inherited from the {ceiling.label}: {render(added)}"
            )

    return replace(scope, paths=paths, forbidden_paths=forbidden), notes


def render(patterns: list[str]) -> str:
    """A pattern list as the run should say it out loud."""
    if not patterns:
        return "the whole workspace"
    if list(patterns) == [NOTHING]:
        return "no path at all"
    return ", ".join(f"`{p}`" for p in patterns)
