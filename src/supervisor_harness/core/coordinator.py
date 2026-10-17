"""Batch O: the coordinator holds the plan.

Nineteen PRs from #112 to #132 each added a rule for a case one run had hit:
which task a failed check belongs to, where a finding goes, when a check waits
for another task, what a blocked report's words name. Each was right for its
run, and the next run found a case it did not cover. Turnstone keeps those
calls with its coordinator, a model: when a child reports back it decides
"whether the goal is met, then close it out, push a follow-up, or spawn another
child to cover the gap", and its primer allows a model that "merely *picks*
among actions the plain rules already allow".

So here. When reports come back -- a task that failed its checks, an implementer
that stopped, a finding an agent sent about other tasks' work -- the coordinator
is shown the facts: the plan, each task's state and attempts, each failed
check's output, the files it names and which tasks' agents wrote them, in what
order. It picks from a fixed menu (`MENU`). This module reads its picks and
refuses any the harness cannot carry out, saying why; `Supervisor._coordinate`
applies the rest. It cannot pass a check, drop one, overrule a mechanical
result, widen a scope, or answer for the owner: everything it can do sends work
back, moves a check to other work, orders work, or asks the owner.

A consultation is fresh each time, so it is a case that can be replayed: what
the coordinator holds is the run's state, which it is given whole, including
what it decided before.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import (
    SUPERVISOR,
    AgentKind,
    CriterionStatus,
    ExecutionTask,
    MessageKind,
    RunState,
    TaskStatus,
)
from .fails_before import safe_relative
from .paths import matches_any

#: What an agent sends the supervisor that may concern another task's work.
FINDING_KINDS = frozenset({MessageKind.WARNING, MessageKind.FINDING,
                           MessageKind.CONTRADICTION})

#: A terminal's colour codes, which a check's output carries and a reader does not need.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

#: How much of a failed check's evidence, and of each task's action, it is shown.
EVIDENCE_CHARS = 1500
ACTION_CHARS = 300

#: The picks, and what each does. The names are the `decide` tool's enum.
MENU: dict[str, str] = {
    "send_back": ("the task's implementer goes again, in its own conversation, with your "
                  "`message` and the checks that failed. `check`, if given, is one more "
                  "thing its verifier must find true. For a task not yet started, the "
                  "message is added to what it will do. Use it for a task whose own work "
                  "is wrong, or that a finding shows is wrong."),
    "move_check": ("the failed check `criterion` goes to `to_task`, whose work it fails on, "
                   "with your `message`; that task is sent back if it had finished. The "
                   "task the check leaves is judged on the rest of its checks. Use it when "
                   "a check fails on another task's files, or on a part another task "
                   "builds."),
    "wait": ("the task starts again once `to_task` is done, its stop not counted against "
             "it. Use it when it needs work another task is still to do, or is being sent "
             "back to do."),
    "as_it_stands": ("a task that stopped is judged on what it did. Use it when it stopped "
                     "for something that is not its own work."),
    "ask_owner": ("the owner is asked your `message`, and the task waits for the answer. "
                  "Only for what no task in the plan can do: a decision, a permission, a "
                  "credential, a resource."),
    "leave_failed": ("nothing more is tried for the task; it is reported as failed. Use it "
                     "when another attempt would not help."),
}

#: The picks that settle what happens to a task; at most one per task.
_DISPOSITIONS = frozenset(MENU) - {"move_check"}

SYSTEM = """\
You coordinate a run in which agents carry out a plan of tasks in one repository. \
You do not edit files or run commands; the agents do. The harness has run every \
check and shows you what came back: tasks that failed their checks, agents that \
stopped, findings agents sent about other tasks' work.

Decide what happens next for each, and call decide once with all of it. Read the \
files you need first, with the tools.

- Send work to the task whose work is wrong -- not to whoever found it. A check \
that fails only on files another task wrote is that task's; the files a check names \
and who wrote them are listed for you. The task that wrote a file last has the \
version that is there.
- A test that fails because the product does not do what its requirement says is \
the product's: move the check to the task that builds it. A test that is itself \
wrong is its writer's.
- What a test saw the running product do outweighs any reading of the product's \
code -- yours or another agent's. Code that reads right can still not behave so: \
a style rule, a later render, an event that never fires. A claim that the code is \
correct is not evidence against a test. When a test that drives the product fails, \
or a tester reports what the product did, the product's task gets it. A test is its \
writer's only where the requirement as quoted, read on its own, says otherwise; when \
some of a tester's tests are wrong and some are not, each goes to its own task. \
Never ask a tester to make a test check less.
- An agent that stopped because it needs another task's work waits for that task. \
One that stopped only for other tasks' files is judged as it stands, and what fails \
there goes to their tasks.
- An agent may delete or fix what it made itself, scratch files included; tell it so \
when that is what it missed.
- Ask the owner only for what no task in the plan can do.
- Say in each message what is wrong and where, concretely: file, line, test, output.
"""

NUDGE = "Read what you need, then call decide with what happens to each open matter."
NOW = "Your reading time is up. Call decide now, with a pick for each open matter."
ROLE = (SYSTEM, "decide", NUDGE)


@dataclass(frozen=True)
class Matter:
    """One thing that came back and is open: a failed task, a stop, a finding."""

    key: str
    kind: str           # "failed" | "stopped" | "finding"
    task_id: str        # for a finding: the sender's task, if it has one
    message_id: str = ""


@dataclass
class Handles:
    """The short names a consultation uses: T1.. for tasks, C1.. for failed checks.

    Matched exactly, so a pick names a task the way the brief named it or is
    refused -- nothing reads a title out of prose.
    """

    tasks: dict[str, str]
    checks: dict[str, tuple[str, str]]

    def of(self, task_id: str) -> str:
        return next((h for h, t in self.tasks.items() if t == task_id), "?")


@dataclass
class Action:
    """One pick the harness will apply."""

    do: str
    task_id: str
    criterion_id: str = ""
    to_task_id: str = ""
    message: str = ""
    check: str = ""

    def record(self, state: RunState) -> dict[str, str]:
        task = state.tasks.get(self.task_id)
        out = {"do": self.do, "task": self.task_id,
               "attempt": str(task.attempts if task else 0)}
        for key, value in (("criterion", self.criterion_id), ("to_task", self.to_task_id),
                           ("message", self.message), ("check", self.check)):
            if value:
                out[key] = value
        return out


# -- what is open ---------------------------------------------------------------------


def decided(state: RunState) -> set[str]:
    """The matters already put to the coordinator."""
    return {key for c in state.coordination for key in c.matters}


def open_matters(state: RunState) -> list[Matter]:
    """What has come back and not yet been decided, in the plan's order.

    A failed task, once per attempt -- one whose agent was abandoned, with no
    check failed, is the host's "do not send this again" and is not open. A task
    stopped without a question to the owner, once per attempt. A finding sent
    to the supervisor, once.
    """
    seen = decided(state)
    asked = {e.task_id for e in state.open_escalations()}
    out: list[Matter] = []
    for task in state.tasks.values():
        if (task.status is TaskStatus.FAILED
                and any(c.status is CriterionStatus.FAIL for c in task.dod)):
            out.append(Matter(f"failed:{task.id}:{task.attempts}", "failed", task.id))
        elif task.status is TaskStatus.BLOCKED and task.id not in asked:
            out.append(Matter(f"stopped:{task.id}:{task.attempts}", "stopped", task.id))
    for message in state.messages:
        if message.recipient == SUPERVISOR and message.kind in FINDING_KINDS:
            sender = state.agents.get(message.sender)
            out.append(Matter(f"finding:{message.id}", "finding",
                              sender.task_id or "" if sender else "", message.id))
    return [m for m in out if m.key not in seen]


def left_failed(state: RunState) -> set[str]:
    """The tasks the coordinator left failed at the attempt they are on."""
    return {a["task"] for c in state.coordination for a in c.actions
            if a.get("do") == "leave_failed" and (task := state.tasks.get(a["task"]))
            and str(task.attempts) == a.get("attempt")}


def stop_turn(state: RunState, task: ExecutionTask) -> Any:
    """The last turn of the task's implementer on this attempt: what it said when it stopped."""
    agents = {a.id for a in state.agents.values()
              if a.task_id == task.id and a.kind is AgentKind.EXECUTION
              and a.attempt == task.attempts}
    return next((t for t in reversed(state.turns) if t.agent_id in agents), None)


def writers(state: RunState, tree: Path, rel: str) -> list[tuple[str, int]]:
    """Who wrote ``rel``, in order: (task id, attempt), a run of one task's writes as one."""
    out: list[tuple[str, int]] = []
    for turn in state.turns:
        agent = state.agents.get(turn.agent_id)
        if (agent is None or agent.kind is not AgentKind.EXECUTION or not agent.task_id
                or not any(safe_relative(raw, tree) == rel for raw in turn.files_touched)):
            continue
        entry = (agent.task_id, agent.attempt)
        if not out or out[-1] != entry:
            out.append(entry)
    return out


# -- what it is shown -----------------------------------------------------------------


def handles_for(state: RunState, matters: list[Matter]) -> Handles:
    tasks = {f"T{n}": task.id for n, task in enumerate(state.tasks.values(), 1)}
    checks: dict[str, tuple[str, str]] = {}
    for matter in matters:
        if matter.kind != "failed":
            continue
        for crit in state.tasks[matter.task_id].dod:
            if crit.status is CriterionStatus.FAIL:
                checks[f"C{len(checks) + 1}"] = (matter.task_id, crit.id)
    return Handles(tasks, checks)


def brief(state: RunState, matters: list[Matter], handles: Handles, tree: Path,
          files_in: Callable[[str], set[str]], max_attempts: int) -> str:
    """What the coordinator is shown: the request, the plan, what is open, what it did before."""
    h = handles.of
    plan = "\n".join(_task_line(state, task, h, max_attempts) for task in state.tasks.values()
                     if task.status not in (TaskStatus.REJECTED, TaskStatus.PROPOSED))
    crit_handle = {v: k for k, v in handles.checks.items()}
    sections = []
    for n, matter in enumerate(matters, 1):
        task = state.tasks.get(matter.task_id)
        if matter.kind == "failed" and task is not None:
            lines = [f"### {n}. {h(task.id)} {task.title!r} failed its checks "
                     f"(attempt {task.attempts} of {max_attempts})"]
            for crit in task.dod:
                if crit.status is not CriterionStatus.FAIL:
                    continue
                how = f" `{crit.command}`" if crit.command else ""
                lines.append(f"- {crit_handle[(task.id, crit.id)]} [{crit.method.value}]{how} "
                             f"-- {crit.statement}\n  What came back (from "
                             f"{crit.verified_by or 'the harness'}):\n"
                             + _indent(_tail(crit.evidence)))
                lines += _files(state, tree, files_in(crit.evidence), h)
            sections.append("\n".join(lines))
        elif matter.kind == "stopped" and task is not None:
            turn = stop_turn(state, task)
            account = (turn.blocked_on if turn is not None else "") or "(it gave no reason)"
            needs = turn.needs if turn is not None else ""
            waiting = turn.waiting_for if turn is not None else ""
            lines = [f"### {n}. {h(task.id)} {task.title!r} stopped "
                     f"(attempt {task.attempts} of {max_attempts})",
                     f"It says it needs: {needs or 'not said'}"
                     + (f"; waiting for: {waiting!r}" if waiting else ""),
                     "Its account:\n" + _indent(_tail(account)),
                     "(A task that stopped has no failed check of its own to move.)"]
            lines += _files(state, tree, files_in(account), h)
            sections.append("\n".join(lines))
        elif matter.kind == "finding":
            message = next(m for m in state.messages if m.id == matter.message_id)
            sender = (f"{h(task.id)} {task.title!r}'s agent" if task is not None
                      else f"agent {message.sender}")
            said = " -- ".join(p for p in (message.subject, message.content) if p)
            lines = [f"### {n}. A {message.kind.value} from {sender}",
                     _indent(_tail(said))]
            lines += _files(state, tree, files_in(said), h)
            sections.append("\n".join(lines))
    earlier = _earlier(state, h)
    menu = "\n".join(f"- `{name}`: {meaning}" for name, meaning in MENU.items())
    return (
        f"# What came back, and what happens next\n\n"
        f"## The request\n{state.prompt.strip()[:3000]}\n\n"
        f"## The plan\n{plan}\n\n"
        f"## What is open\n\n" + "\n\n".join(sections) + "\n\n"
        + (f"## What you decided before\n{earlier}\n\n" if earlier else "")
        + f"## What you may do\n{menu}\n\n"
        "Name tasks as T1, T2, ... and failed checks as C1, C2, ..., exactly as above. "
        "Each open matter needs a pick: a failed task one of send_back, wait or "
        "leave_failed, unless every check it failed is moved; a stopped task one of "
        "send_back, wait, as_it_stands, ask_owner or leave_failed. A finding needs a "
        "pick only if it shows a task's work is wrong."
    )


def _task_line(state: RunState, task: ExecutionTask, h: Callable[[str], str],
               max_attempts: int) -> str:
    deps = ", ".join(h(d) for d in task.depends_on if d in state.tasks) or "none"
    scope = ", ".join(task.scope.paths) or "the whole workspace"
    action = " ".join((task.action or task.title).split())
    if len(action) > ACTION_CHARS:
        action = action[:ACTION_CHARS].rsplit(" ", 1)[0] + " ..."
    return (f"- {h(task.id)} **{task.title}** -- {task.status.value}, attempt "
            f"{task.attempts} of {max_attempts}; after: {deps}; may change: {scope}\n"
            f"  {action}")


def _files(state: RunState, tree: Path, named: Iterable[str],
           h: Callable[[str], str]) -> list[str]:
    lines = []
    for rel in sorted(named):
        wrote = writers(state, tree, rel)
        holders = [h(t.id) for t in state.tasks.values() if matches_any(rel, t.scope.paths)]
        who = (", then ".join(f"{h(t)} (attempt {a})" for t, a in wrote)
               if wrote else "no agent of this run")
        lines.append(f"  - `{rel}`: written by {who}"
                     + (f"; in the scope of {', '.join(holders)}" if holders else ""))
    return (["  Files it names:", *lines] if lines else [])


def _earlier(state: RunState, h: Callable[[str], str]) -> str:
    lines = []
    for consultation in state.coordination:
        for act in consultation.actions:
            task = state.tasks.get(act.get("task", ""))
            if task is None:
                continue
            target = state.tasks.get(act.get("to_task", ""))
            to = f" to {h(target.id)}" if target is not None else ""
            lines.append(f"- {act['do']} {h(task.id)}{to} (attempt {act.get('attempt')}): "
                         f"{' '.join(act.get('message', '').split())[:200]} -> {h(task.id)} is now "
                         f"{task.status.value}" + (f", {h(target.id)} {target.status.value}"
                                                   if target is not None else ""))
    return "\n".join(lines)


def _tail(text: str) -> str:
    text = _ANSI.sub("", text).strip()
    return text if len(text) <= EVIDENCE_CHARS else "... " + text[-EVIDENCE_CHARS:]


def _indent(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines()) or "    (nothing)"


# -- the decide tool, and reading what it picked ---------------------------------------


def decide_tool(handles: Handles) -> dict[str, Any]:
    tasks = list(handles.tasks)
    item: dict[str, Any] = {
        "type": "object",
        "properties": {
            "do": {"type": "string", "enum": list(MENU)},
            "task": {"type": "string", "enum": tasks},
            "criterion": {"type": "string", "description": "move_check: the failed check"},
            "to_task": {"type": "string", "description": "move_check and wait: the other task"},
            "message": {"type": "string",
                        "description": "What the task, or the owner, is told"},
            "check": {"type": "string",
                      "description": "send_back: one more thing its verifier must find true"},
        },
        "required": ["do", "task"],
    }
    if handles.checks:
        item["properties"]["criterion"]["enum"] = list(handles.checks)
    return {
        "name": "decide",
        "description": "What happens next for each open matter. Call it once.",
        "parameters": {
            "type": "object",
            "properties": {"actions": {"type": "array", "items": item},
                           "summary": {"type": "string"}},
            "required": ["actions"],
        },
    }


def read_decision(arguments: dict[str, Any] | None, handles: Handles, state: RunState,
                  matters: list[Matter], max_attempts: int,
                  earlier: Iterable[Action] = ()) -> tuple[list[Action], list[str]]:
    """The picks the harness can carry out, and why each other one was refused.

    ``earlier`` are the picks already taken in this consultation, when it is
    asked again: a task one of them settled, or a check one of them moved, is
    not picked for twice. Run 22's second ask was told a stopped task already
    sent back was "waiting for the owner's answer".
    """
    earlier = list(earlier)
    actions: list[Action] = []
    refused: list[str] = []
    open_tasks = {m.task_id: m.kind for m in matters if m.kind != "finding"}
    disposed = {a.task_id for a in earlier if a.do in _DISPOSITIONS}
    moved = {a.criterion_id for a in earlier if a.do == "move_check"}
    raw = (arguments or {}).get("actions") or []
    picks = [p for p in raw if isinstance(p, dict)] if isinstance(raw, list) else []
    # Work that will run again after this decision, for `wait` to wait on.
    will_run = {handles.tasks.get(str(p.get("to_task" if p.get("do") == "move_check"
                                             else "task", "")).strip(), "")
                for p in picks if p.get("do") in ("send_back", "move_check")}
    will_run |= {a.to_task_id if a.do == "move_check" else a.task_id for a in earlier
                 if a.do in ("send_back", "move_check")}
    for pick in picks:
        do = str(pick.get("do", "")).strip()
        name = str(pick.get("task", "")).strip()
        said = f"{do} {name}".strip()
        task_id = handles.tasks.get(name)
        if do not in MENU:
            refused.append(f"{said}: `{do}` is not one of {', '.join(MENU)}")
            continue
        if task_id is None:
            refused.append(f"{said}: there is no task {name!r}; name it as T1, T2, ...")
            continue
        task = state.tasks[task_id]
        message = str(pick.get("message", "")).strip()
        if do in _DISPOSITIONS and task_id in disposed:
            refused.append(f"{said}: {name} already has a pick in this decision")
            continue
        check = handles.checks.get(str(pick.get("criterion", "")).strip())
        if do == "move_check" and check is not None and check[1] in moved:
            refused.append(f"{said}: that check is moved already")
            continue
        problem = _problem(do, pick, task, handles, state, open_tasks, will_run, max_attempts)
        if problem:
            refused.append(f"{said}: {problem}")
            continue
        action = Action(do, task_id, message=message,
                        check=str(pick.get("check", "")).strip() if do == "send_back" else "")
        if do == "move_check" and check is not None:
            action.criterion_id = check[1]
            moved.add(check[1])
        if do in ("move_check", "wait"):
            action.to_task_id = handles.tasks[str(pick.get("to_task", "")).strip()]
        if do in _DISPOSITIONS:
            disposed.add(task_id)
        actions.append(action)
    return actions, refused


def _problem(do: str, pick: dict[str, Any], task: ExecutionTask, handles: Handles,
             state: RunState, open_tasks: dict[str, str], will_run: set[str],
             max_attempts: int) -> str:
    """Why the harness cannot carry out a pick, or ``""``."""
    kind = open_tasks.get(task.id)
    if do == "send_back":
        return _cannot_send_back(task, kind, max_attempts)
    if do == "move_check":
        if kind == "stopped":
            return ("it stopped, so it has no failed check to move. Judge it as_it_stands, and "
                    "send_back the task whose work needs the fix, saying what")
        return _cannot_move(pick, task, handles, state, max_attempts)
    if kind is None:
        return "it is not one of the open matters"
    if do == "wait":
        return _cannot_wait(pick, task, handles, state, will_run)
    if do == "as_it_stands" and kind != "stopped":
        return "only a task that stopped is judged as it stands"
    return ""


def _cannot_send_back(task: ExecutionTask, kind: str | None, max_attempts: int) -> str:
    if task.status in (TaskStatus.REJECTED, TaskStatus.DEFERRED):
        return f"it is {task.status.value}"
    if task.status is TaskStatus.FAILED and task.attempts >= max_attempts:
        return f"it has had all {max_attempts} attempts at its own work; leave_failed, or ask_owner"
    if task.status is TaskStatus.BLOCKED and kind != "stopped":
        return "it is waiting for the owner's answer"
    if task.status in (TaskStatus.IN_PROGRESS, TaskStatus.AWAITING_VERIFICATION):
        return "it is being worked on or checked now"
    return ""


def _cannot_move(pick: dict[str, Any], task: ExecutionTask, handles: Handles, state: RunState,
                 max_attempts: int) -> str:
    crit = handles.checks.get(str(pick.get("criterion", "")).strip())
    if crit is None:
        return "name the failed check as C1, C2, ..., as listed"
    if crit[0] != task.id:
        return f"that check is {handles.of(crit[0])}'s, not this task's"
    target_id = handles.tasks.get(str(pick.get("to_task", "")).strip())
    if target_id is None:
        return "name the task the check goes to, as T1, T2, ..."
    if target_id == task.id:
        return "a check moves to another task"
    target = state.tasks[target_id]
    if target.status in (TaskStatus.REJECTED, TaskStatus.DEFERRED):
        return f"{handles.of(target_id)} is {target.status.value}"
    if target.status is TaskStatus.FAILED and target.attempts >= max_attempts:
        return f"{handles.of(target_id)} has had all its attempts"
    return ""


def _cannot_wait(pick: dict[str, Any], task: ExecutionTask, handles: Handles, state: RunState,
                 will_run: set[str]) -> str:
    target_id = handles.tasks.get(str(pick.get("to_task", "")).strip())
    if target_id is None or target_id == task.id:
        return "name the other task it waits for, as T1, T2, ..."
    target = state.tasks[target_id]
    unsettled = target.status in (TaskStatus.PROPOSED, TaskStatus.APPROVED,
                                  TaskStatus.IN_PROGRESS, TaskStatus.AWAITING_VERIFICATION)
    if not unsettled and target_id not in will_run:
        return (f"{handles.of(target_id)} is {target.status.value} and is not being sent "
                "back: there is nothing to wait for")
    if waits_on(state, target, task.id):
        return f"{handles.of(target_id)} waits on this task, so it would wait for ever"
    return ""


def waits_on(state: RunState, task: ExecutionTask, other_id: str) -> bool:
    """Whether ``task`` depends on ``other_id``, directly or through other tasks."""
    seen: set[str] = set()
    stack = list(task.depends_on)
    while stack:
        current = stack.pop()
        if current == other_id:
            return True
        if current not in seen:
            seen.add(current)
            stack.extend(state.tasks[current].depends_on if current in state.tasks else [])
    return False


def undecided(state: RunState, matters: list[Matter], actions: list[Action]) -> list[Matter]:
    """The open tasks no pick settles: no disposition, and a failed check not moved."""
    disposed = {a.task_id for a in actions if a.do in _DISPOSITIONS}
    moved = {a.criterion_id for a in actions if a.do == "move_check"}
    out = []
    for matter in matters:
        if matter.kind == "finding" or matter.task_id in disposed:
            continue
        task = state.tasks[matter.task_id]
        if matter.kind == "failed" and all(
                c.id in moved for c in task.dod if c.status is CriterionStatus.FAIL):
            continue
        out.append(matter)
    return out


def fallback(state: RunState, matters: list[Matter], max_attempts: int) -> list[Action]:
    """What happens when the coordinator cannot be asked, or left a matter without a pick.

    The behaviour before #112: a failed task is followed up as itself while its
    attempts last, then left failed; a stopped one goes to the owner. A finding
    is on the record and changes nothing.
    """
    out = []
    for matter in matters:
        task = state.tasks.get(matter.task_id)
        if task is None or matter.kind == "finding":
            continue
        if matter.kind == "failed":
            out.append(Action("send_back" if task.attempts < max_attempts else "leave_failed",
                              task.id))
        else:
            out.append(Action("ask_owner", task.id))
    return out
