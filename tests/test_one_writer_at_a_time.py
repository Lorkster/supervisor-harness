"""Tasks whose scopes may meet are executed one at a time, in the plan's order.

Measured in go-live runs 5, 7 and 9 (a local model, item 9a): every approved
task was started at once -- `max_parallel_agents` is four, and the model
declared no dependencies -- so an implementer whose task built on another's
stalled on code its peer had not written yet ("peer must land X first"), and
two writers worked the same files of one tree at the same time.
"""

from __future__ import annotations

import copy
from typing import Any

from supervisor_harness.core.paths import globs_may_overlap
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import RunMode
from supervisor_harness.store.events import EventType

from .conftest import FakeProvider
from .test_send_back_criteria import PROMPT


def test_scopes_that_may_meet_are_told_apart_from_scopes_that_cannot() -> None:
    meet = [
        (["src/timing.py"], ["src/timing.py"]),
        (["src/"], ["src/timing.py"]),
        (["src/**"], ["src/core/*.py"]),
        (["src/*.py"], ["src/core/"]),          # undecided: kept apart
        ([], ["docs/x.md"]),                    # no paths is the whole workspace
        (["**"], ["anything.txt"]),
        (["src/*.py"], ["src/timing.py"]),
    ]
    apart = [
        (["src/timing.py"], ["src/reporting.py"]),
        (["src/"], ["tests/"]),
        (["src/**"], ["tests/test_x.py"]),
        (["src/*.py"], ["tests/*.py"]),
        (["src/timing.py"], ["src/core/*.py"]),
    ]
    for left, right in meet:
        assert globs_may_overlap(left, right), (left, right)
        assert globs_may_overlap(right, left), (right, left)
    for left, right in apart:
        assert not globs_may_overlap(left, right), (left, right)
        assert not globs_may_overlap(right, left), (right, left)


def _two_tasks(fake: FakeProvider, first: list[str], second: list[str]) -> dict[str, Any]:
    synthesis = fake._synthesis(None)  # type: ignore[arg-type]
    one = synthesis["tasks"][0]
    one["scope_paths"] = first
    two = copy.deepcopy(one)
    two["title"] = "Add the limiter's metrics"
    two["scope_paths"] = second
    synthesis["tasks"] = [one, two]
    return synthesis


async def _spawn_order(supervisor: Supervisor, fake: FakeProvider,
                       first: list[str], second: list[str]) -> list[str]:
    """Each implementer's spawn and end, in the order the log has them."""
    fake.overrides["synthesis"] = _two_tasks(fake, first, second)
    response = await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = supervisor.store.load_state(response.run_id)
    implementers = {a.id: state.tasks[a.task_id].title for a in state.agents.values()
                    if a.task_id and a.kind.value == "execution"}
    order: list[str] = []
    for event in supervisor.store.log(response.run_id).read():
        if event.type is EventType.AGENT_SPAWNED:
            agent = event.payload["agent"]
            if agent["id"] in implementers:
                order.append(f"start {implementers[agent['id']]}")
        elif (event.type is EventType.AGENT_STATUS
              and event.payload["agent_id"] in implementers
              and event.payload["status"] not in ("running", "pending")):
            order.append(f"end {implementers[event.payload['agent_id']]}")
    return order


async def test_tasks_that_share_files_run_one_after_the_other(
    supervisor: Supervisor, fake: FakeProvider,
) -> None:
    order = await _spawn_order(supervisor, fake, ["src/auth/**", "tests/**"],
                               ["src/auth/**", "tests/**"])
    first, second = "Add rate limiting to the login endpoint", "Add the limiter's metrics"
    assert order.index(f"start {first}") < order.index(f"end {first}") \
        < order.index(f"start {second}"), order


async def test_tasks_that_cannot_meet_still_run_together(
    supervisor: Supervisor, fake: FakeProvider,
) -> None:
    order = await _spawn_order(supervisor, fake, ["src/auth/**"], ["tests/**"])
    assert order[:2] == ["start Add rate limiting to the login endpoint",
                         "start Add the limiter's metrics"], order
