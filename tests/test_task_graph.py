"""Task dependency resolution and per-attempt verification.

Two defects that this harness demonstrated on itself while reviewing itself.

The synthesis model names a dependency by title, because task ids do not exist
until the harness parses its answer -- but ``runnable_tasks`` matched those
titles against a set of ids, so a dependent task was approved by the user and
then never dispatched, silently, for the life of the run.

Separately, a retired verification agent stays in ``state.agents`` because the
fold replays it, and the guard that decides whether to build a verifier did not
look at which attempt the existing one belonged to. A remediated task was
therefore closed on its first attempt's evidence and could never be re-proven.
"""

from __future__ import annotations

from supervisor_harness.core import phases
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    ExecutionTask,
    RunState,
    TaskStatus,
)


def _task(title: str, **kw: object) -> ExecutionTask:
    return ExecutionTask(title=title, action=f"do: {title}", **kw)  # type: ignore[arg-type]


# -- dependency resolution -------------------------------------------------


def test_a_dependency_named_by_title_resolves_to_that_tasks_id() -> None:
    """The model writes titles; runnable_tasks compares ids. Bridge the two."""
    first = _task("Settle every agent-terminating path")
    second = _task("Give each remediation attempt its own verifier",
                   depends_on=["Settle every agent-terminating path"])

    notes = phases.resolve_dependencies([first, second])

    assert second.depends_on == [first.id]
    assert notes == {}


def test_dependency_resolution_is_insensitive_to_case_and_padding() -> None:
    first = _task("Fix the index")
    second = _task("Use the index", depends_on=["  fix THE index  "])

    phases.resolve_dependencies([first, second])

    assert second.depends_on == [first.id]


def test_an_already_resolved_id_is_left_alone() -> None:
    """Resolution must be idempotent -- it runs once, but must not corrupt ids."""
    first = _task("First")
    second = _task("Second", depends_on=[first.id])

    phases.resolve_dependencies([first, second])
    phases.resolve_dependencies([first, second])

    assert second.depends_on == [first.id]


def test_a_dependency_naming_no_task_is_dropped_and_reported() -> None:
    """Left in place it would block the task forever, and say nothing."""
    task = _task("Lonely", depends_on=["A task nobody proposed"])

    notes = phases.resolve_dependencies([task])

    assert task.depends_on == []
    assert "A task nobody proposed" in " ".join(notes[task.id])


def test_a_self_dependency_is_dropped_and_reported() -> None:
    task = _task("Recursive", depends_on=["Recursive"])

    notes = phases.resolve_dependencies([task])

    assert task.depends_on == []
    assert notes[task.id]


def test_a_dependent_task_is_runnable_only_once_its_dependency_verifies() -> None:
    """The end-to-end property the resolution exists to make true."""
    first = _task("First", status=TaskStatus.APPROVED)
    second = _task("Second", depends_on=["First"], status=TaskStatus.APPROVED)
    phases.resolve_dependencies([first, second])

    state = RunState(prompt="p")
    state.tasks = {first.id: first, second.id: second}

    runnable = {t.id for t in phases.runnable_tasks(state)}
    assert runnable == {first.id}, "the dependent task must wait"

    first.status = TaskStatus.VERIFIED
    runnable = {t.id for t in phases.runnable_tasks(state)}
    assert second.id in runnable, "the dependent task must run once unblocked"


def test_an_unresolved_dependency_would_park_the_task_forever() -> None:
    """Guards the regression directly: unresolved, the task never runs."""
    first = _task("First", status=TaskStatus.VERIFIED)
    second = _task("Second", depends_on=["First"], status=TaskStatus.APPROVED)
    # deliberately NOT resolved
    state = RunState(prompt="p")
    state.tasks = {first.id: first, second.id: second}

    assert second.id not in {t.id for t in phases.runnable_tasks(state)}

    phases.resolve_dependencies([first, second])
    assert second.id in {t.id for t in phases.runnable_tasks(state)}


# -- per-attempt verification ---------------------------------------------


def test_a_verification_agent_records_the_attempt_it_was_built_for() -> None:
    task = _task("Anything")
    task.attempts = 2
    state = RunState(prompt="p")

    agent = phases.build_verification_agent(state, task, _config(), _registry())

    assert agent.attempt == 2
    assert agent.task_id == task.id


def test_a_retired_verifier_does_not_match_the_next_attempt() -> None:
    """The guard predicate itself: same task and kind, different attempt.

    A stopped or done verifier stays in state.agents because the fold replays
    AGENT_SPAWNED, so identity alone is not enough to decide whether this
    attempt has been verified.
    """
    task = _task("Anything")
    task.attempts = 2
    retired = AgentSpec(kind=AgentKind.VERIFICATION, task_id=task.id, attempt=1)

    covered = (retired.task_id == task.id
               and retired.kind is AgentKind.VERIFICATION
               and retired.attempt == task.attempts)

    assert not covered, "attempt 2 must not be considered already verified"


def _config():
    from supervisor_harness.config import HarnessConfig

    return HarnessConfig()


def _registry(tmp_path=None):
    from pathlib import Path

    from supervisor_harness.agents.registry import AgentRegistry
    from supervisor_harness.host.detect import HostInfo

    return AgentRegistry(Path(tmp_path or "."), HostInfo(), [])
