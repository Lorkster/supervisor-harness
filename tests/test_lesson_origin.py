"""Where a lesson came from, and whether anything acts on it.

The lessons library is deliberately cross-workspace: a lesson learned in one
project is worth having in the next, and that is the feature. What was missing
was any way to tell the two apart at the point of use. `Lesson.workspace` had
been recorded since batch 8 and the ranking that uses it had been written and
tested -- but no production caller passed the argument that turns it on, and no
brief ever said where a lesson came from.
"""

from __future__ import annotations

from pathlib import Path

from supervisor_harness.agents.brief import build_analysis_brief
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    Lesson,
    RunMode,
    RunState,
)

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"


def _brief_with(lessons: list[Lesson], workspace: str) -> str:
    return build_analysis_brief(
        RunState(prompt=PROMPT, workspace=workspace),
        AgentSpec(id="agt_1", role="security", kind=AgentKind.ANALYSIS, title="Security"),
        None,
        [],
        {"type": "object", "properties": {}},
        lessons=lessons,
    )


def test_the_brief_says_where_each_lesson_was_learned() -> None:
    """Untagged, a borrowed convention reads as a local one.

    An agent given "earlier runs went wrong in these specific ways" has no way
    to tell a rule drawn from this repository from one drawn from a stranger's
    -- which is exactly the judgement it has to make when the two disagree.
    """
    brief = _brief_with(
        [
            Lesson(statement="Local convention", workspace="/work/this-repo", target="*"),
            Lesson(statement="Borrowed convention", workspace="/work/other-repo", target="*"),
        ],
        workspace="/work/this-repo",
    )

    assert "Local convention" in brief and "Borrowed convention" in brief
    assert "(learned here" in brief
    assert "(learned in other-repo" in brief
    # The origin is named by its directory, not by someone's disk layout.
    assert "/work/other-repo" not in brief
    # And the agent is told what to do when the two disagree.
    assert "this workspace wins" in brief


def test_a_lesson_with_no_recorded_origin_still_reads_honestly() -> None:
    """Rows written before the origin was recorded must not claim to be local."""
    brief = _brief_with([Lesson(statement="Old row", target="*")], workspace="/work/this-repo")
    assert "learned in an earlier run" in brief
    assert "learned here" not in brief


async def test_the_brief_an_agent_actually_gets_carries_the_origin(
    supervisor: Supervisor, workspace: Path,
) -> None:
    """The wiring, not the helper: `_lessons_block` is only reached one way."""
    supervisor.store.add_lesson(
        Lesson(
            run_id="earlier",
            workspace="/somewhere/else",
            statement="Name the concrete dependency to reuse",
            how_to_apply="Say which module and symbol.",
            target="*",
        )
    )

    response = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    state = supervisor.store.load_state(response.run_id)
    briefs = [b for b in state.briefs.values() if "Name the concrete dependency" in b]

    assert briefs, "no brief carried the lesson"
    assert "learned in else" in briefs[0]


async def test_lessons_reaching_a_brief_are_ranked_and_aged_by_policy(
    supervisor: Supervisor, config, workspace: Path,
) -> None:
    """Both keyword arguments were left at their defaults by every caller.

    `lessons_for` ranks local above borrowed and drops anything past an age cap,
    and `policy.lesson_max_age_days` is supposed to set that cap. Neither ran:
    the brief builder called `lessons_for(targets, limit)` and nothing else, so
    a workspace configuring the cap changed nothing and borrowed experience
    sorted level with local.
    """
    config.policy.lesson_max_age_days = 30
    here = str(workspace)

    supervisor.store.add_lesson(
        Lesson(run_id="a", workspace="/elsewhere", statement="BORROWED lesson", target="*")
    )
    supervisor.store.add_lesson(
        Lesson(run_id="b", workspace=here, statement="LOCAL lesson", target="*")
    )
    supervisor.store.add_lesson(
        Lesson(run_id="c", workspace=here, statement="STALE lesson", target="*",
               created_at="2020-01-01T00:00:00Z", updated_at="2020-01-01T00:00:00Z")
    )

    response = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    state = supervisor.store.load_state(response.run_id)
    carried = [b for b in state.briefs.values() if "lesson" in b]
    assert carried, "no brief carried any lesson"
    brief = carried[0]

    # The cap the policy sets is the cap that applies.
    assert "STALE lesson" not in brief
    # And local leads borrowed.
    assert brief.index("LOCAL lesson") < brief.index("BORROWED lesson")
