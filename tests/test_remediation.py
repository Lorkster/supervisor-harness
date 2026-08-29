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


# -- who a correction is for -----------------------------------------------

TWO_TASKS: dict[str, Any] = {
    "summary": "Two independent gaps: the limiter, and the audit log.",
    "conflicts": [],
    "open_questions": [],
    "recommended_mode": "execute",
    "tasks": [
        {
            "title": "Add rate limiting to the login endpoint",
            "action": "Add middleware in src/auth/login.py that limits login attempts.",
            "motivation": "Credential stuffing is unconstrained.",
            "dod": [
                {
                    "statement": "The eleventh attempt from one IP within a minute is refused",
                    "method": "test", "command": "pytest tests/test_rate_limit.py -q",
                    "expect": "0", "mandatory": True,
                },
                {
                    "statement": "The limiter keys on account identifier as well as IP",
                    "method": "inspection", "expect": "src/auth/login.py: account_key",
                    "mandatory": True,
                },
            ],
            "scope_paths": ["src/auth/**"],
            "suggested_role": "security-engineer",
        },
        {
            "title": "Redact secrets from the audit log",
            "action": "Stop src/audit.py writing the bearer token into the log line.",
            "motivation": "The audit log is readable by support staff.",
            "dod": [
                {
                    "statement": "The audit line carries no bearer token",
                    "method": "test", "command": "pytest tests/test_audit.py -q",
                    "expect": "0", "mandatory": True,
                },
                {
                    "statement": "The redaction helper is applied at the single write site",
                    "method": "inspection", "expect": "src/audit.py: redact",
                    "mandatory": True,
                },
            ],
            "scope_paths": ["src/audit.py"],
            "suggested_role": "implementer",
        },
    ],
}

LIMITER_CORRECTION = "key the limiter on the account identifier as well as the client IP"
AUDIT_CORRECTION = "redact the refresh token as well, not only the bearer token"

TARGETED_CHECKPOINT: dict[str, Any] = {
    **FAILING_CHECKPOINT,
    "remediation": [
        f"Add rate limiting to the login endpoint: {LIMITER_CORRECTION}.",
        f"Redact secrets from the audit log: {AUDIT_CORRECTION}.",
        "Every task: state the file you changed in your report.",
    ],
}


async def test_each_reopened_task_is_briefed_with_its_own_corrections(
    supervisor: Supervisor, fake
) -> None:
    """A correction that names another task must not reach this one's brief.

    Briefing every failed task with every correction is not a cosmetic waste:
    the corrections are instructions, so an agent reopening the limiter was
    told to change the audit log as well. In a real run that had to be undone
    by hand.
    """
    fake.overrides["synthesis"] = TWO_TASKS
    fake.script("verification", fake.failing_verification, fake.failing_verification)
    fake.script("checkpoint", TARGETED_CHECKPOINT)

    response = await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = supervisor.store.load_state(response.run_id)

    limiter = next(t for t in state.tasks.values() if "rate limiting" in t.title)
    audit = next(t for t in state.tasks.values() if "audit log" in t.title)
    assert limiter.attempts >= 2 and audit.attempts >= 2, "both tasks should be reopened"

    assert LIMITER_CORRECTION in limiter.action
    assert AUDIT_CORRECTION not in limiter.action, (
        "the limiter was briefed with the audit log's correction"
    )
    assert AUDIT_CORRECTION in audit.action
    assert LIMITER_CORRECTION not in audit.action, (
        "the audit log was briefed with the limiter's correction"
    )
    # A correction that names no task is everyone's.
    assert "state the file you changed" in limiter.action
    assert "state the file you changed" in audit.action
