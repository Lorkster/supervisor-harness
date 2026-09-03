"""Event types and the fold that projects them into a :class:`RunState`.

The append-only event log is authoritative. Anything the harness needs in order
to resume, audit or learn from a run must be expressible as an event here --
never as a side effect written straight into the state snapshot.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, fields
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
    Fact,
    Finding,
    Lesson,
    Message,
    Note,
    Phase,
    Report,
    RunMode,
    RunState,
    ScopeEnvelope,
    Usage,
)
from ..serde import from_jsonable, to_jsonable


class EventType(StrEnum):
    RUN_CREATED = "run_created"
    PHASE_CHANGED = "phase_changed"
    HOST_AGENTS_DECLARED = "host_agents_declared"
    RUN_MODE_SET = "run_mode_set"
    ENVELOPE_SET = "envelope_set"
    CONTEXT_SET = "context_set"
    FACT_ESTABLISHED = "fact_established"
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


# A dispatch table by nature, and the longest function in the codebase: 123
# statements across 48 branches. Recorded as finding Q-A2 rather than
# suppressed -- see docs/quality-assessment.md.
def _orphan(state: RunState, event_type: EventType, target: str) -> None:
    """Record an event whose branch exists but whose target does not.

    A no-op here is indistinguishable from an event that had nothing to do,
    which is what made a log disagreeing with itself fold to a state that
    looked complete. Deduplicated by type and target, so a run that keeps
    reporting against one absent agent records it once.
    """
    _remember(state.orphaned_events, f"{event_type} -> {target}")


def _on_run_created(state: RunState, event: Event) -> None:
    p = event.payload
    genesis = from_jsonable(p.get("run", {}), RunState)
    state.id = genesis.id
    state.prompt = genesis.prompt
    state.workspace = genesis.workspace
    state.mode = genesis.mode
    state.backend = genesis.backend
    state.host = genesis.host
    state.created_at = genesis.created_at


def _on_phase_changed(state: RunState, event: Event) -> None:
    p = event.payload
    state.phase = Phase(p["phase"])
    if p.get("error"):
        state.error = p["error"]


def _on_host_agents_declared(state: RunState, event: Event) -> None:
    p = event.payload
    state.host_agents = list(p.get("agents") or [])


def _on_run_mode_set(state: RunState, event: Event) -> None:
    p = event.payload
    state.mode = RunMode(p["mode"])


def _on_envelope_set(state: RunState, event: Event) -> None:
    p = event.payload
    state.envelope = from_jsonable(p["envelope"], ScopeEnvelope)


def _on_context_set(state: RunState, event: Event) -> None:
    p = event.payload
    if p.get("shared_context") is not None:
        state.shared_context = str(p["shared_context"])
    for key, value in (p.get("facts") or {}).items():
        state.facts[str(key)] = str(value)


def _on_fact_established(state: RunState, event: Event) -> None:
    p = event.payload
    fact = from_jsonable(p["fact"], Fact)
    if not any(
        f.key == fact.key and f.statement == fact.statement
        and f.agent_id == fact.agent_id
        for f in state.established
    ):
        state.established.append(fact)


def _on_brief_rendered(state: RunState, event: Event) -> None:
    p = event.payload
    state.briefs[p["agent_id"]] = str(p.get("brief", ""))


def _on_agent_spawned(state: RunState, event: Event) -> None:
    p = event.payload
    spec = from_jsonable(p["agent"], AgentSpec)
    seen = state.agents.get(spec.id)
    state.agents[spec.id] = _merge_into(seen, spec) if seen is not None else spec
    state.turn_counts.setdefault(spec.id, 0)


def _on_agent_dispatched(state: RunState, event: Event) -> None:
    p = event.payload
    agent = state.agents.get(p["agent_id"])
    if agent is not None:
        agent.unreported_dispatches += 1
        if not agent.unreported_since:
            agent.unreported_since = event.ts
    else:
        _orphan(state, event.type, p["agent_id"])


def _on_agent_status(state: RunState, event: Event) -> None:
    p = event.payload
    agent = state.agents.get(p["agent_id"])
    if agent is not None:
        agent.status = AgentStatus(p["status"])
    else:
        _orphan(state, event.type, p["agent_id"])


def _on_turn_recorded(state: RunState, event: Event) -> None:
    p = event.payload
    turn = from_jsonable(p["turn"], AgentTurn)
    # The body, not only the tally. Kept whole, including the turn's own
    # findings and messages: they are projected separately as well, by the
    # FINDING_ADDED and MESSAGE_SENT events the same turn emits, so this
    # duplicates them -- but a half-populated ``AgentTurn`` is a trap for
    # whoever reads one next, and the log holds the whole thing either way.
    first_time = _upsert(state.turns, turn)
    if first_time:
        # Both of these are running totals, so they are the two things in
        # this branch a replay could double. The list above cannot be, and
        # the resets below are assignments; these needed the guard.
        state.turn_counts[turn.agent_id] = state.turn_counts.get(turn.agent_id, 0) + 1
        prior = state.usage.get(turn.agent_id, Usage())
        state.usage[turn.agent_id] = prior.add(turn.usage)
    # The agent answered, so it is not silent: the abandonment bound starts
    # again from the next packet it is handed.
    agent = state.agents.get(turn.agent_id)
    if agent is not None:
        agent.unreported_dispatches = 0
        agent.unreported_since = ""


def _on_finding_added(state: RunState, event: Event) -> None:
    p = event.payload
    _upsert(state.findings, from_jsonable(p["finding"], Finding))


def _on_directive_issued(state: RunState, event: Event) -> None:
    p = event.payload
    _upsert(state.directives, from_jsonable(p["directive"], Directive))


def _on_drift_assessed(state: RunState, event: Event) -> None:
    p = event.payload
    state.drift[p["agent_id"]] = from_jsonable(p["assessment"], DriftAssessment)


def _on_message_sent(state: RunState, event: Event) -> None:
    p = event.payload
    _upsert(state.messages, from_jsonable(p["message"], Message))


def _on_message_delivered(state: RunState, event: Event) -> None:
    p = event.payload
    ids = set(p.get("message_ids", []))
    recipient = p.get("agent_id", "")
    for msg in state.messages:
        if msg.id in ids and recipient and recipient not in msg.delivered_to:
            msg.delivered_to.append(recipient)


def _on_task_proposed(state: RunState, event: Event) -> None:
    p = event.payload
    task = from_jsonable(p["task"], ExecutionTask)
    seen_task = state.tasks.get(task.id)
    state.tasks[task.id] = _merge_into(seen_task, task) if seen_task is not None else task
    if p.get("notes"):
        state.task_notes[task.id] = [str(n) for n in p["notes"]]


def _on_task_decided_or_task_updated(state: RunState, event: Event) -> None:
    p = event.payload
    task = from_jsonable(p["task"], ExecutionTask)
    seen_task = state.tasks.get(task.id)
    state.tasks[task.id] = _merge_into(seen_task, task) if seen_task is not None else task


def _on_criterion_verified(state: RunState, event: Event) -> None:
    p = event.payload
    verified = state.tasks.get(p["task_id"])
    if verified is None:
        _orphan(state, event.type, p["task_id"])
    elif not any(crit.id == p["criterion_id"] for crit in verified.dod):
        # The task is here but this criterion is not: a definition of done
        # that was replaced after the verdict was recorded. Worth naming
        # separately, since the task existing makes it look accounted for.
        _orphan(state, event.type, f"{p['task_id']}/{p['criterion_id']}")
    else:
        for crit in verified.dod:
            if crit.id == p["criterion_id"]:
                crit.status = CriterionStatus(p["status"])
                crit.evidence = p.get("evidence", "")
                crit.verified_at = event.ts
                crit.verified_by = event.actor
                break


def _on_report_written(state: RunState, event: Event) -> None:
    p = event.payload
    state.report = from_jsonable(p["report"], Report)


def _on_checkpoint_recorded(state: RunState, event: Event) -> None:
    p = event.payload
    checkpoint = from_jsonable(p["checkpoint"], Checkpoint)
    _upsert(state.checkpoints, checkpoint)
    # The high-water mark, not the last one seen. Assignment let a replayed
    # or out-of-order checkpoint move the counter backwards, and the
    # remediation budget is bounded on it -- so an iteration folded twice, or
    # a log read in a different order, silently bought the run another round
    # of remediation it had already spent.
    state.checkpoint_iteration = max(state.checkpoint_iteration, checkpoint.iteration)


def _on_lesson_learned(state: RunState, event: Event) -> None:
    p = event.payload
    _upsert(state.lessons, from_jsonable(p["lesson"], Lesson))


def _on_artifact_written(state: RunState, event: Event) -> None:
    p = event.payload
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


def _on_note(state: RunState, event: Event) -> None:
    p = event.payload
    _upsert(
        state.notes,
        Note(
            id=event.id,
            text=str(p.get("text", "")),
            actor=event.actor,
            ts=event.ts,
            context={k: str(v) for k, v in p.items() if k != "text"},
        ),
    )


def _on_run_ended(state: RunState, event: Event) -> None:
    p = event.payload
    state.phase = Phase(p.get("phase", Phase.COMPLETE))
    if p.get("error"):
        state.error = p["error"]


#: Which handler answers for each event type. A dispatch table rather than
#: a 24-way `elif` chain: the fold used to be one 123-statement function
#: with a cyclomatic complexity of 46, and adding an event type meant
#: finding the right place in the middle of it (finding Q-A2).
#:
#: The table is checked against `EventType` by a test, so a member added
#: without a handler is a failure rather than a silent no-op.
_HANDLERS: dict[EventType, Callable[[RunState, Event], None]] = {
    EventType.RUN_CREATED: _on_run_created,
    EventType.PHASE_CHANGED: _on_phase_changed,
    EventType.HOST_AGENTS_DECLARED: _on_host_agents_declared,
    EventType.RUN_MODE_SET: _on_run_mode_set,
    EventType.ENVELOPE_SET: _on_envelope_set,
    EventType.CONTEXT_SET: _on_context_set,
    EventType.FACT_ESTABLISHED: _on_fact_established,
    EventType.BRIEF_RENDERED: _on_brief_rendered,
    EventType.AGENT_SPAWNED: _on_agent_spawned,
    EventType.AGENT_DISPATCHED: _on_agent_dispatched,
    EventType.AGENT_STATUS: _on_agent_status,
    EventType.TURN_RECORDED: _on_turn_recorded,
    EventType.FINDING_ADDED: _on_finding_added,
    EventType.DIRECTIVE_ISSUED: _on_directive_issued,
    EventType.DRIFT_ASSESSED: _on_drift_assessed,
    EventType.MESSAGE_SENT: _on_message_sent,
    EventType.MESSAGE_DELIVERED: _on_message_delivered,
    EventType.TASK_PROPOSED: _on_task_proposed,
    EventType.TASK_DECIDED: _on_task_decided_or_task_updated,
    EventType.TASK_UPDATED: _on_task_decided_or_task_updated,
    EventType.CRITERION_VERIFIED: _on_criterion_verified,
    EventType.REPORT_WRITTEN: _on_report_written,
    EventType.CHECKPOINT_RECORDED: _on_checkpoint_recorded,
    EventType.LESSON_LEARNED: _on_lesson_learned,
    EventType.ARTIFACT_WRITTEN: _on_artifact_written,
    EventType.NOTE: _on_note,
    EventType.RUN_ENDED: _on_run_ended,
}


def _apply(state: RunState, event: Event) -> RunState:
    """Apply one event to the accumulating state, in place."""
    handler = _HANDLERS.get(event.type)
    if handler is not None:
        handler(state, event)
    else:
        # No handler for this type. Record it rather than drop it, so an event
        # type added later -- or misspelled -- is visible in the folded state.
        # A type read off disk arrives as UNKNOWN carrying its own name, which
        # is the name worth reporting.
        name = str(event.payload.get(UNRECOGNISED_TYPE_KEY) or event.type)
        if name not in state.unhandled_events:
            state.unhandled_events.append(name)

    state.updated_at = event.ts
    return state


def _remember(bucket: list[str], entry: str) -> None:
    """Record a diagnostic once. Deduplicated, so a repeat costs nothing."""
    if entry not in bucket:
        bucket.append(entry)


def _merge_into(existing: Any, incoming: Any) -> Any:
    """Copy every field of ``incoming`` onto ``existing``, and return ``existing``.

    The dict branches of this fold assigned a freshly deserialised object into
    the map, which detached the caller from what it had just written. Every
    caller in ``core.supervisor`` follows the same shape -- mutate a task or an
    agent, then emit an event describing it -- so after the emit the object it
    held was no longer the object in ``RunState``, and its next mutation went
    somewhere nothing reads. One call site compensated with a re-fetch; the
    others did not, and nothing marked which was which.

    Updating in place makes the two the same object again, so the pattern the
    module docstring describes is what actually happens. A replay is unaffected:
    the first event carrying an id still creates the object, and later ones
    update it, which is what a fold is meant to do.
    """
    for f in fields(type(existing)):
        setattr(existing, f.name, getattr(incoming, f.name))
    return existing


def _upsert(items: list[Any], new: Any) -> bool:
    """Place an item in a list by id, replacing an earlier copy of the same id.

    The fold was idempotent per event in its dict branches, which assign by key,
    and not in its list branches, which appended unconditionally. That asymmetry
    was per-branch rather than by design: replaying a log -- which ``reindex``,
    ``RunStore.open`` and every second reader do routinely -- duplicated
    every finding, directive, message, checkpoint and lesson in it, while leaving
    agents and tasks correct. An event applied twice now says exactly what it
    said once.

    Returns whether this was a new item. Callers that keep a running total
    alongside the list -- turn counts, accumulated usage -- need to know, because
    an increment is not idempotent on its own however careful the list is.
    """
    ident = getattr(new, "id", None)
    if ident:
        for index, existing in enumerate(items):
            if getattr(existing, "id", None) == ident:
                items[index] = new
                return False
    items.append(new)
    return True


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
        state = _apply(state, event)
    except Exception as exc:  # noqa: BLE001 - one bad record must not end the replay
        _remember(
            state.rejected_events,
            f"{event.type} ({exc.__class__.__name__}: {exc})",
        )
    # Advanced whether or not the event applied. The watermark answers "which
    # events has this state seen?", not "which ones did it like": a record that
    # was rejected here will be rejected identically on every replay, so holding
    # the mark back for it would make a current snapshot look permanently stale.
    state.last_seq = max(state.last_seq, event.seq)
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
