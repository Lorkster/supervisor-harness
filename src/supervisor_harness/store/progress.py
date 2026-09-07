"""A line per event, for someone watching a run from another terminal.

The harness only wakes when it is called, so it cannot push progress to whoever
started it. What it can do is leave a trail that something else follows:

    tail -f .supervisor/runs/<run_id>/progress.ndjson

That is the whole design. It is the inverse of a producer that pushes lines into
an agent -- here the run writes and a person reads -- and it is the cheapest
answer available to "it is slow and I cannot see what is going on", because it
needs no protocol, no port, and nothing from the host.

## Why not just read the event log

``events.jsonl`` is already a line per event and is already tailable. It is also
the authoritative record: full payloads, whole briefs, every finding in full.
Following it means watching thousands of characters scroll past to learn that a
lens finished. This is the same sequence with the payloads reduced to one
sentence each -- derived, disposable, and safe to delete.

Which is the other half of the reason it is a separate file: a reader that
truncates or rotates what it is tailing must not be truncating the record.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .events import Event, EventType

#: What each event says in one line. Anything absent from this map is still
#: written -- with its type as the note -- because a new event type silently
#: vanishing from the progress view is worse than one that reads a bit bare.
_NOTES: dict[EventType, str] = {
    EventType.RUN_CREATED: "run created",
    EventType.PHASE_CHANGED: "phase",
    EventType.AGENT_SPAWNED: "agent briefed",
    EventType.AGENT_DISPATCHED: "dispatched, waiting",
    EventType.TURN_RECORDED: "turn reported",
    EventType.DIRECTIVE_ISSUED: "directive",
    EventType.DRIFT_ASSESSED: "drift assessed",
    EventType.ASSISTS_RECORDED: "harness repaired the answer",
    EventType.FINDING_ADDED: "finding",
    EventType.MESSAGE_SENT: "message",
    EventType.TASK_PROPOSED: "task proposed",
    EventType.TASK_DECIDED: "task decided",
    EventType.TASK_UPDATED: "task updated",
    EventType.LESSON_LEARNED: "lesson",
    EventType.NOTE: "note",
}


def _detail(event: Event) -> str:
    """The one thing worth knowing about this event, beyond its type.

    Deliberately shallow. This is a status line, so it reaches one level into
    the payload for the field a reader is actually following and stops there --
    a progress view that renders a whole finding is the log again.
    """
    p = event.payload
    if event.type is EventType.PHASE_CHANGED:
        return str(p.get("phase", ""))
    if event.type in (EventType.AGENT_DISPATCHED, EventType.AGENT_SPAWNED):
        return str(p.get("kind") or p.get("agent_id", ""))
    if event.type is EventType.DIRECTIVE_ISSUED:
        directive = p.get("directive")
        return str(directive.get("kind", "")) if isinstance(directive, dict) else ""
    if event.type is EventType.DRIFT_ASSESSED:
        assessment = p.get("assessment")
        return f"score {assessment.get('score')}" if isinstance(assessment, dict) else ""
    if event.type is EventType.ASSISTS_RECORDED:
        return f"{p.get('total', 0)} repair(s)"
    if event.type is EventType.FINDING_ADDED:
        finding = p.get("finding")
        return str(finding.get("title", ""))[:80] if isinstance(finding, dict) else ""
    if event.type is EventType.NOTE:
        return str(p.get("text", ""))[:120]
    if event.type is EventType.TURN_RECORDED:
        turn = p.get("turn")
        return str(turn.get("claimed_status", ""))if isinstance(turn, dict) else ""
    return ""


def as_line(event: Event) -> dict[str, Any]:
    """One event reduced to what a watcher needs."""
    return {
        "seq": event.seq,
        "ts": event.ts,
        "type": str(event.type),
        "actor": event.actor,
        "note": _NOTES.get(event.type, str(event.type)),
        "detail": _detail(event),
    }


def append(path: Path, events: list[Event]) -> None:
    """Append these events' progress lines. Never raises.

    Swallowing here is deliberate and is the one place in the store where that
    is the right call: this file is derived and disposable, and a run that
    cannot write a progress line has still recorded everything that matters.
    Letting it fail a run would trade the authoritative record for the
    convenience over it.
    """
    if not events:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(as_line(event), ensure_ascii=False) + "\n")
    except OSError:
        return


__all__ = ["append", "as_line"]
