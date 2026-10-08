"""Batch G: a second reading of each task, which can only veto.

Under envelope approval no person reads a task before it runs: the
deterministic gate (`core/autonomy.py`) decides. The gate checks what can be
checked mechanically -- scope, criteria that can be enforced, risk -- and in
three go-live runs it passed the same task, which every check then verified:
add the Playwright suite to `npm run check`. The project's CI runs `npm run
check` before it installs Playwright's browser, so the change breaks CI. A
person at the approval prompt would have caught it by reading the workflow.

This is that reading: a reviewer with read-only tools looks at the task against
the request and the repository, and rules -- proceed, or a veto from a fixed
menu. A veto parks the task for the owner, like any escalation. Adapted from
Turnstone's judge, with its rule: a judge only vetoes. It never passes a task
the gate refused (it is only asked about the ones the gate passed), it cannot
widen anything, and a reviewer that fails to rule is noted and the gate's
decision stands -- the absence of a veto is not an approval it made.
"""

from __future__ import annotations

from typing import Any

from ..contracts import _DOD
from ..models import ExecutionTask, RunState, TaskStatus

#: How much of each other task's action the reviewer is shown.
PLAN_ACTION_CHARS = 300

#: What a veto may say. The first four are the plan's; the fifth is the case
#: that made the batch worth building.
VETO_REASONS: dict[str, str] = {
    "off_request": "it does not serve what the request asked for, or works against it",
    "scope_unjustified": "it changes things its purpose does not need",
    "criteria_cannot_fail": "its definition of done would pass even if the change were wrong",
    "risk_understated": "it is riskier than its plan says",
    "breaks_the_project": ("it would break something the project relies on: its CI, its "
                           "build, or its documented workflow"),
}

REVIEWER_SYSTEM = """\
You review a task before it is carried out with no person watching. You can only \
veto: if nothing is wrong with it, it goes ahead.

Read what you need with the tools, then call ruling. Look in particular at what the \
task would change and what depends on it: the project's CI workflows (for example \
.github/workflows/), its build and package scripts, and its own documented workflow \
(docs/, CONTRIBUTING, AGENTS.md, CLAUDE.md). A change that looks right in isolation \
can break the pipeline that runs it.

Veto only for a concrete reason you can point to in a file. "It could be better" is \
not a veto; "this breaks .github/workflows/ci.yml line 31" is.

The task is one part of a plan, and the brief lists the other parts. Judge it by its \
own part: a requirement another task in the plan covers -- a test, a wiring, a \
follow-up -- is not missing from this one. A gap no task in the plan covers is.
"""

NUDGE = "Read what you need, then call ruling: proceed, or a veto with its reason."
NOW = ("Your reading time is up. Call ruling now: proceed, or a veto naming the file "
       "and line that justify it.")

RULING_TOOL: dict[str, Any] = {
    "name": "ruling",
    "description": "Your decision on the task. Call it once.",
    "parameters": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["proceed", *VETO_REASONS]},
            "why": {"type": "string",
                    "description": "For a veto: what is wrong, citing file and line"},
        },
        "required": ["verdict"],
    },
}

ROLE = (REVIEWER_SYSTEM, "ruling", NUDGE)


def review_brief(state: RunState, task: ExecutionTask) -> str:
    criteria = "\n".join(f"- {c.statement}" for c in task.dod) or "- (none)"
    scope = ", ".join(f"`{p}`" for p in task.scope.paths) or "the whole workspace"
    vetoes = "\n".join(f"- `{name}`: {meaning}" for name, meaning in VETO_REASONS.items())
    return (
        f"# Task to review: {task.title}\n\n"
        f"## The request it is part of\n{state.prompt}\n\n"
        f"## What it would do\n{task.action}\n\n{task.motivation}\n\n"
        f"## It may change\n{scope}\n\n"
        f"## Done means\n{criteria}\n\n"
        + _rest_of_the_plan(state, task)
        + f"## You may veto it because\n{vetoes}"
    )


def _rest_of_the_plan(state: RunState, task: ExecutionTask) -> str:
    """The plan's other tasks, so a task is not faulted for what a sibling does.

    Measured in go-live run 18: two tasks were vetoed as `criteria_cannot_fail`
    for not requiring the network-cut Playwright test -- which was another
    task of the same plan, approved, carried out and verified. The reviewer
    had been shown each task alone.
    """
    others = [t for t in state.tasks.values()
              if t.id != task.id and t.status is not TaskStatus.REJECTED]
    if not others:
        return ""
    lines = []
    for other in others:
        action = " ".join((other.action or other.title).split())
        if len(action) > PLAN_ACTION_CHARS:
            action = action[:PLAN_ACTION_CHARS].rsplit(" ", 1)[0] + " ..."
        lines.append(f"- **{other.title}**: {action}")
    return "## The rest of the plan\n" + "\n".join(lines) + "\n\n"


def parse_ruling(arguments: dict[str, Any] | None) -> tuple[str, str] | None:
    """The veto and its reason, or None to let the task proceed.

    Anything that is not a recognised veto -- "proceed", a verdict off the
    menu, no ruling at all -- is not a veto: this reviewer cannot say no by
    accident, only by naming a reason.
    """
    if not arguments:
        return None
    verdict = str(arguments.get("verdict", "")).strip()
    if verdict not in VETO_REASONS:
        return None
    return verdict, str(arguments.get("why", "")).strip()


# -- one revision after a veto ----------------------------------------------------
# Go-live run 21: the reviewer vetoed three of five tasks, each rightly and each
# with the fix in its reason -- the e2e test bundled with the CI-breaking wiring,
# a loader change whose caller no task covered, results criteria that could not
# fail. A veto ended each one; nothing revised them. Like a definition of done
# that cannot be enforced, a vetoed task goes back to the planner once, with the
# veto, and the revision faces the gate and the reviewer again. A second veto
# goes to the owner. The reviewer still only vetoes: the revision is the
# planner's, and it can widen nothing -- its scope is held to the run's envelope.

REVISION_SYSTEM = """\
You planned a task that a reviewer has vetoed. Revise the task so the objection \
no longer holds: change what it does, what it may change, or its definition of \
done. Keep what the request needs from this task. If the objection is that part \
of the task should not be done at all, leave that part out. A requirement another \
task in the plan covers does not belong here.
"""

REVISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string",
                   "description": "What the task will now concretely do"},
        "scope_paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "dod": {"type": "array", "items": _DOD, "minItems": 2},
    },
    "required": ["action", "scope_paths", "dod"],
}


def revision_prompt(state: RunState, task: ExecutionTask, veto: tuple[str, str]) -> str:
    criteria = "\n".join(
        f"- [{c.method.value}] {c.statement}"
        + (f" (command: `{c.command}`)" if c.command else "")
        + (f" (expect: {c.expect})" if c.expect else "")
        for c in task.dod) or "- (none)"
    return (
        f"# The vetoed task: {task.title}\n\n"
        f"## The request\n{state.prompt}\n\n"
        f"## What it would do\n{task.action}\n\n"
        f"## It may change\n{', '.join(task.scope.paths) or 'the whole workspace'}\n\n"
        f"## Done means\n{criteria}\n\n"
        + _rest_of_the_plan(state, task)
        + f"## The veto: `{veto[0]}` -- {VETO_REASONS.get(veto[0], '')}\n{veto[1]}\n\n"
        "Return the revised task: its action, the paths it may change, and its "
        "whole definition of done."
    )
