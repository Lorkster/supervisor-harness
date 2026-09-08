"""Keeping the lessons library worth reading.

A lesson is a correction from an earlier run, fed back into the briefs of later
ones. The library that holds them was append-mostly: `add_lesson` merged an
exact repeat and otherwise appended, `occurrences` only ever rose, `confidence`
only ever rose, and nothing ever decided that something learned once had stopped
being true. With `max_lessons_in_brief` at six, *which* six an agent gets was
already arbitrary and got more arbitrary with every run.

## The shape, and where it comes from

NOOA's memory subsystem runs consolidation as an **ordered deterministic
pipeline** -- clean, then abstract, then re-score, then forget -- with the
generative step optional and skipped when no reasoner is supplied, so the whole
pass stays offline-testable. It returns a report of what it changed.

That structure is taken whole, because it is already this codebase's house
style: deterministic first, a model only where one earns its place, and an
artifact saying what happened. The steps here are

1. **merge** rows that say the same thing to the same target;
2. **confirm** the ones this run re-learned, and age the ones it did not;
3. **re-score** confidence from that age;
4. **supersede** -- optional, needs a reasoner, skipped without one;
5. **archive** what has decayed below the floor or aged out.

## The clock, which is not NOOA's

They decay against *time since last access*, on an Ebbinghaus curve. That is the
wrong clock here. A lesson about a subsystem nobody has touched in three months
is not less true for having been left alone -- and the same lesson in a project
under daily churn is far more likely to be stale after a fortnight.

So the clock is **runs since the lesson was last confirmed**, counted only in
workspaces that know it. A lesson learned in project A does not decay while only
project B is running: those runs had no opportunity to re-learn it, so their
silence is not evidence about it. Ten runs in the project that *did* learn it,
none of which learned it again, is evidence.

Wall-clock age is kept as a separate outer bound (`lesson_max_age_days`), which
is what stops a library that nobody runs against from growing without limit.

## Archiving, not deleting

An archived lesson keeps its row, its origin and the reason it went. The library
is an append-only record of what the harness has learned, and a pass that
silently removed rows would make "this was never learned" and "this was learned
and later judged stale" indistinguishable -- the same confusion `is_copied_context`
and the `unreadable` fact state exist to prevent elsewhere. Hard deletion stays
where it was: `prune_lessons`, on the outer age bound.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from ..config import Policy
from ..ids import now_iso
from ..models import Lesson

#: What a reasoner is handed and what it must return: a cluster of lessons
#: sharing a target, and the ids it judges superseded, each with a reason. A
#: plain callable rather than a provider, so the pipeline stays testable without
#: one and the caller decides what a "model" means.
Reasoner = Callable[[Sequence[Lesson]], dict[str, str]]

_PUNCTUATION = re.compile(r"[.!?,;:\s]+$")
_WHITESPACE = re.compile(r"\s+")


def normalise_statement(raw: str) -> str:
    """A lesson's statement in the form two of them are compared on.

    Case, internal whitespace and trailing punctuation. Nothing fuzzier, and
    deliberately: `blackboard.normalise_fact_key` refuses a similarity merge
    because merging two things that were never the same silently destroys a
    distinction, while leaving two spellings apart is merely untidy and is
    visible. A lesson is a sentence rather than a key, so the temptation is
    stronger and the argument is identical.

    The consequence is honest and worth stating: "Do not X" and "Don't X" stay
    two rows. Deduplicating those is the reasoner's job, where a judgement is
    being made by something that can make one.
    """
    collapsed = _WHITESPACE.sub(" ", str(raw).strip().lower())
    return _PUNCTUATION.sub("", collapsed)


@dataclass
class ConsolidationReport:
    """What one pass changed, for the log and for `supervisor lessons`."""

    merged: int = 0
    confirmed: int = 0
    aged: int = 0
    rescored: int = 0
    superseded: int = 0
    archived: int = 0
    #: Ordered, one line per thing done, so a surprising library can be
    #: explained rather than guessed at.
    ops: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.ops)

    def summary(self) -> str:
        counts = {
            "merged": self.merged, "confirmed": self.confirmed, "aged": self.aged,
            "rescored": self.rescored, "superseded": self.superseded,
            "archived": self.archived,
        }
        live = [f"{name} {count}" for name, count in counts.items() if count]
        return ", ".join(live) or "nothing to do"

    def to_payload(self) -> dict[str, object]:
        return {
            "merged": self.merged, "confirmed": self.confirmed, "aged": self.aged,
            "rescored": self.rescored, "superseded": self.superseded,
            "archived": self.archived, "ops": list(self.ops),
        }


def _merge(lessons: list[Lesson], report: ConsolidationReport) -> list[Lesson]:
    """Fold rows saying the same thing to the same target into the first of them.

    `add_lesson` already merges an exact repeat as it arrives. This catches what
    that cannot: two runs recording the same lesson with different trailing
    punctuation or spacing, and rows that predate a normalisation change.
    """
    canonical: dict[tuple[str, str], Lesson] = {}
    out: list[Lesson] = []
    for lesson in lessons:
        key = (lesson.target.lower(), normalise_statement(lesson.statement))
        prior = canonical.get(key)
        if prior is None or lesson.archived:
            # An archived row is never a merge target and never absorbs one: it
            # is a record of a judgement, and folding live experience into it
            # would resurrect the judgement as though it had been re-made.
            if prior is None and not lesson.archived:
                canonical[key] = lesson
            out.append(lesson)
            continue
        prior.occurrences += lesson.occurrences
        prior.confidence = max(prior.confidence, lesson.confidence)
        prior.runs_since_confirmed = min(prior.runs_since_confirmed,
                                         lesson.runs_since_confirmed)
        for origin in [lesson.workspace, *lesson.also_seen_in]:
            if origin and not prior.learned_in(origin):
                prior.also_seen_in.append(origin)
        prior.updated_at = now_iso()
        report.merged += 1
        report.ops.append(f"merged a repeat of {lesson.statement[:60]!r} into {prior.id}")
    return out


def _age(
    lessons: list[Lesson],
    workspace: str,
    confirmed: set[str],
    report: ConsolidationReport,
) -> None:
    """Reset the clock on what this run re-learned; advance it on what it did not.

    Only for lessons this workspace knows. A run in project B is not evidence
    about a lesson learned in project A -- it had no opportunity to re-learn it,
    so counting its silence would decay a lesson for being ignored by somewhere
    that never had it.
    """
    for lesson in lessons:
        if lesson.archived:
            continue
        if lesson.id in confirmed:
            lesson.runs_since_confirmed = 0
            report.confirmed += 1
            continue
        if workspace and lesson.learned_in(workspace):
            lesson.runs_since_confirmed += 1
            report.aged += 1


def _rescore(lessons: list[Lesson], policy: Policy, report: ConsolidationReport) -> None:
    """Confidence falls once a lesson has gone unconfirmed for long enough.

    Nothing happens inside the grace period: a lesson is not weaker for having
    been learned in the previous run and not the current one, and decaying from
    the first run would make every lesson's rank a function of how recently it
    happened to fire rather than of how well it holds.
    """
    grace = max(0, policy.lesson_decay_after_runs)
    for lesson in lessons:
        if lesson.archived:
            continue
        overdue = lesson.runs_since_confirmed - grace
        if overdue <= 0:
            continue
        before = lesson.confidence
        lesson.confidence = round(
            max(0.0, lesson.confidence - overdue * policy.lesson_decay_per_run), 4
        )
        if lesson.confidence != before:
            report.rescored += 1
            report.ops.append(
                f"{lesson.id} decayed {before:.2f} -> {lesson.confidence:.2f} "
                f"after {lesson.runs_since_confirmed} run(s) unconfirmed"
            )


def _supersede(
    lessons: list[Lesson], reasoner: Reasoner | None, report: ConsolidationReport
) -> None:
    """Retire lessons a later one contradicts. Skipped entirely without a reasoner.

    Contradiction is not mechanically detectable -- two sentences disagreeing
    about how to scope a brief look exactly like two sentences about different
    things -- so this is the one step that needs a judgement, and the pipeline
    is built so that it is the one step that can be absent. Everything above
    runs, and is tested, with no model anywhere near it.

    A reasoner that raises is treated as one that was not supplied: the library
    is better slightly overgrown than half-consolidated by a pass that failed
    partway.
    """
    if reasoner is None:
        return
    live = [le for le in lessons if not le.archived]
    by_target: dict[str, list[Lesson]] = {}
    for lesson in live:
        by_target.setdefault(lesson.target.lower(), []).append(lesson)

    for cluster in by_target.values():
        if len(cluster) < 2:
            continue
        try:
            verdicts = reasoner(cluster)
        except Exception:  # noqa: BLE001 - a failed judgement is not a failed pass
            report.ops.append("supersede step skipped: the reasoner raised")
            return
        known = {le.id: le for le in cluster}
        for lesson_id, reason in (verdicts or {}).items():
            target = known.get(str(lesson_id))
            if target is None or target.archived:
                continue
            _archive(target, f"superseded: {reason}", report)
            report.superseded += 1


def _archive(lesson: Lesson, reason: str, report: ConsolidationReport) -> None:
    lesson.archived = True
    lesson.archived_at = now_iso()
    lesson.archived_reason = reason
    lesson.updated_at = lesson.archived_at
    report.archived += 1
    report.ops.append(f"archived {lesson.id}: {reason}")


def _retire(lessons: list[Lesson], policy: Policy, report: ConsolidationReport) -> None:
    """Tombstone what has decayed past the floor.

    Archived rather than deleted, and the row keeps why. "Never learned" and
    "learned, then judged stale" are different facts about a library, and a pass
    that removed the row would leave a reader unable to tell them apart -- or,
    worse, leave the next run free to learn it again with no record that it had
    already been retired once.
    """
    for lesson in lessons:
        if lesson.archived or lesson.confidence > policy.lesson_confidence_floor:
            continue
        _archive(
            lesson,
            f"confidence fell to {lesson.confidence:.2f} after "
            f"{lesson.runs_since_confirmed} run(s) without being re-learned",
            report,
        )


def consolidate(
    lessons: list[Lesson],
    *,
    policy: Policy,
    workspace: str = "",
    confirmed: Iterable[str] = (),
    reasoner: Reasoner | None = None,
) -> tuple[list[Lesson], ConsolidationReport]:
    """Run the pipeline over a library and say what changed.

    ``confirmed`` is the ids this run re-learned, which is what resets their
    clock. Pure apart from the timestamps it stamps: it mutates the lessons it
    is given and returns them, so a caller holding a lock can write the result
    back without a second read.
    """
    report = ConsolidationReport()
    kept = _merge(list(lessons), report)
    _age(kept, workspace, {str(c) for c in confirmed}, report)
    _rescore(kept, policy, report)
    _supersede(kept, reasoner, report)
    _retire(kept, policy, report)
    return kept, report


__all__ = [
    "ConsolidationReport",
    "Reasoner",
    "consolidate",
    "normalise_statement",
]
