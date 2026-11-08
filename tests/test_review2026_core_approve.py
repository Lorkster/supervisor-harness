"""Approval acts only where approval is asked, and only on what it was asked about.

Review findings 1 and 2. `parse_decision` used to turn any text it did not know
-- "rejct", "deny", "" -- into an approval, and `Supervisor.approve` had no
guard on the run's phase or on the task's status, so a stray call restarted a
finished run and a reject flipped a task under a live agent.
"""

from __future__ import annotations

import pytest

from supervisor_harness.contracts import parse_decision
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import Decision, Phase, RunMode, TaskStatus
from supervisor_harness.serde import to_jsonable
from supervisor_harness.store.events import EventType

from .test_envelope_expiry import _age_the_grant

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"


async def _reach_approval(supervisor: Supervisor):
    response = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    while response.action not in ("await_approval", "complete", "failed"):
        response = await supervisor.advance(response.run_id)
    assert response.action == "await_approval", response.message
    return response


@pytest.mark.parametrize("text", ["rejct", "deny", "no", "", None, 7, "approved!"])
def test_an_unknown_decision_is_not_an_approval(text) -> None:
    assert parse_decision(text) is None


def test_a_known_decision_still_parses() -> None:
    assert parse_decision(" Reject ") is Decision.REJECT
    assert parse_decision("approve") is Decision.APPROVE


async def test_a_misspelt_decision_is_not_applied_and_the_run_keeps_waiting(
    supervisor: Supervisor,
) -> None:
    response = await _reach_approval(supervisor)
    run_id = response.run_id
    task_id = response.tasks[0]["id"]

    result = await supervisor.approve(run_id, [{"task_id": task_id, "decision": "rejct"}])

    assert result.action == "await_approval", result.message
    assert result.detail["decisions_applied"] == 0
    assert task_id in result.detail["not_applied"]
    assert "rejct" in result.detail["not_applied"][task_id]
    state = supervisor.store.load_state(run_id)
    assert state.phase is Phase.AWAITING_APPROVAL
    assert state.tasks[task_id].status is TaskStatus.PROPOSED


async def test_an_unknown_task_id_is_reported_not_swallowed(supervisor: Supervisor) -> None:
    response = await _reach_approval(supervisor)
    result = await supervisor.approve(
        response.run_id, [{"task_id": "tsk_nope", "decision": "approve"}]
    )
    assert result.detail["decisions_applied"] == 0
    assert "tsk_nope" in result.detail["not_applied"]
    state = supervisor.store.load_state(response.run_id)
    assert state.phase is Phase.AWAITING_APPROVAL


async def test_approve_on_a_complete_run_changes_nothing(supervisor: Supervisor) -> None:
    final = await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    assert final.action == "complete", final.message
    before = supervisor.store.load_state(final.run_id)
    task_id = next(iter(before.tasks))

    result = await supervisor.approve(final.run_id, [{"task_id": task_id, "decision": "reject"}])

    after = supervisor.store.load_state(final.run_id)
    assert after.phase is Phase.COMPLETE
    assert after.tasks[task_id].status is before.tasks[task_id].status
    assert after.last_seq == before.last_seq, "a refused approve must not write to the log"
    assert result.detail["decisions_applied"] == 0
    assert "complete" in result.message


async def test_approve_on_a_run_that_has_not_proposed_anything_is_refused(
    supervisor: Supervisor,
) -> None:
    response = await _reach_approval(supervisor)
    # Put the run back where a stray call could find it: executing, not asking.
    task_id = response.tasks[0]["id"]
    await supervisor.approve(response.run_id, [{"task_id": task_id, "decision": "approve"}])
    state = supervisor.store.load_state(response.run_id)
    if state.phase is Phase.AWAITING_APPROVAL:  # pragma: no cover - defensive
        pytest.skip("run did not leave approval")
    seq = state.last_seq

    result = await supervisor.approve(response.run_id, [{"task_id": task_id, "decision": "reject"}])

    after = supervisor.store.load_state(response.run_id)
    assert after.last_seq == seq
    assert after.tasks[task_id].status is not TaskStatus.REJECTED
    assert result.detail["decisions_applied"] == 0


async def test_a_task_that_is_not_proposed_is_not_flipped(supervisor: Supervisor) -> None:
    """Inside the approval wait, a decision still applies only to a PROPOSED task."""
    response = await _reach_approval(supervisor)
    run_id = response.run_id
    task_id = response.tasks[0]["id"]
    session = supervisor.store.open(run_id)
    task = session.state.tasks[task_id]
    task.status = TaskStatus.IN_PROGRESS  # a live agent holds it
    session.emit(EventType.TASK_UPDATED, {"task": to_jsonable(task)})

    result = await supervisor.approve(run_id, [{"task_id": task_id, "decision": "reject"}])

    assert supervisor.store.load_state(run_id).tasks[task_id].status is TaskStatus.IN_PROGRESS
    assert task_id in result.detail["not_applied"]
    assert "in_progress" in result.detail["not_applied"][task_id]


async def test_the_stale_envelope_renewal_still_works(supervisor: Supervisor) -> None:
    """Executing is a valid phase for approve exactly while a renewal is pending."""
    response = await _reach_approval(supervisor)
    run_id = response.run_id
    _age_the_grant(supervisor, run_id)
    paused = await supervisor.approve(
        run_id, [{"task_id": t["id"], "decision": "approve"} for t in response.tasks]
    )
    assert paused.detail["needs"] == "envelope_renewal"

    renewed = await supervisor.approve(run_id, [], renew_envelope=True)

    assert renewed.action != "await_approval", renewed.message
    state = supervisor.store.load_state(run_id)
    assert state.envelope.source == "renewed on resume"


def _two_task_plan(request):
    from .conftest import FakeProvider

    plan = FakeProvider()._synthesis(request)
    first = plan["tasks"][0]
    second = {**first, "title": "Document the limiter", "depends_on": [first["title"]],
              "dod": [dict(first["dod"][0]), dict(first["dod"][1])]}
    plan["tasks"] = [first, second]
    return plan


async def test_a_task_waiting_on_a_rejected_one_still_runs_and_is_told_so(
    supervisor: Supervisor, fake
) -> None:
    fake.script("synthesis", _two_task_plan)
    response = await _reach_approval(supervisor)
    first, second = response.tasks[0]["id"], response.tasks[1]["id"]
    assert supervisor.store.load_state(response.run_id).tasks[second].depends_on == [first]

    result = await supervisor.approve(response.run_id, [
        {"task_id": first, "decision": "reject"},
        {"task_id": second, "decision": "approve"},
    ])

    state = supervisor.store.load_state(response.run_id)
    assert state.tasks[second].depends_on == []
    assert any("which you rejected" in n.text for n in state.notes)
    assert state.tasks[second].status is not TaskStatus.APPROVED, result.message
