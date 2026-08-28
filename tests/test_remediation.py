"""The remediation loop: fail a task, reopen it, and prove it again.

This branch of the phase machine had no coverage at all. The fake provider's
checkpoint answered `passed: True` unconditionally, so no test could reach
`_remediate`, the second-attempt verifier, or the iteration bound -- which is
why a guard that suppressed re-verification entirely shipped without anything
noticing. Every test here fails a task on purpose.
"""

from __future__ import annotations

from typing import Any

from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import AgentKind, Phase, RunMode, TaskStatus

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"

FAILING_CHECKPOINT: dict[str, Any] = {
    "quality": 0.3,
    "scope_fidelity": 1.0,
    "completeness": 0.4,
    "passed": False,
    "gaps": ["the limiter is not applied to the login route"],
    "remediation": ["Apply the limiter to the login route and prove it with a test."],
    "avoidable_causes": [],
    "summary": "The task did not meet its definition of done.",
}


async def _run_with_one_failed_attempt(supervisor: Supervisor, fake) -> Any:
    """Fail verification once, then let the run proceed normally."""
    fake.script("verification", fake.failing_verification)
    fake.script("checkpoint", FAILING_CHECKPOINT)
    return await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)


async def test_a_failing_checkpoint_reopens_the_task_with_its_corrections(
    supervisor: Supervisor, fake
) -> None:
    """The task returns to a runnable state and carries the correction text."""
    response = await _run_with_one_failed_attempt(supervisor, fake)
    state = supervisor.store.load_state(response.run_id)

    task = next(iter(state.tasks.values()))
    assert task.attempts >= 2, "the task should have been tried again"
    assert "Checkpoint corrections" in task.action
    assert "Apply the limiter to the login route" in task.action


async def test_a_remediated_task_gets_a_verifier_of_its_own(
    supervisor: Supervisor, fake
) -> None:
    """The regression this file exists for.

    A retired verifier stays in state.agents because the fold replays it. When
    the construction guard matched on task and kind alone, the first attempt's
    verifier suppressed every later one and the remediated task was closed on
    stale evidence -- the run could never record work it had actually done.
    """
    response = await _run_with_one_failed_attempt(supervisor, fake)
    state = supervisor.store.load_state(response.run_id)
    task = next(iter(state.tasks.values()))

    verifiers = [
        a for a in state.agents.values()
        if a.kind is AgentKind.VERIFICATION and a.task_id == task.id
    ]
    assert len(verifiers) >= 2, (
        f"expected a verifier per attempt, got {len(verifiers)} "
        f"for {task.attempts} attempts"
    )
    assert {a.attempt for a in verifiers} >= {1, 2}, (
        "each verifier must record the attempt it was built for"
    )


async def test_the_second_attempt_can_reach_verified(
    supervisor: Supervisor, fake
) -> None:
    """Remediation is only worth having if the retry can actually succeed."""
    response = await _run_with_one_failed_attempt(supervisor, fake)
    state = supervisor.store.load_state(response.run_id)
    task = next(iter(state.tasks.values()))

    assert task.status is TaskStatus.VERIFIED, (
        f"task ended {task.status}; a remediated task that passes its criteria "
        "on the second attempt must be recorded as verified"
    )
    assert response.action == "complete", response.message


async def test_a_checkpoint_that_keeps_failing_stops_at_the_iteration_bound(
    supervisor: Supervisor, fake, config
) -> None:
    """The loop must terminate rather than remediate forever."""
    fake.overrides["verification"] = {"results": [], "summary": "nothing proven"}
    fake.overrides["checkpoint"] = dict(FAILING_CHECKPOINT)

    response = await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = supervisor.store.load_state(response.run_id)

    assert response.action in ("complete", "failed"), response.message
    assert state.phase in (Phase.COMPLETE, Phase.FAILED)
    assert len(state.checkpoints) <= config.policy.max_checkpoint_iterations, (
        f"{len(state.checkpoints)} checkpoints exceeds the configured bound"
    )
    task = next(iter(state.tasks.values()))
    assert task.attempts <= config.policy.max_task_attempts


async def test_a_failing_checkpoint_with_nothing_to_reopen_does_not_loop(
    supervisor: Supervisor, fake
) -> None:
    """A checkpoint can fail on grounds no task can act on; that must settle."""
    fake.script("checkpoint", FAILING_CHECKPOINT)

    response = await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = supervisor.store.load_state(response.run_id)

    # Verification passed, so no task is FAILED and _remediate reopens nothing.
    assert response.action == "complete", response.message
    assert all(t.status is TaskStatus.VERIFIED for t in state.tasks.values())
    notes = [
        e.payload.get("text", "")
        for e in supervisor.store.log(response.run_id).read_all()
        if e.type.value == "note"
    ]
    assert any("no actionable remediation" in n for n in notes), notes[-5:]
