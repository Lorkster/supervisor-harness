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
    Artifact,
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
    RunMode,
    RunState,
    Usage,
)
from ..serde import from_jsonable, to_jsonable


class EventType(StrEnum):
    RUN_CREATED = "run_created"
    PHASE_CHANGED = "phase_changed"
    HOST_AGENTS_DECLARED = "host_agents_declared"
    RUN_MODE_SET = "run_mode_set"
    CONTEXT_SET = "context_set"
    BRIEF_RENDERED = "brief_rendered"
    AGENT_SPAWNED = "agent_spawned"
    AGENT_DISPATCHED = "agent_dispatched"
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
    #: Never emitted: what a type this build does not define is read back as,
    #: so the log line survives the read. See :func:`event_from_dict`.
    UNKNOWN = "unknown"


#: Payload key carrying the type name a log line used, on an event read as
#: :attr:`EventType.UNKNOWN`. The fold reports that name rather than the
#: sentinel, so the state names the type the log actually contains.
UNRECOGNISED_TYPE_KEY = "_unrecognised_type"

_KNOWN_TYPES = frozenset(t.value for t in EventType)


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

    def orphan(target: str) -> None:
        """Record an event whose branch exists but whose target does not.

        A no-op here is indistinguishable from an event that had nothing to do,
        which is what made a log disagreeing with itself fold to a state that
        looked complete. Deduplicated by type and target, so a run that keeps
        reporting against one absent agent records it once.
        """
        _remember(state.orphaned_events, f"{t} -> {target}")

    if t is EventType.RUN_CREATED:
        # Merge the genesis record's identity and configuration into the
        # accumulator rather than replacing it: a second RUN_CREATED anywhere in
        # the log must not discard the history folded before it, and fold()'s
        # initial state must survive it. Phase is left to PHASE_CHANGED and
        # RUN_ENDED, which are the events that actually move it.
        genesis = from_jsonable(p.get("run", {}), RunState)
        state.id = genesis.id
        state.prompt = genesis.prompt
        state.workspace = genesis.workspace
        state.mode = genesis.mode
        state.backend = genesis.backend
        state.host = genesis.host
        state.created_at = genesis.created_at

    elif t is EventType.PHASE_CHANGED:
        state.phase = Phase(p["phase"])
        if p.get("error"):
            state.error = p["error"]

    elif t is EventType.HOST_AGENTS_DECLARED:
        state.host_agents = list(p.get("agents") or [])

    elif t is EventType.RUN_MODE_SET:
        state.mode = RunMode(p["mode"])

    elif t is EventType.CONTEXT_SET:
        if p.get("shared_context") is not None:
            state.shared_context = str(p["shared_context"])
        for key, value in (p.get("facts") or {}).items():
            state.facts[str(key)] = str(value)

    elif t is EventType.BRIEF_RENDERED:
        state.briefs[p["agent_id"]] = str(p.get("brief", ""))

    elif t is EventType.AGENT_SPAWNED:
        spec = from_jsonable(p["agent"], AgentSpec)
        state.agents[spec.id] = spec
        state.turn_counts.setdefault(spec.id, 0)

    elif t is EventType.AGENT_DISPATCHED:
        # A packet went out to the host for this agent. Counted, because a host
        # agent that never answers is otherwise indistinguishable from one still
        # working, and the count is what the abandonment bound is measured on.
        agent = state.agents.get(p["agent_id"])
        if agent is not None:
            agent.unreported_dispatches += 1
            if not agent.unreported_since:
                agent.unreported_since = event.ts
        else:
            orphan(p["agent_id"])

    elif t is EventType.AGENT_STATUS:
        agent = state.agents.get(p["agent_id"])
        if agent is not None:
            agent.status = AgentStatus(p["status"])
        else:
            orphan(p["agent_id"])

    elif t is EventType.TURN_RECORDED:
        turn = from_jsonable(p["turn"], AgentTurn)
        state.turn_counts[turn.agent_id] = state.turn_counts.get(turn.agent_id, 0) + 1
        prior = state.usage.get(turn.agent_id, Usage())
        state.usage[turn.agent_id] = prior.add(turn.usage)
        # The agent answered, so it is not silent: the abandonment bound starts
        # again from the next packet it is handed.
        agent = state.agents.get(turn.agent_id)
        if agent is not None:
            agent.unreported_dispatches = 0
            agent.unreported_since = ""

    elif t is EventType.FINDING_ADDED:
        _upsert(state.findings, from_jsonable(p["finding"], Finding))

    elif t is EventType.DIRECTIVE_ISSUED:
        _upsert(state.directives, from_jsonable(p["directive"], Directive))

    elif t is EventType.DRIFT_ASSESSED:
        state.drift[p["agent_id"]] = from_jsonable(p["assessment"], DriftAssessment)

    elif t is EventType.MESSAGE_SENT:
        _upsert(state.messages, from_jsonable(p["message"], Message))

    elif t is EventType.MESSAGE_DELIVERED:
        ids = set(p.get("message_ids", []))
        recipient = p.get("agent_id", "")
        for msg in state.messages:
            if msg.id in ids and recipient and recipient not in msg.delivered_to:
                msg.delivered_to.append(recipient)

    elif t is EventType.TASK_PROPOSED:
        task = from_jsonable(p["task"], ExecutionTask)
        state.tasks[task.id] = task

    elif t in (EventType.TASK_DECIDED, EventType.TASK_UPDATED):
        task = from_jsonable(p["task"], ExecutionTask)
        state.tasks[task.id] = task

    elif t is EventType.CRITERION_VERIFIED:
        task = state.tasks.get(p["task_id"])
        if task is None:
            orphan(p["task_id"])
        elif not any(crit.id == p["criterion_id"] for crit in task.dod):
            # The task is here but this criterion is not: a definition of done
            # that was replaced after the verdict was recorded. Worth naming
            # separately, since the task existing makes it look accounted for.
            orphan(f"{p['task_id']}/{p['criterion_id']}")
        else:
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
        _upsert(state.checkpoints, checkpoint)
        # The high-water mark, not the last one seen. Assignment let a replayed
        # or out-of-order checkpoint move the counter backwards, and the
        # remediation budget is bounded on it -- so an iteration folded twice, or
        # a log read in a different order, silently bought the run another round
        # of remediation it had already spent.
        state.checkpoint_iteration = max(state.checkpoint_iteration, checkpoint.iteration)

    elif t is EventType.LESSON_LEARNED:
        _upsert(state.lessons, from_jsonable(p["lesson"], Lesson))

    elif t is EventType.ARTIFACT_WRITTEN:
        artifact = Artifact(
            path=str(p.get("path", "")),
            kind=str(p.get("kind", "")),
            actor=event.actor,
            ts=event.ts,
        )
        # The report artifact is rewritten on every improvement iteration, so
        # keep one entry per path: the latest write is the one that survives.
        state.artifacts = [a for a in state.artifacts if a.path != artifact.path]
        state.artifacts.append(artifact)

    elif t is EventType.NOTE:
        pass  # audit-only: a note carries nothing the state projects

    elif t is EventType.RUN_ENDED:
        state.phase = Phase(p.get("phase", Phase.COMPLETE))
        if p.get("error"):
            state.error = p["error"]

    else:
        # No branch for this type. Record it rather than drop it, so an event
        # type added later -- or misspelled -- is visible in the folded state.
        # A type read off disk arrives as UNKNOWN carrying its own name, which
        # is the name worth reporting.
        name = str(p.get(UNRECOGNISED_TYPE_KEY) or t)
        if name not in state.unhandled_events:
            state.unhandled_events.append(name)

    state.updated_at = event.ts
    return state


def _remember(bucket: list[str], entry: str) -> None:
    """Record a diagnostic once. Deduplicated, so a repeat costs nothing."""
    if entry not in bucket:
        bucket.append(entry)


def _upsert(items: list[Any], new: Any) -> None:
    """Place an item in a list by id, replacing an earlier copy of the same id.

    The fold was idempotent per event in its dict branches, which assign by key,
    and not in its list branches, which appended unconditionally. That asymmetry
    was per-branch rather than by design: replaying a log -- which ``reindex``,
    ``RunSession.reload`` and every second reader do routinely -- duplicated
    every finding, directive, message, checkpoint and lesson in it, while leaving
    agents and tasks correct. An event applied twice now says exactly what it
    said once.
    """
    ident = getattr(new, "id", None)
    if ident:
        for index, existing in enumerate(items):
            if getattr(existing, "id", None) == ident:
                items[index] = new
                return
    items.append(new)


def fold(events: list[Event], initial: RunState | None = None) -> RunState:
    """Rebuild run state by replaying events in order.

    One event's failure is contained to that event. The fold used to apply every
    record with nothing around it, so a single malformed payload -- a phase name
    this build does not define, a task that will not deserialise -- raised out of
    ``fold``, out of ``RunStore.open`` and out of every command that reads a run.
    The log is append-only, so that state was permanent: the run could never be
    opened again, and the events after the bad one were unreachable even though
    they were intact.

    Skipping the event is the lesser loss, and it is recorded rather than
    swallowed: :attr:`RunState.rejected_events` carries what failed and why, and
    ``supervisor status`` reports it. Nothing here repairs the log -- the record
    stays on disk exactly as written, because it is the audit trail and this is
    only its projection.
    """
    state = initial or RunState()
    for event in sorted(events, key=lambda e: e.seq):
        state = _apply_contained(state, event)
    return state


def _apply_contained(state: RunState, event: Event) -> RunState:
    """Apply one event, recording rather than raising when it cannot be applied.

    Used by :func:`fold` and by ``RunSession.emit``, so a payload that cannot be
    projected costs the same either way: the live session stays usable, and the
    run can still be reopened afterwards. The event itself is already on disk by
    the time this runs and stays there -- the log is the audit trail, and this is
    only its projection.
    """
    try:
        return _apply(state, event)
    except Exception as exc:  # noqa: BLE001 - one bad record must not end the replay
        _remember(
            state.rejected_events,
            f"{event.type} ({exc.__class__.__name__}: {exc})",
        )
        return state


def event_to_dict(event: Event) -> dict[str, Any]:
    return to_jsonable(event)


def event_from_dict(data: dict[str, Any]) -> Event:
    """Rebuild an event, keeping one whose type this build does not define.

    Enum coercion raises for an unknown type, and :meth:`EventLog.read` treats
    a raise as a torn line and skips it -- so a misspelled or future type in a
    real log never reached the fold's unhandled branch at all: it was dropped a
    layer below the code that exists to record it. Such a line is read as
    :attr:`EventType.UNKNOWN` instead, carrying the name it actually used, so
    the event is folded, counted and visible rather than silently absent.
    """
    raw = data.get("type")
    if isinstance(raw, str) and raw not in _KNOWN_TYPES:
        payload = data.get("payload")
        payload = dict(payload) if isinstance(payload, dict) else {}
        payload[UNRECOGNISED_TYPE_KEY] = raw
        data = {**data, "type": EventType.UNKNOWN.value, "payload": payload}
    return from_jsonable(data, Event)
