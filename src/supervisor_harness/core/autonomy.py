"""Approving the envelope instead of each task: what has to hold for that to be safe.

Per-task approval cannot widen the envelope -- an edited scope is clamped to it
-- so what a person adds at approval is not authority but a *quality* judgement:
are these the right tasks, can their definitions of done fail? Envelope mode
replaces that person with two things that are not models, and keeps the person
for what only they can decide.

**Before the run starts** (:func:`refusal`): the owner has enabled the mode in
their own configuration (``policy.approval``, which a workspace file cannot set),
and the run has a verifier that is not a model -- the harness may run commands,
its tests are mandatory, ``fails_before`` is on, and the work happens on the
run's own branch. Without those, nothing would check the work except models,
and a run approved on that basis is the always-open lock.

**At approval** (:func:`gate`): each proposed task is checked deterministically.
One that passes is approved by the harness, recorded as such. One that does not
is parked as an escalation, with the harness's reason, and the owner answers it
at ``awaiting_owner`` once the rest of the run is done. Nothing here judges
*whether a task is a good idea* -- that would be a model deciding, and a model
may only ever narrow what happens (batch G of the autonomy plan). Every check
here can only send a task to the owner; none can approve one the per-task path
would have refused.
"""

from __future__ import annotations

from pathlib import Path

from ..config import HarnessConfig, Policy
from ..models import (
    EscalationReason,
    ExecutionTask,
    Severity,
    VerifyMethod,
)
from .baseline import git_baseline
from .dod import unsafe_command, validate_criteria

#: The value of ``policy.approval`` that enables this mode.
ENVELOPE = "envelope"

#: Risks a task may carry and still go ahead without its owner. The rating is
#: the planning model's own, so it can only be used to *send* a task to the
#: owner: a model that understates a risk loses nothing it had before.
UNATTENDED_RISKS = frozenset({Severity.INFO, Severity.LOW, Severity.MEDIUM})

#: Criterion methods the harness proves by running something.
_RUN_METHODS = (VerifyMethod.COMMAND, VerifyMethod.TEST, VerifyMethod.FAILS_BEFORE)


def refusal(config: HarnessConfig, workspace: Path) -> str | None:
    """Why this run may not be approved by envelope, or ``None`` if it may."""
    policy = config.policy
    if policy.approval != ENVELOPE:
        return (
            "envelope approval is not enabled. Set \"policy\": {\"approval\": "
            "\"envelope\"} in your own configuration (under SUPERVISOR_HOME, normally "
            "~/.supervisor/config.json); a workspace's file cannot set it"
        )
    missing = [
        reason for reason, ok in (
            ("policy.allow_command_execution: the harness must be able to run the checks "
             "itself", policy.allow_command_execution),
            ("policy.require_tests: every code task must carry its tests", policy.require_tests),
            ("policy.require_fails_before: the tests must be shown to detect the change",
             policy.require_fails_before),
            ("policy.execution_worktree: the work must happen on the run's own branch",
             policy.execution_worktree),
        ) if not ok
    ]
    if not git_baseline(workspace):
        missing.append("a git repository with a commit to branch from")
    if missing:
        return ("envelope approval needs a verifier that is not a model, and this run "
                "would not have one. Missing: " + "; ".join(missing))
    return None


def gate(task: ExecutionTask, policy: Policy) -> list[tuple[EscalationReason, str]]:
    """Every reason this task must go to its owner rather than ahead; empty if none."""
    reasons: list[tuple[EscalationReason, str]] = []
    if task.clamped:
        reasons.append((EscalationReason.NEEDS_WIDER_SCOPE,
                        "the task asked for more than the run may modify, and was "
                        "narrowed: " + "; ".join(task.clamped)))
    weak = [i.problem for i in validate_criteria(task.dod, policy) if i.severity is Severity.HIGH]
    if weak:
        reasons.append((EscalationReason.UNENFORCEABLE_DEFINITION_OF_DONE,
                        "its definition of done could not be enforced: " + "; ".join(weak)))
    unrunnable = [
        f"{c.statement!r}: {unsafe_command(c.command) or 'no command'}"
        for c in task.mandatory_criteria
        if c.method in _RUN_METHODS and (not c.command.strip() or unsafe_command(c.command))
    ]
    if unrunnable:
        reasons.append((EscalationReason.UNRUNNABLE_CRITERION,
                        "a mandatory check the harness will not run: " + "; ".join(unrunnable)))
    if task.risk not in UNATTENDED_RISKS:
        reasons.append((EscalationReason.HIGH_RISK,
                        f"rated {task.risk} risk by the plan, above what goes ahead unattended"))
    return reasons
