"""A gap goes to the task that builds it: the plan is held, not frozen.

Minesweeper run 7, discarded: the scaffold failed all three attempts on a goal
expectation -- "opening dist/index.html from disk shows a playable Minesweeper"
-- and on the Playwright half of a stack requirement, both work later tasks in
the same plan were there to do; the goal's page was written as a sentence, and
never shown. Turnstone's coordinator, when a child reports: "decide whether the
goal is met, then close it out, push a follow-up, or spawn another child to
cover the gap."
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from supervisor_harness.agents.brief import build_verifier_brief, later_tasks
from supervisor_harness.contracts import parse_tasks
from supervisor_harness.core import phases
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    AgentStatus,
    CriterionStatus,
    DoDCriterion,
    ExecutionTask,
    GoalExpectation,
    Requirement,
    RunState,
    TaskStatus,
    VerifyMethod,
)

SCAFFOLD, ENGINE, GATES = "Scaffold", "Engine", "Gates"
PLAYABLE = "opening dist/index.html from disk shows a playable game"


def _plan(fake: Any, goal: list[dict[str, Any]]) -> dict[str, Any]:
    """Three tasks in a line -- the scaffold, the engine after it, the gates after both."""
    data = copy.deepcopy(fake._synthesis(None))
    base = data["tasks"][0]
    data["tasks"] = [
        {**base, "title": SCAFFOLD, "depends_on": [], "scope_paths": ["package.json"]},
        {**base, "title": ENGINE, "depends_on": [SCAFFOLD], "scope_paths": ["src/e.ts"]},
        {**base, "title": GATES, "depends_on": [ENGINE], "scope_paths": ["vite.config.ts"]},
    ]
    data["goal"] = goal
    return data


def _expect(statement: str, by: str, whole: bool, look: str = "") -> dict[str, Any]:
    return {"statement": statement, "delivered_by": by, "look": look, "build": "",
            "whole": whole}


# -- at planning: a goal of the whole result comes last, and its page is a path -------------


def test_a_whole_result_expectation_on_an_early_task_goes_back(fake: Any) -> None:
    data = _plan(fake, [_expect(PLAYABLE, SCAFFOLD, whole=True),
                        _expect("the board is a grid", ENGINE, whole=False)])
    (line,) = phases.goal_problems(data, parse_tasks(data, "run_X"))
    assert line.startswith(f"goal expectation {PLAYABLE!r} holds only for the whole result, "
                           f"but {SCAFFOLD!r} comes before {ENGINE!r}, {GATES!r}")

    data = _plan(fake, [_expect(PLAYABLE, GATES, whole=True)])
    assert phases.goal_problems(data, parse_tasks(data, "run_X")) == [], (
        "the gates come after every part, the scaffold through the engine")


def test_with_a_ledger_the_parts_are_the_tasks_that_build_requirements(fake: Any) -> None:
    data = _plan(fake, [_expect(PLAYABLE, ENGINE, whole=True)])
    tasks = parse_tasks(data, "run_X")
    tasks[0].requirements, tasks[1].requirements = ["R1"], ["R2"]
    ledger = [Requirement(id="R1", quote="q"), Requirement(id="R2", quote="q")]
    assert phases.goal_problems(data, tasks, ledger) == [], (
        "the gates build no part, so the engine, last of the builders, is late enough")


@pytest.mark.parametrize(("look", "ok"), [
    ("dist/index.html", True), ("file:dist/index.html", True), ("", True),
    ("dist/index.html (open via file:// after npm run build)", False),
    ("open dist/index.html", False),
])
def test_a_page_to_look_at_is_a_path(fake: Any, look: str, ok: bool) -> None:
    data = _plan(fake, [_expect("the board is a grid", GATES, whole=False, look=look)])
    lines = phases.goal_problems(data, parse_tasks(data, "run_X"))
    assert (lines == []) is ok
    if not ok:
        assert f"look {look!r} is not a path" in lines[0]


def test_what_the_send_back_left_early_is_moved_to_the_furthest_task(fake: Any) -> None:
    data = _plan(fake, [_expect(PLAYABLE, SCAFFOLD, whole=True)])
    tasks = parse_tasks(data, "run_X")
    phases.resolve_dependencies(tasks)

    (expectation,), lines = phases.attach_goal(tasks, data, "Build a game")

    scaffold, _, gates = tasks
    assert expectation.whole and expectation.task_id == gates.id
    assert any(c.goal_id == expectation.id for c in gates.dod)
    assert not any(c.goal_id for c in scaffold.dod), "not judged on the scaffold"
    assert lines == [f"goal expectation {PLAYABLE!r} holds only for the whole result: "
                     f"moved from {SCAFFOLD!r} to {GATES!r}"]


# -- at verification: a gap a later task builds moves there ---------------------------------


def _line(supervisor: Supervisor, run_id: str = "run_L") -> tuple[Any, AgentSpec, list[
        ExecutionTask]]:
    scaffold = ExecutionTask(title=SCAFFOLD, status=TaskStatus.IN_PROGRESS, attempts=1)
    engine = ExecutionTask(title=ENGINE, depends_on=[scaffold.id], status=TaskStatus.APPROVED)
    gates = ExecutionTask(title=GATES, depends_on=[engine.id], status=TaskStatus.APPROVED)
    sibling = ExecutionTask(title="Docs", status=TaskStatus.APPROVED)
    goal = GoalExpectation(statement=PLAYABLE, task_id=scaffold.id, whole=True)
    scaffold.dod = [
        DoDCriterion(statement="npm run build exits 0", method=VerifyMethod.COMMAND,
                     command="npm run build", status=CriterionStatus.PASS,
                     verified_by="harness", evidence="exit 0"),
        DoDCriterion(statement="Delivers R1 as the request states them",
                     method=VerifyMethod.REVIEW, rubric="R1: ... Playwright ..."),
        DoDCriterion(statement=f"For whoever asked: {PLAYABLE}", method=VerifyMethod.REVIEW,
                     goal_id=goal.id),
        DoDCriterion(statement="The lint passes", method=VerifyMethod.COMMAND,
                     command="npm run lint"),
    ]
    verifier = AgentSpec(id="agt_v", kind=AgentKind.VERIFICATION, task_id=scaffold.id,
                         status=AgentStatus.RUNNING)
    state = RunState(id=run_id, prompt="Build a game", goal=[goal],
                     tasks={t.id: t for t in (scaffold, engine, gates, sibling)},
                     agents={verifier.id: verifier})
    return supervisor.store.create(state), verifier, [scaffold, engine, gates, sibling]


def _verdict(*rows: tuple[DoDCriterion, str, str]) -> dict[str, Any]:
    return {"results": [{"criterion_id": c.id, "status": status, "evidence": "e",
                         **({"belongs_to": owner} if owner else {})}
                        for c, status, owner in rows]}


def test_a_verdict_can_name_the_later_task_that_builds_the_part() -> None:
    from supervisor_harness.contracts import VERIFICATION_SCHEMA
    from supervisor_harness.core.conversation import VERDICT_TOOL

    for schema in (VERIFICATION_SCHEMA, VERDICT_TOOL["parameters"]):
        item = schema["properties"]["results"]["items"]
        assert "belongs_to" in item["properties"] and "belongs_to" not in item["required"]


def test_later_tasks_are_those_that_depend_on_it_through_any_chain(
    supervisor: Supervisor,
) -> None:
    session, _, (scaffold, engine, gates, _) = _line(supervisor)
    assert later_tasks(session.state, scaffold) == [engine, gates]
    assert later_tasks(session.state, gates) == []
    brief = build_verifier_brief(session.state, scaffold)
    assert "## Later in the plan" in brief and f"- {GATES}:" in brief
    assert "- Docs:" not in brief, "beside it, not after it"
    assert "## Later in the plan" not in build_verifier_brief(session.state, gates)


async def test_a_check_a_later_task_builds_moves_there(supervisor: Supervisor) -> None:
    session, verifier, (scaffold, engine, gates, _) = _line(supervisor)
    _, playwright, playable, lint = scaffold.dod
    playwright.status, playwright.evidence = CriterionStatus.FAIL, "failed on attempt 1"

    await supervisor._report_verification(session, verifier, _verdict(
        (playwright, "fail", ENGINE), (playable, "fail", GATES), (lint, "pass", "")))

    assert [c.statement for c in scaffold.dod] == ["npm run build exits 0", "The lint passes"]
    assert scaffold.status is TaskStatus.VERIFIED, "no attempt spent on another task's part"
    (moved,) = [c for c in engine.dod if c.statement == playwright.statement]
    assert moved.status is CriterionStatus.UNVERIFIED and moved.id != playwright.id
    assert moved.evidence == "", "judged afresh where it lands, not carrying this verdict"
    (goal_check,) = [c for c in gates.dod if c.goal_id]
    assert goal_check.goal_id == playable.goal_id
    assert session.state.goal[0].task_id == gates.id, "the expectation moved with its check"
    notes = [n.text for n in session.state.notes if n.text.startswith("a check moved")]
    assert notes == [f"a check moved from {SCAFFOLD!r} to {ENGINE!r}, which builds the part "
                     "it failed on",
                     f"a check moved from {SCAFFOLD!r} to {GATES!r}, which builds the part "
                     "it failed on"]
    (supervisor.store.runs_dir / "run_L" / "state.json").unlink()
    replayed = supervisor.store.load_state("run_L")
    assert len(replayed.tasks[gates.id].dod) == 1 and replayed.goal[0].task_id == gates.id


@pytest.mark.parametrize("owner", ["Docs", "Nowhere", SCAFFOLD, ""])
async def test_a_failure_stays_where_it_is_unless_a_later_task_builds_it(
    supervisor: Supervisor, owner: str,
) -> None:
    """A task beside it, one not in the plan, itself, or no name: the failure stands."""
    session, verifier, (scaffold, *_rest) = _line(supervisor, f"run_{owner or 'none'}")
    _, playwright, playable, lint = scaffold.dod

    await supervisor._report_verification(session, verifier, _verdict(
        (playwright, "fail", owner), (playable, "pass", ""), (lint, "pass", "")))

    assert playwright in scaffold.dod and playwright.status is CriterionStatus.FAIL
    assert scaffold.status is TaskStatus.FAILED


async def test_a_mechanical_result_or_a_settled_task_keeps_the_failure(
    supervisor: Supervisor,
) -> None:
    session, verifier, (scaffold, engine, gates, _) = _line(supervisor)
    _, playwright, playable, lint = scaffold.dod
    gates.status = TaskStatus.VERIFIED

    await supervisor._report_verification(session, verifier, _verdict(
        (lint, "fail", ENGINE), (playable, "fail", GATES), (playwright, "pass", "")))

    assert lint.status is CriterionStatus.FAIL, "a command's result is a fact about the tree"
    assert playable.status is CriterionStatus.FAIL, "a task already settled takes nothing new"
    assert not any(c.statement == lint.statement for c in engine.dod)
