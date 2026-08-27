"""Event types and the fold that projects them into a :class:`RunState`.

The append-only event log is authoritative. Anything the harness needs in order
to resume, audit or learn from a run must be expressible as an event here --
never as a side effect written straight into the state snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..ids import new_id, now_iso
from ..models import (
    AgentSpec,
    AgentStatus,
    AgentTurn,
    Checkpoint,
    CriterionStatus,
    Directive,
    DriftAssessment,
    ExecutionTask,
    Finding,
    Lesson,
    Message,
    Phase,
    Report,
    RunState,
    Usage,
)
from ..serde import from_jsonable, to_jsonable


class EventType(StrEnum):
    RUN_CREATED = "run_created"
    PHASE_CHANGED = "phase_changed"
    HOST_AGENTS_DECLARED = "host_agents_declared"
    AGENT_SPAWNED = "agent_spawned"
    AGENT_STATUS = "agent_status"
    TURN_RECORDED = "turn_recorded"
    FINDING_ADDED = "finding_added"
    DIRECTIVE_ISSUED = "directive_issued"
    DRIFT_ASSESSED = "drift_assessed"
    MESSAGE_SENT = "message_sent"
    MESSAGE_DELIVERED = "message_delivered"
    TASK_PROPOSED = "task_proposed"
    TASK_DECIDED = "task_decided"
    TASK_UPDATED = "task_updated"
    CRITERION_VERIFIED = "criterion_verified"
    REPORT_WRITTEN = "report_written"
    CHECKPOINT_RECORDED = "checkpoint_recorded"
    LESSON_LEARNED = "lesson_learned"
    ARTIFACT_WRITTEN = "artifact_written"
    NOTE = "note"
    RUN_ENDED = "run_ended"


@dataclass
class Event:
    """One immutable fact about a run."""

    seq: int = 0
    id: str = field(default_factory=lambda: new_id("evt"))
    run_id: str = ""
    type: EventType = EventType.NOTE
    actor: str = "supervisor"
    ts: str = field(default_factory=now_iso)
    payload: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Fold
# --------------------------------------------------------------------------


def _apply(state: RunState, event: Event) -> RunState:  # noqa: C901 - a dispatch table by nature
    p = event.payload
    t = event.type

    if t is EventType.RUN_CREATED:
        state = from_jsonable(p.get("run", {}), RunState)

    elif t is EventType.PHASE_CHANGED:
        state.phase = Phase(p["phase"])
        if p.get("error"):
            state.error = p["error"]

    elif t is EventType.HOST_AGENTS_DECLARED:
        state.host_agents = list(p.get("agents") or [])

    elif t is EventType.AGENT_SPAWNED:
        spec = from_jsonable(p["agent"], AgentSpec)
        state.agents[spec.id] = spec
        state.turn_counts.setdefault(spec.id, 0)

    elif t is EventType.AGENT_STATUS:
        agent = state.agents.get(p["agent_id"])
        if agent is not None:
            agent.status = AgentStatus(p["status"])

    elif t is EventType.TURN_RECORDED:
        turn = from_jsonable(p["turn"], AgentTurn)
        state.turn_counts[turn.agent_id] = state.turn_counts.get(turn.agent_id, 0) + 1
        prior = state.usage.get(turn.agent_id, Usage())
        state.usage[turn.agent_id] = prior.add(turn.usage)

    elif t is EventType.FINDING_ADDED:
        state.findings.append(from_jsonable(p["finding"], Finding))

    elif t is EventType.DIRECTIVE_ISSUED:
        state.directives.append(from_jsonable(p["directive"], Directive))

    elif t is EventType.DRIFT_ASSESSED:
        state.drift[p["agent_id"]] = from_jsonable(p["assessment"], DriftAssessment)

    elif t is EventType.MESSAGE_SENT:
        state.messages.append(from_jsonable(p["message"], Message))

    elif t is EventType.MESSAGE_DELIVERED:
        ids = set(p.get("message_ids", []))
        for msg in state.messages:
            if msg.id in ids:
                msg.delivered = True

    elif t is EventType.TASK_PROPOSED:
        task = from_jsonable(p["task"], ExecutionTask)
        state.tasks[task.id] = task

    elif t in (EventType.TASK_DECIDED, EventType.TASK_UPDATED):
        task = from_jsonable(p["task"], ExecutionTask)
        state.tasks[task.id] = task

    elif t is EventType.CRITERION_VERIFIED:
        task = state.tasks.get(p["task_id"])
        if task is not None:
            for crit in task.dod:
                if crit.id == p["criterion_id"]:
                    crit.status = CriterionStatus(p["status"])
                    crit.evidence = p.get("evidence", "")
                    crit.verified_at = event.ts
                    crit.verified_by = event.actor
                    break

    elif t is EventType.REPORT_WRITTEN:
        state.report = from_jsonable(p["report"], Report)

    elif t is EventType.CHECKPOINT_RECORDED:
        checkpoint = from_jsonable(p["checkpoint"], Checkpoint)
        state.checkpoints.append(checkpoint)
        state.checkpoint_iteration = checkpoint.iteration

    elif t is EventType.LESSON_LEARNED:
        state.lessons.append(from_jsonable(p["lesson"], Lesson))

    elif t is EventType.RUN_ENDED:
        state.phase = Phase(p.get("phase", Phase.COMPLETE))
        if p.get("error"):
            state.error = p["error"]

    state.updated_at = event.ts
    return state


def fold(events: list[Event], initial: RunState | None = None) -> RunState:
    """Rebuild run state by replaying events in order."""
    state = initial or RunState()
    for event in sorted(events, key=lambda e: e.seq):
        state = _apply(state, event)
    return state


def event_to_dict(event: Event) -> dict[str, Any]:
    return to_jsonable(event)


def event_from_dict(data: dict[str, Any]) -> Event:
    return from_jsonable(data, Event)
