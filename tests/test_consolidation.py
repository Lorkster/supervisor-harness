"""Keeping the lessons library worth reading.

Batch 6 of `docs/development-plan.md`. The library was append-mostly: an exact
repeat merged, everything else appended, `occurrences` and `confidence` only
ever rose, and nothing ever decided that something learned once had stopped
being true. With `max_lessons_in_brief` at six, *which* six an agent got was
already arbitrary.

The pipeline's shape is NOOA's -- ordered, deterministic, with the generative
step optional so the pass stays offline-testable. Every test here but one runs
with no model anywhere near it, which is the property being protected as much as
the behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from supervisor_harness.config import Policy, default_config
from supervisor_harness.core.consolidate import (
    ConsolidationReport,
    consolidate,
    normalise_statement,
)
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.host.detect import HostInfo
from supervisor_harness.models import Backend, Lesson, RunMode
from supervisor_harness.store.runstore import RunStore

from .conftest import FakeProvider
from .test_host_delegation import HostSimulator

HERE = "/projects/harness"
ELSEWHERE = "/projects/other"


@pytest.fixture
def policy() -> Policy:
    return Policy(
        lesson_decay_after_runs=2,
        lesson_decay_per_run=0.2,
        lesson_confidence_floor=0.15,
    )


def _lesson(statement: str = "Scope the brief to one subsystem", **kwargs: object) -> Lesson:
    fields: dict[str, object] = {
        "statement": statement,
        "target": "architecture",
        "workspace": HERE,
        "confidence": 0.6,
        "why": "a broad brief drew a broad answer",
    }
    fields.update(kwargs)
    return Lesson(**fields)  # type: ignore[arg-type]


# -- merging ----------------------------------------------------------------


def test_statements_are_compared_on_case_whitespace_and_trailing_punctuation() -> None:
    assert normalise_statement("Do not widen the scope.") == "do not widen the scope"
    assert normalise_statement("  Do  not   widen the scope  ") == "do not widen the scope"
    assert normalise_statement("DO NOT WIDEN THE SCOPE!!") == "do not widen the scope"


def test_a_rephrasing_is_deliberately_not_merged() -> None:
    """The same call `normalise_fact_key` makes, for the same reason.

    Merging two things that were never the same silently destroys a distinction
    a run may depend on; leaving two spellings apart is untidy and visible. A
    lesson is a sentence rather than a key, so the temptation is stronger and
    the argument is identical. Deduplicating these is the reasoner's job.
    """
    assert normalise_statement("Do not X") != normalise_statement("Don't X")


def test_rows_saying_the_same_thing_are_folded_into_the_first(policy: Policy) -> None:
    first = _lesson("Scope the brief to one subsystem", occurrences=2)
    second = _lesson("scope the brief to one subsystem.", occurrences=3,
                     workspace=ELSEWHERE, confidence=0.8)

    kept, report = consolidate([first, second], policy=policy)

    assert len(kept) == 1
    assert report.merged == 1
    assert kept[0].occurrences == 5
    assert kept[0].confidence == 0.8, "the stronger reading survives"
    assert kept[0].learned_in(ELSEWHERE), "and the second origin is kept"


def test_an_archived_row_neither_absorbs_nor_is_absorbed(policy: Policy) -> None:
    """It is a record of a judgement, not a place to put live experience.

    Folding a fresh lesson into a retired row would resurrect the judgement as
    though it had been re-made, which is exactly what reviving is for and what
    `add_lesson` does explicitly.
    """
    retired = _lesson(archived=True, archived_reason="decayed")
    fresh = _lesson()

    kept, _ = consolidate([retired, fresh], policy=policy)

    assert len(kept) == 2


# -- the clock --------------------------------------------------------------


def test_a_lesson_this_run_relearned_has_its_clock_reset(policy: Policy) -> None:
    lesson = _lesson(runs_since_confirmed=5)

    kept, report = consolidate(
        [lesson], policy=policy, workspace=HERE, confirmed=[lesson.id]
    )

    assert kept[0].runs_since_confirmed == 0
    assert report.confirmed == 1


def test_a_lesson_this_run_did_not_relearn_ages(policy: Policy) -> None:
    lesson = _lesson(runs_since_confirmed=1)

    kept, report = consolidate([lesson], policy=policy, workspace=HERE)

    assert kept[0].runs_since_confirmed == 2
    assert report.aged == 1


def test_a_run_elsewhere_is_not_evidence_about_a_lesson_it_never_had(
    policy: Policy,
) -> None:
    """The divergence from NOOA, and the reason for it.

    They decay against time since last access. A lesson about a subsystem
    nobody has touched in three months is not less true for having been left
    alone -- and a run in a project that never learned it had no opportunity to
    re-learn it, so its silence says nothing at all.
    """
    lesson = _lesson(workspace=HERE, runs_since_confirmed=0)

    kept, report = consolidate([lesson], policy=policy, workspace=ELSEWHERE)

    assert kept[0].runs_since_confirmed == 0
    assert report.aged == 0


def test_a_lesson_relearned_elsewhere_ages_there_too(policy: Policy) -> None:
    """"Knows it" means learned here at any point, not first."""
    lesson = _lesson(workspace=HERE, also_seen_in=[ELSEWHERE])

    kept, _ = consolidate([lesson], policy=policy, workspace=ELSEWHERE)

    assert kept[0].runs_since_confirmed == 1


# -- decay ------------------------------------------------------------------


def test_nothing_decays_inside_the_grace_period(policy: Policy) -> None:
    """A lesson is not weaker for having fired last run and not this one.

    Decaying from the first unconfirmed run would make every lesson's rank a
    function of how recently it happened to fire rather than of how well it
    holds.
    """
    lesson = _lesson(confidence=0.6, runs_since_confirmed=0)

    kept, report = consolidate([lesson], policy=policy, workspace=HERE)

    assert kept[0].runs_since_confirmed == 1
    assert kept[0].confidence == 0.6
    assert report.rescored == 0


def test_confidence_falls_once_a_lesson_is_overdue(policy: Policy) -> None:
    lesson = _lesson(confidence=0.6, runs_since_confirmed=3)

    kept, report = consolidate([lesson], policy=policy, workspace=HERE)

    # Aged to 4, two runs past the grace of 2, at 0.2 a run.
    assert kept[0].runs_since_confirmed == 4
    assert kept[0].confidence == pytest.approx(0.2)
    assert report.rescored == 1
    assert any("decayed" in op for op in report.ops)


def test_a_lesson_that_falls_through_the_floor_is_archived(policy: Policy) -> None:
    lesson = _lesson(confidence=0.3, runs_since_confirmed=4)

    kept, report = consolidate([lesson], policy=policy, workspace=HERE)

    assert kept[0].archived is True
    assert report.archived == 1
    assert "without being re-learned" in kept[0].archived_reason


def test_an_archived_lesson_keeps_its_row_and_its_reason(policy: Policy) -> None:
    """"Never learned" and "learned, then judged stale" are different facts.

    A pass that removed the row would leave a reader unable to tell them apart,
    and leave the next run free to learn it again with nothing recording that it
    had already been retired once.
    """
    lesson = _lesson(confidence=0.1)

    kept, _ = consolidate([lesson], policy=policy, workspace=HERE)

    assert len(kept) == 1
    assert kept[0].archived and kept[0].archived_at and kept[0].archived_reason


def test_an_archived_lesson_does_not_age_or_decay_further(policy: Policy) -> None:
    lesson = _lesson(archived=True, confidence=0.1, runs_since_confirmed=9)

    kept, report = consolidate([lesson], policy=policy, workspace=HERE)

    assert kept[0].runs_since_confirmed == 9
    assert report.aged == 0 and report.rescored == 0 and report.archived == 0


# -- the optional step ------------------------------------------------------


def test_the_whole_pipeline_runs_without_a_reasoner(policy: Policy) -> None:
    """The property NOOA's design protects, and the reason for the ordering.

    Contradiction is not mechanically detectable, so superseding needs a
    judgement -- and the pipeline is built so that it is the *only* step that
    needs one. Everything else runs, and is tested, with no model involved.
    """
    kept, report = consolidate(
        [_lesson(confidence=0.1), _lesson("Another thing", runs_since_confirmed=8)],
        policy=policy, workspace=HERE,
    )

    assert report.superseded == 0
    assert report.archived >= 1, "the deterministic steps still did their work"
    assert all(isinstance(le, Lesson) for le in kept)


def test_a_reasoner_can_retire_a_lesson_a_later_one_contradicts(policy: Policy) -> None:
    old = _lesson("Always widen the scope", target="architecture")
    new = _lesson("Never widen the scope", target="architecture")

    def reasoner(cluster: list[Lesson]) -> dict[str, str]:
        return {old.id: "contradicted by a later run"}

    kept, report = consolidate(
        [old, new], policy=policy, workspace=HERE, reasoner=reasoner,
    )

    assert report.superseded == 1
    retired = next(le for le in kept if le.id == old.id)
    assert retired.archived and "contradicted by a later run" in retired.archived_reason
    assert not next(le for le in kept if le.id == new.id).archived


def test_a_reasoner_that_raises_does_not_fail_the_pass(policy: Policy) -> None:
    """Better slightly overgrown than half-consolidated by a pass that died."""
    def reasoner(cluster: list[Lesson]) -> dict[str, str]:
        raise RuntimeError("the model was unreachable")

    kept, report = consolidate(
        [_lesson("A"), _lesson("B")], policy=policy, workspace=HERE, reasoner=reasoner,
    )

    assert len(kept) == 2
    assert report.superseded == 0
    assert any("reasoner raised" in op for op in report.ops)


def test_a_reasoner_is_not_asked_about_a_lone_lesson(policy: Policy) -> None:
    asked: list[int] = []

    def reasoner(cluster: list[Lesson]) -> dict[str, str]:
        asked.append(len(cluster))
        return {}

    consolidate([_lesson("A")], policy=policy, workspace=HERE, reasoner=reasoner)

    assert asked == [], "nothing can contradict a lesson with no peer"


# -- the report -------------------------------------------------------------


def test_a_pass_that_changed_nothing_says_so(policy: Policy) -> None:
    kept, report = consolidate([], policy=policy, workspace=HERE)

    assert kept == []
    assert not report
    assert report.summary() == "nothing to do"


def test_the_report_names_what_it_did(policy: Policy) -> None:
    report = ConsolidationReport(merged=1, archived=2)

    assert report.summary() == "merged 1, archived 2"
    assert report.to_payload()["archived"] == 2


# -- through the library ----------------------------------------------------


def test_a_retired_lesson_is_not_offered_to_a_brief(tmp_path: Path) -> None:
    """Which is what retiring it meant."""
    store = RunStore(tmp_path / ".supervisor")
    store.add_lesson(_lesson("Retired thing", target="architecture"))
    store.add_lesson(_lesson("Live thing", target="architecture"))

    def retire_the_first(library: list[Lesson]) -> tuple[list[Lesson], None]:
        library[0].archived = True
        return library, None

    store.update_lessons(retire_the_first)

    offered = [le.statement for le in store.lessons_for(["architecture"], workspace=HERE)]
    assert offered == ["Live thing"]
    assert len(store.lessons()) == 2, "the row is kept, only withheld"


def test_re_learning_a_retired_lesson_revives_it(tmp_path: Path) -> None:
    """The evidence changed, so the judgement should.

    A retired lesson that a later run learns again is not a duplicate to be
    dropped: it is the case for the lesson being remade, and the row keeps the
    fact that it happened.
    """
    store = RunStore(tmp_path / ".supervisor")
    stored = store.add_lesson(_lesson("Scope the brief", target="architecture"))

    def retire(library: list[Lesson]) -> tuple[list[Lesson], None]:
        library[0].archived = True
        library[0].archived_reason = "decayed to 0.10"
        library[0].runs_since_confirmed = 9
        return library, None

    store.update_lessons(retire)
    revived = store.add_lesson(_lesson("Scope the brief", target="architecture"))

    assert revived.id == stored.id
    assert revived.archived is False
    assert revived.runs_since_confirmed == 0
    assert "revived after being re-learned" in revived.archived_reason
    assert "decayed to 0.10" in revived.archived_reason, "the earlier judgement is kept"


async def test_a_run_consolidates_what_it_left_behind(
    tmp_path: Path, fake: FakeProvider
) -> None:
    """End to end: the pass runs when the library changed, not on a schedule.

    Which lessons a run re-learned is what resets their clock, and nothing
    outside the run can reconstruct that afterwards -- so consolidation belongs
    at the end of a run rather than to a cron job that could only guess.

    Driven through `HostSimulator`, which reports every packet the way a host
    does. An ad-hoc loop that only advances never reaches the improvement phase
    at all: the run fails in synthesis with its packets unreported, and the
    library is untouched for a reason that has nothing to do with consolidation.
    """
    config = default_config()
    config.backend = Backend.HOST
    config.routing = {k: "host" for k in config.routing}
    config.policy = Policy(default_max_turns=2, execution_max_turns=2,
                           max_analysis_lenses=1, lesson_decay_after_runs=0,
                           lesson_decay_per_run=0.9)
    store = RunStore(tmp_path / ".supervisor")
    stale = store.add_lesson(_lesson("A thing nobody re-learns", target="architecture",
                                     workspace=str(tmp_path), confidence=0.9))

    supervisor = Supervisor(
        workspace=tmp_path, config=config, store=store,
        host=HostInfo(name="claude-code", workspace=str(tmp_path), confidence=1.0),
    )
    started = await supervisor.start("Add rate limiting to the login endpoint",
                                     mode=RunMode.EXECUTE)
    final = await HostSimulator(supervisor, fake).drive(started)

    assert final.action == "complete", final.message
    state = store.load_state(started.run_id)
    assert state.consolidation, "the pass is on the run's record"

    after = {le.id: le for le in store.lessons()}[stale.id]
    assert after.runs_since_confirmed >= 1, "the run that did not re-learn it aged it"
    assert after.confidence < 0.9, "and it decayed for not being re-learned"
