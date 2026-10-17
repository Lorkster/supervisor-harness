"""Turn the moments a recorded run's reports came back into coordinator cases (batch O).

    python tools/extract_coordinator_cases.py SPEC.json --home HOME --out DIR

SPEC is a list of cases, each naming a run in HOME's store and a window of its
log: what came back after ``since`` and up to ``seq`` is what the coordinator is
asked about. For example:

    [{"id": "ms-run16-board-ignored", "run": "run_01M4YA2X71JZDN",
      "since": 758, "seq": 776, "repo": "${MINESWEEPER}", "commit": "8b0c...",
      "statuses": {"Scaffold project": "verified"},
      "checks": [{"check": "picks",
                  "expect": [[{"do": "send_back", "task": "Implement the page"}]]}],
      "labelled_by": "..."}]

The run's state is folded from its log up to ``seq`` and put in the shape a
coordinated run would have had at that moment:

- a stop the old rules sent to the owner is a stop, not yet a question;
- a check the old rules held for another task (``held_for``) failed;
- what came back before the window is marked as decided, so the case asks about
  its own moment only.

It is then reduced to what the coordinator reads: tasks, the agents and turns
that say who wrote which file and why an agent stopped, the findings sent to the
supervisor, the goal and the requirements.

The tree a run had at that moment is gone: runs commit once, at the end. A
case's fixture is the run's starting commit, which has the brief's documents.
The run's final commit would mislead -- in run 16 it already had the fix the
moment was about, and the coordinator, reading it, rightly found nothing wrong.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from supervisor_harness.core import coordinator
from supervisor_harness.models import (
    AgentSpec,
    AgentTurn,
    Coordination,
    CriterionStatus,
    EscalationReason,
    RunState,
    TaskStatus,
)
from supervisor_harness.serde import to_jsonable
from supervisor_harness.store.events import event_from_dict, fold


def _events(path: Path) -> list[Any]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(event_from_dict(json.loads(line)))
        except (ValueError, KeyError):
            continue
    return out


def at_the_moment(events: list[Any], since: int, seq: int,
                  statuses: dict[str, str] | None = None) -> RunState:
    """The run's state at ``seq``, as a coordinated run would have had it.

    ``statuses`` puts tasks, by title prefix, back to the status they had before
    the old rules acted on the window -- a task a routed finding reopened, say.
    """
    upto = [e for e in events if e.seq <= seq]
    state = fold(upto)
    # A check the old rules held for another task (``held_for``, a field this
    # build no longer has): read off the log, where the task carried it.
    last = {str((e.payload.get("task") or {}).get("id", "")): e.payload["task"]
            for e in upto if e.type == "task_updated"}
    held_ids = {str(c.get("id", "")) for task in last.values()
                for c in task.get("dod") or [] if c.get("held_for")}
    # A finding the old rules routed in the window is the coordinator's to decide:
    # the expectation it became, and its check, are taken back out.
    routed = {str(raw.get("id", "")) for e in upto
              if e.type == "goal_set" and e.seq > since
              for raw in e.payload.get("goal") or []
              if str(raw.get("source", "")).startswith("agent ")}
    state.goal = [g for g in state.goal if g.id not in routed]
    for task in state.tasks.values():
        task.dod = [c for c in task.dod if c.goal_id not in routed]
    for prefix, status in (statuses or {}).items():
        for task in state.tasks.values():
            if task.title.casefold().startswith(prefix.casefold()):
                task.status = TaskStatus(status)
    raised = {str(e.payload["escalation"]["id"]) for e in upto
              if e.type == "escalation_raised" and e.seq > since}
    for esc_id in raised:
        escalation = state.escalations.get(esc_id)
        if (escalation is not None and escalation.resolution is None
                and escalation.reason is EscalationReason.AGENT_BLOCKED):
            del state.escalations[esc_id]
    for task in state.tasks.values():
        held = [c for c in task.dod if c.id in held_ids]
        for crit in held:
            crit.status = CriterionStatus.FAIL
        if held and task.status is TaskStatus.AWAITING_VERIFICATION:
            task.status = TaskStatus.FAILED
    # What came back before the window was decided then.
    changed: dict[str, int] = {}
    for e in upto:
        if e.type in ("task_updated", "task_decided"):
            changed[str((e.payload.get("task") or {}).get("id", ""))] = e.seq
        elif e.type == "criterion_verified":
            changed[str(e.payload.get("task_id", ""))] = e.seq
    sent = {str((e.payload.get("message") or {}).get("id", "")): e.seq
            for e in upto if e.type == "message_sent"}
    before = [m.key for m in coordinator.open_matters(state)
              if (sent.get(m.message_id, 0) if m.kind == "finding"
                  else changed.get(m.task_id, 0)) <= since]
    # A coordinated run keeps what its coordinator decided, which it is shown.
    state.coordination = [*state.coordination, *(
        [Coordination(matters=before, summary="decided before this case")] if before else [])]
    return state


def reduced(state: RunState) -> dict[str, Any]:
    """The state as a case carries it: what the coordinator reads, nothing else."""
    slim = RunState(id=state.id, prompt=state.prompt, tasks=state.tasks, goal=state.goal,
                    requirements=state.requirements, escalations=state.escalations,
                    coordination=state.coordination, phase=state.phase,
                    backend=state.backend, mode=state.mode)
    slim.agents = {a.id: AgentSpec(id=a.id, kind=a.kind, role=a.role, title=a.title,
                                   task_id=a.task_id, attempt=a.attempt, status=a.status)
                   for a in state.agents.values()}
    slim.turns = [AgentTurn(id=t.id, agent_id=t.agent_id, seq=t.seq,
                            files_touched=t.files_touched, claimed_status=t.claimed_status,
                            blocked_on=t.blocked_on, needs=t.needs, waiting_for=t.waiting_for)
                  for t in state.turns if t.files_touched or t.blocked_on]
    slim.messages = [m for m in state.messages
                     if m.recipient == "supervisor" and m.kind in coordinator.FINDING_KINDS]
    data = to_jsonable(slim)
    return data if isinstance(data, dict) else {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", type=Path)
    ap.add_argument("--home", type=Path, required=True, help="the SUPERVISOR_HOME of the runs")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for spec in json.loads(args.spec.read_text(encoding="utf-8")):
        events = _events(args.home / "runs" / spec["run"] / "events.jsonl")
        state = at_the_moment(events, int(spec["since"]), int(spec["seq"]),
                              spec.get("statuses"))
        open_now = coordinator.open_matters(state)
        case = {
            "id": spec["id"], "role": "coordinator",
            "fixture": {"kind": "git", "repo": spec["repo"], "commit": spec["commit"]},
            "request": state.prompt,
            "input": {"state": reduced(state)},
            "checks": [{"check": "answered"}, {"check": "picked_for_each"},
                       *spec.get("checks", [])],
            "labelled_by": spec.get("labelled_by", ""),
        }
        (args.out / f"{spec['id']}.json").write_text(
            json.dumps(case, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"{spec['id']}: {len(open_now)} open -- "
              + ", ".join(f"{m.kind} {state.tasks[m.task_id].title[:40]!r}"
                          if m.task_id in state.tasks else m.kind for m in open_now))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
