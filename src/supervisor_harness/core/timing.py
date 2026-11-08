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

## Busy is not the sum of the dispatches

Dispatches overlap -- that is the point of a fan-out -- so adding them up
measures agent effort, not wall clock. Both numbers are here because they answer
different questions and are routinely confused:

* **busy** is the wall time covered by at least one dispatch. It is bounded by
  the run's elapsed time, and `elapsed - busy` is honest idle.
* **agent seconds** is their sum. Four lenses working five minutes each is
  twenty agent-minutes across five minutes busy, and the ratio of the two is
  **concurrency**: how many agents were really running at once.

This module reported the sum and called it `dispatched_seconds`, which read as
"how much of the run was agents working". Against a real 53-minute run that gave
98.6%, and the true figure was 52.8% -- the same overlap counted four times.
Anything derived from it was wrong in the same direction, including the idle
time, which the offline tool computed as `elapsed - sum` and printed as *minus*
five minutes. A negative duration is at least visible; a plausible percentage is
not, which is why this went unnoticed through a whole batch of work aimed at
exactly this question.

One thing no arithmetic here can recover: both ends of a dispatch are stamped
when the *host* calls the harness. Concurrency above 1 proves the host had
several packets out at once; concurrency of 1 across a fan-out issued in one
breath means the host ran them one at a time, or reported them that way. The
harness cannot see which.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..ids import now_iso, parse_timestamp
from ..store.events import Event, EventType


def _parsed(ts: str) -> datetime | None:
    """A timestamp from the log, or ``None`` if it cannot be read.

    Never raises. A projection that dies on one malformed timestamp is worse
    than one that reports a slightly shorter run: the log is authoritative for
    what happened, and this is a convenience over it.

    Always timezone-aware (a stamp with no zone is read as UTC), through the one
    parser `ids` owns. A naive stamp beside an aware one used to make the
    subtraction in :func:`_elapsed` raise ``TypeError`` from a function that
    promised not to.
    """
    return parse_timestamp(ts)


def _elapsed(start: str, end: str) -> float:
    a, b = _parsed(start), _parsed(end)
    if a is None or b is None:
        return 0.0
    return max(0.0, (b - a).total_seconds())


@dataclass
class Span:
    """One measured interval, with enough identity to attribute it.

    ``start`` and ``end`` are kept alongside the duration because a set of
    durations cannot be unioned and a set of intervals can. Without them the
    only available total is the sum, which is how this module came to report
    one.
    """

    label: str
    seconds: float
    agent_id: str = ""
    kind: str = ""
    start: str = ""
    end: str = ""


def _union_seconds(spans: list[Span]) -> float:
    """Wall time covered by at least one of these spans.

    The overlapping part is counted once, so the result never exceeds the run's
    elapsed time however many agents were out at once. Spans whose timestamps
    could not be read are skipped rather than treated as instantaneous: a span
    at the epoch would swallow the whole run.
    """
    intervals = []
    for span in spans:
        first, last = _parsed(span.start), _parsed(span.end)
        if first is not None and last is not None and last > first:
            intervals.append((first.timestamp(), last.timestamp()))

    covered, cursor = 0.0, float("-inf")
    for start, end in sorted(intervals):
        if end <= cursor:
            continue        # wholly inside a span already counted
        covered += end - max(start, cursor)
        cursor = end
    return covered


@dataclass
class Timing:
    """Where a run's time went, as far as the log can say."""

    total_seconds: float = 0.0
    by_phase: dict[str, float] = field(default_factory=dict)
    by_kind: dict[str, float] = field(default_factory=dict)
    dispatches: list[Span] = field(default_factory=list)
    waiting: list[Span] = field(default_factory=list)

    @property
    def busy_seconds(self) -> float:
        """Wall time with at least one dispatch out. Never above `total_seconds`."""
        return _union_seconds(self.dispatches)

    @property
    def agent_seconds(self) -> float:
        """Effort: the dispatches added up, overlap and all. May exceed the run."""
        return sum(s.seconds for s in self.dispatches)

    @property
    def idle_seconds(self) -> float:
        """Elapsed time with no agent out: the harness thinking, or nobody at all.

        `max(0, ...)` is belt and braces rather than arithmetic. The union is
        bounded by the run's span by construction, and the clamp is here so that
        a log with a timestamp from the future reports zero idle instead of a
        negative duration -- which is what the caller of the old sum printed.
        """
        return max(0.0, self.total_seconds - self.busy_seconds)

    @property
    def concurrency(self) -> float:
        """Agents running at once, averaged over the busy time.

        1.0 across a fan-out means it did not fan out. Above 1 is proof the host
        had several packets out together; at 1 the harness cannot tell a host
        that queued them from one that ran them and reported them in order.
        """
        busy = self.busy_seconds
        return self.agent_seconds / busy if busy else 0.0

    @property
    def slowest(self) -> Span | None:
        return max(self.dispatches, key=lambda s: s.seconds, default=None)

    def summary(self) -> str:
        """One line: how long, and how much of it was agents working."""
        if not self.total_seconds:
            return "no elapsed time recorded"
        share = self.busy_seconds / self.total_seconds
        parts = [
            f"{clock(self.total_seconds)} elapsed",
            f"{clock(self.busy_seconds)} busy across {len(self.dispatches)} dispatch(es)"
            f" ({share:.0%}, {self.concurrency:.2f} at once)",
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
            "busy_seconds": round(self.busy_seconds, 2),
            "idle_seconds": round(self.idle_seconds, 2),
            "agent_seconds": round(self.agent_seconds, 2),
            "concurrency": round(self.concurrency, 2),
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


class TimingTracker:
    """The fold behind :func:`measure`, kept open so it can be fed more events.

    A ledger is stamped on every response, and each one used to re-read and
    re-walk the whole log to say where the time went. The walk is a single pass
    that only looks backwards (an open dispatch, the current phase), so what it
    needs between events is small enough to keep, and a log that has grown by one
    event costs one event.

    Events must arrive in sequence order, as :meth:`EventLog.read_all` yields
    them. ``now`` is applied when a result is asked for, not when events arrive,
    because the only thing that depends on it is how long an unanswered dispatch
    has been out.
    """

    def __init__(self) -> None:
        self.first = ""
        self.last = ""
        self.count = 0
        self._by_phase: dict[str, float] = {}
        self._by_kind: dict[str, float] = {}
        self._dispatches: list[Span] = []
        # An open dispatch per agent. A second dispatch to the same agent before
        # it answered replaces the first: the packet was re-issued, and the span
        # that matters is the one the answer will land against.
        self._open: dict[str, tuple[str, str]] = {}      # agent_id -> (ts, kind)
        self._phase_start = ""
        self._phase_name = ""

    def feed(self, events: list[Event]) -> None:
        for event in events:
            if not self.count:
                self.first = self._phase_start = event.ts
            self.count += 1
            self.last = event.ts
            self._apply(event)

    def _apply(self, event: Event) -> None:
        if event.type is EventType.PHASE_CHANGED:
            if self._phase_name:
                self._by_phase[self._phase_name] = (
                    self._by_phase.get(self._phase_name, 0.0)
                    + _elapsed(self._phase_start, event.ts)
                )
            self._phase_name = str(event.payload.get("phase", ""))
            self._phase_start = event.ts
        elif event.type is EventType.AGENT_DISPATCHED:
            agent_id = str(event.payload.get("agent_id", ""))
            if agent_id:
                self._open[agent_id] = (event.ts, str(event.payload.get("kind", "")))
        elif event.type is EventType.TURN_RECORDED:
            turn = event.payload.get("turn") or {}
            agent_id = str(turn.get("agent_id", "")) if isinstance(turn, dict) else ""
            started = self._open.pop(agent_id, None)
            if started is None:
                return
            seconds = _elapsed(started[0], event.ts)
            kind = started[1] or "agent"
            self._dispatches.append(
                Span(
                    label=f"{kind} {agent_id}",
                    seconds=seconds,
                    agent_id=agent_id,
                    kind=kind,
                    start=started[0],
                    end=event.ts,
                )
            )
            self._by_kind[kind] = self._by_kind.get(kind, 0.0) + seconds

    def result(self, *, now: str = "") -> Timing:
        """Where the time went, as of the events fed so far."""
        timing = Timing()
        if not self.count:
            return timing
        now = now or now_iso()
        timing.total_seconds = _elapsed(self.first, self.last)
        timing.by_phase = dict(self._by_phase)
        timing.by_kind = dict(self._by_kind)
        timing.dispatches = list(self._dispatches)
        if self._phase_name:
            timing.by_phase[self._phase_name] = (
                timing.by_phase.get(self._phase_name, 0.0)
                + _elapsed(self._phase_start, self.last)
            )
        # Measured against `now` rather than the last event: an agent that has not
        # answered has been out since it was dispatched, and the log stops moving
        # precisely because it is the thing everyone is waiting for.
        for agent_id, (ts, kind) in self._open.items():
            timing.waiting.append(
                Span(
                    label=f"{kind or 'agent'} {agent_id}",
                    seconds=_elapsed(ts, now),
                    agent_id=agent_id,
                    kind=kind,
                    start=ts,
                    end=now,
                )
            )
        timing.waiting.sort(key=lambda s: -s.seconds)
        return timing


def measure(events: list[Event], *, now: str = "") -> Timing:
    """Fold the log into where the time went.

    ``now`` is injectable so that a test can measure a finished run without its
    own clock deciding the answer, and so a live run's open dispatches are
    measured against one instant rather than against a different one each.
    """
    tracker = TimingTracker()
    tracker.feed(events)
    return tracker.result(now=now)


__all__ = ["Span", "Timing", "TimingTracker", "clock", "measure"]
