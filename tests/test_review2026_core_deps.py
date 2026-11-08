"""Dependencies that can never be satisfied are caught early (review finding 10)."""

from __future__ import annotations

from supervisor_harness.core import coordinator, phases
from supervisor_harness.models import ExecutionTask, RunState, TaskStatus


def _task(title: str, **kw: object) -> ExecutionTask:
    return ExecutionTask(title=title, action=f"do: {title}", **kw)  # type: ignore[arg-type]


def test_a_self_reference_by_id_is_dropped_and_noted() -> None:
    task = _task("Solo")
    task.depends_on = [task.id]

    notes = phases.resolve_dependencies([task])

    assert task.depends_on == []
    assert any("self-dependency" in n for n in notes[task.id])


def test_a_cycle_is_broken_and_noted() -> None:
    a, b, c = _task("A"), _task("B"), _task("C")
    a.depends_on, b.depends_on, c.depends_on = ["B"], ["C"], ["A"]

    notes = phases.resolve_dependencies([a, b, c])

    assert a.depends_on == [b.id] and b.depends_on == [c.id]
    assert c.depends_on == [], "the edge that closed the loop"
    assert any("could never start" in n for n in notes[c.id])
    # and the plan is runnable from the end of the chain
    state = RunState(tasks={t.id: t for t in (a, b, c)})
    for t in (a, b, c):
        t.status = TaskStatus.APPROVED
    assert [t.id for t in phases.runnable_tasks(state)] == [c.id]


def test_two_waits_in_one_decision_that_name_each_other_are_not_both_taken() -> None:
    a, b = _task("Engine", status=TaskStatus.APPROVED), _task("Tests", status=TaskStatus.APPROVED)
    state = RunState(tasks={a.id: a, b.id: b})
    handles = coordinator.Handles(tasks={"T1": a.id, "T2": b.id}, checks={})
    matters = [coordinator.Matter(key="a", kind="failed", task_id=a.id),
               coordinator.Matter(key="b", kind="failed", task_id=b.id)]
    arguments = {"actions": [
        {"do": "wait", "task": "T1", "to_task": "T2"},
        {"do": "wait", "task": "T2", "to_task": "T1"},
    ]}

    actions, refused = coordinator.read_decision(arguments, handles, state, matters, 2)

    assert [(x.task_id, x.to_task_id) for x in actions] == [(a.id, b.id)]
    assert len(refused) == 1 and "wait for ever" in refused[0]
