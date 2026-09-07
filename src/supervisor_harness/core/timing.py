"""Where a run's wall clock actually went.

A supervised run is slow, and that is partly inherent: it is twenty-five to
forty sequential sub-agent invocations where an unsupervised workflow is three
to five. What was not inherent was being unable to say *where* the time went. No
timing was recorded anywhere, so "it is slow" could not be attributed to a
phase, a lens or a provider, and there was nothing to aim an improvement at.

Nothing new is written for this. Every event already carries a timestamp, so the
answer is a projection over the log in the same way `core/journal.py` is: no new
events, no model call, and nothing at run time to keep in step. Storing a
duration alongside the events that already imply it would be a second source of
truth for a number that can only be derived.

## What is measured, and what that means

A **dispatch** is the span from a packet being handed out to the answer coming
back. In host mode that is almost entirely the host's own sub-agent working, not
the harness -- which is the point: it is the number that says whether a run is
slow because the harness is thinking or because thirty sub-agents each took
forty seconds.

A **phase** is the span between `PHASE_CHANGED` events, and includes everything:
dispatches, the caller's own deliberation, and any time the run sat idle waiting
for a host that had wandered off. A phase far longer than the dispatches inside
it is the signature of that last case.

**Waiting** is a dispatch with no answer yet. Reported separately and never
folded into a total, because an agent that will never report would otherwise
inflate the run's elapsed time for ever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..ids import now_iso
from ..store.events import Event, EventType


def _parsed(ts: str) -> datetime | None:
    """A timestamp from the log, or ``None`` if it cannot be read.

    Never raises. A projection that dies on one malformed timestamp is worse
    than one that reports a slightly shorter run: the log is authoritative for
    what happened, and this is a convenience over it.
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _elapsed(start: str, end: str) -> float:
    a, b = _parsed(start), _parsed(end)
    if a is None or b is None:
        return 0.0
    return max(0.0, (b - a).total_seconds())


@dataclass
class Span:
    """One measured interval, with enough identity to attribute it."""

    label: str
    seconds: float
    agent_id: str = ""
    kind: str = ""


@dataclass
class Timing:
    """Where a run's time went, as far as the log can say."""

    total_seconds: float = 0.0
    by_phase: dict[str, float] = field(default_factory=dict)
    by_kind: dict[str, float] = field(default_factory=dict)
    dispatches: list[Span] = field(default_factory=list)
    waiting: list[Span] = field(default_factory=list)

    @property
    def dispatched_seconds(self) -> float:
        """Time spent inside answered dispatches."""
        return sum(s.seconds for s in self.dispatches)

    @property
    def slowest(self) -> Span | None:
        return max(self.dispatches, key=lambda s: s.seconds, default=None)

    def summary(self) -> str:
        """One line: how long, and how much of it was agents working."""
        if not self.total_seconds:
            return "no elapsed time recorded"
        share = self.dispatched_seconds / self.total_seconds
        parts = [
            f"{clock(self.total_seconds)} elapsed",
            f"{clock(self.dispatched_seconds)} in {len(self.dispatches)} dispatch(es)"
            f" ({share:.0%})",
        ]
        slowest = self.slowest
        if slowest is not None:
            parts.append(f"slowest {slowest.label} at {clock(slowest.seconds)}")
        if self.waiting:
            parts.append(f"{len(self.waiting)} still out")
        return "; ".join(parts)

    def to_payload(self) -> dict[str, object]:
        return {
            "total_seconds": round(self.total_seconds, 2),
            "dispatched_seconds": round(self.dispatched_seconds, 2),
            "by_phase": {k: round(v, 2) for k, v in self.by_phase.items()},
            "by_kind": {k: round(v, 2) for k, v in self.by_kind.items()},
            "slowest": (
                {"label": self.slowest.label, "seconds": round(self.slowest.seconds, 2)}
                if self.slowest
                else None
            ),
            "waiting": [
                {"agent_id": s.agent_id, "label": s.label, "seconds": round(s.seconds, 2)}
                for s in self.waiting
            ],
        }


def clock(seconds: float) -> str:
    """Durations as someone reads them out loud, not as a float."""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def measure(events: list[Event], *, now: str = "") -> Timing:
    """Fold the log into where the time went.

    ``now`` is injectable so that a test can measure a finished run without its
    own clock deciding the answer, and so a live run's open dispatches are
    measured against one instant rather than against a different one each.
    """
    timing = Timing()
    if not events:
        return timing

    now = now or now_iso()
    first, last = events[0].ts, events[-1].ts
    timing.total_seconds = _elapsed(first, last)

    # An open dispatch per agent. A second dispatch to the same agent before it
    # answered replaces the first: the packet was re-issued, and the span that
    # matters is the one the answer will land against.
    open_dispatch: dict[str, tuple[str, str]] = {}   # agent_id -> (ts, kind)
    phase_start, phase_name = first, ""

    for event in events:
        if event.type is EventType.PHASE_CHANGED:
            if phase_name:
                timing.by_phase[phase_name] = (
                    timing.by_phase.get(phase_name, 0.0) + _elapsed(phase_start, event.ts)
                )
            phase_name = str(event.payload.get("phase", ""))
            phase_start = event.ts
        elif event.type is EventType.AGENT_DISPATCHED:
            agent_id = str(event.payload.get("agent_id", ""))
            if agent_id:
                open_dispatch[agent_id] = (event.ts, str(event.payload.get("kind", "")))
        elif event.type is EventType.TURN_RECORDED:
            turn = event.payload.get("turn") or {}
            agent_id = str(turn.get("agent_id", "")) if isinstance(turn, dict) else ""
            started = open_dispatch.pop(agent_id, None)
            if started is None:
                continue
            seconds = _elapsed(started[0], event.ts)
            kind = started[1] or "agent"
            timing.dispatches.append(
                Span(label=f"{kind} {agent_id}", seconds=seconds, agent_id=agent_id, kind=kind)
            )
            timing.by_kind[kind] = timing.by_kind.get(kind, 0.0) + seconds

    if phase_name:
        timing.by_phase[phase_name] = (
            timing.by_phase.get(phase_name, 0.0) + _elapsed(phase_start, last)
        )

    # Measured against `now` rather than the last event: an agent that has not
    # answered has been out since it was dispatched, and the log stops moving
    # precisely because it is the thing everyone is waiting for.
    for agent_id, (ts, kind) in open_dispatch.items():
        timing.waiting.append(
            Span(
                label=f"{kind or 'agent'} {agent_id}",
                seconds=_elapsed(ts, now),
                agent_id=agent_id,
                kind=kind,
            )
        )
    timing.waiting.sort(key=lambda s: -s.seconds)
    return timing


__all__ = ["Span", "Timing", "clock", "measure"]
