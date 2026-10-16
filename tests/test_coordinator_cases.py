"""Batch O's replay cases: a recorded moment, as a coordinated run would have had it, judged.

A case is folded from a run's log (`tools/extract_coordinator_cases.py`), put in
the shape the coordinator would have seen -- a stop not yet a question to the
owner, a held check failed, an old routing taken back out -- and the
coordinator's picks are judged against what a person labelled right.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from supervisor_harness.config import HarnessConfig
from supervisor_harness.core import coordinator
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.evals.cases import parse_case
from supervisor_harness.evals.checks import judge
from supervisor_harness.evals.roles import Variant, run_role
from supervisor_harness.models import (
    CriterionStatus,
    DoDCriterion,
    Escalation,
    ExecutionTask,
    GoalExpectation,
    RunState,
    TaskStatus,
    VerifyMethod,
)
from supervisor_harness.providers.router import ModelRouter
from supervisor_harness.serde import to_jsonable
from supervisor_harness.store.events import EventType

from .test_coordinator import Coordinator, _engine_and_tests

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "extract_coordinator_cases", ROOT / "tools" / "extract_coordinator_cases.py")
assert _spec and _spec.loader
extract = importlib.util.module_from_spec(_spec)
sys.modules["extract_coordinator_cases"] = extract
_spec.loader.exec_module(extract)


def _case(state: RunState, checks: list[dict[str, object]]) -> dict[str, object]:
    return {"id": "c-coordinator", "role": "coordinator",
            "fixture": {"kind": "files", "files": {}}, "request": "Build Minesweeper.",
            "input": {"state": to_jsonable(state)}, "checks": checks}


async def test_the_coordinator_role_is_judged_by_its_picks(tmp_path: Path,
                                                           config: HarnessConfig) -> None:
    scaffold, engine, e2e = _engine_and_tests()
    state = RunState(prompt="Build Minesweeper.", tasks={t.id: t for t in (scaffold, engine, e2e)})
    move = {"do": "move_check", "task": "Engine", "to_task": "Browser tests"}
    case = parse_case(_case(state, [
        {"check": "answered"}, {"check": "picked_for_each"},
        {"check": "picks", "expect": [[move]], "forbid": [{"do": "send_back", "task": "Engine"}]}]))
    router = ModelRouter(config, host_name="eval")
    router.register("fake", Coordinator({"actions": [{"do": "move_check", "task": "T2",
                                                      "criterion": "C1", "to_task": "T3"}]}))

    out = await run_role(case, tmp_path, tmp_path / "store", Variant(), base=config,
                         router=router)

    assert out.picks == [{"do": "move_check", "task": "Engine", "to_task": "Browser tests",
                          "criterion": "the project type-checks", "message": "",
                          "by": "coordinator"}], out.error
    assert all(r["passed"] for r in judge(case, out, tmp_path).values())

    router.register("fake", Coordinator({"actions": [{"do": "send_back", "task": "T2"}]}))
    out = await run_role(case, tmp_path, tmp_path / "store2", Variant(), base=config,
                         router=router)
    result = judge(case, out, tmp_path)["picks"]
    assert not result["passed"] and "forbidden: ['send_back Engine']" in result["detail"]


async def test_a_moment_is_put_back_as_a_coordinated_run_had_it(supervisor: Supervisor) -> None:
    scaffold, engine, e2e = _engine_and_tests()
    page = ExecutionTask(title="Page", status=TaskStatus.IN_PROGRESS, attempts=1)
    engine.status = TaskStatus.AWAITING_VERIFICATION
    engine.dod[-1].held_for = [e2e.id]          # held by the old rules, for e2e's file
    session = supervisor.store.create(RunState(id="run_Moment", prompt="p",
                                               tasks={t.id: t for t in (scaffold, engine, e2e,
                                                                        page)}))
    for task in session.state.tasks.values():
        await session.aemit(EventType.TASK_PROPOSED, {"task": to_jsonable(task)})
    # Before the window: a question to the owner that stays one.
    early = Escalation(run_id=session.state.id, task_id=scaffold.id, detail="a credential")
    await session.aemit(EventType.ESCALATION_RAISED, {"escalation": to_jsonable(early)})
    since = session.state.last_seq
    # In the window: a check the old rules held, a stop they sent to the owner, and a
    # finding they routed.
    await session.aemit(EventType.TASK_UPDATED, {"task": to_jsonable(engine)})
    page.status = TaskStatus.BLOCKED
    await session.aemit(EventType.TASK_UPDATED, {"task": to_jsonable(page)})
    stop = Escalation(run_id=session.state.id, task_id=page.id, detail="needs the unit tests")
    await session.aemit(EventType.ESCALATION_RAISED, {"escalation": to_jsonable(stop)})
    routed = GoalExpectation(statement="the board works", task_id=scaffold.id,
                             source="agent agt_x")
    await session.aemit(EventType.GOAL_SET, {"goal": to_jsonable([routed])})
    scaffold.dod.append(DoDCriterion(statement="for whoever asked: the board works",
                                     method=VerifyMethod.REVIEW, goal_id=routed.id))
    scaffold.status = TaskStatus.APPROVED       # reopened by the routing
    await session.aemit(EventType.TASK_UPDATED, {"task": to_jsonable(scaffold)})

    events = extract._events(supervisor.store.run_dir(session.state.id) / "events.jsonl")
    state = extract.at_the_moment(events, since, session.state.last_seq,
                                  {"Scaffold": "verified"})

    assert set(state.escalations) == {early.id}, "the stop is not yet a question"
    engine = state.tasks[engine.id]
    assert engine.status is TaskStatus.FAILED
    assert engine.dod[-1].status is CriterionStatus.FAIL and not engine.dod[-1].held_for
    assert state.goal == [] and all(not c.goal_id for c in state.tasks[scaffold.id].dod)
    assert state.tasks[scaffold.id].status is TaskStatus.VERIFIED
    assert {m.kind for m in coordinator.open_matters(state)} == {"stopped", "failed"}

    reduced = extract.reduced(state)
    assert set(reduced) >= {"tasks", "agents", "turns", "messages", "coordination"}
    assert reduced["notes"] == [] and reduced["briefs"] == {}
