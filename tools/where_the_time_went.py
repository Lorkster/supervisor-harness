"""Where a run's wall clock went, printed from its event log and nothing else.

    python tools/where_the_time_went.py <workspace>/.supervisor/runs/<id>/events.jsonl

`supervisor status` already answers this, and answers it better: `core/timing.py`
is the same projection with more in it. This exists for the case that one cannot
serve.

## The case it is for

A run on a machine whose data cannot leave it. The harness's own output carries
the prompt, the findings, the proposed tasks and the report -- a run's actual
work -- so "send me the status JSON" is not a request that can be made of a
corporate laptop, and the useful numbers are buried in something that cannot be
shared wholesale.

This prints **durations, counts, phase names and agent kinds, and nothing else**.
No prompt, no findings, no file paths, no task titles, no evidence. The output
is safe to read out loud, which is the whole point of it: someone can run this
and type the answer back.

Two consequences shape the implementation.

**Standard library only, no harness import.** It has to run on a machine where
this package is not installed, or where the installed build predates the field
being asked about -- `timing` reached `supervisor status` in batch 3, and a log
written before that still contains everything needed to compute it, because the
log has always carried timestamps. Reading the log directly makes the tool
independent of the build that produced it.

**Nothing is read from a payload except an id, a kind and a phase name.** Not
because the rest is uninteresting but because the promise above has to be true
by construction rather than by care.

## What the columns mean

A **dispatch** is a packet handed out until its answer comes back. In
host-delegated mode that is almost entirely the host's sub-agent working.

**idle** is a phase's elapsed time minus the dispatches inside it: the
supervisor's own deliberation, plus any time the run spent waiting on a host
that had gone quiet. A phase whose elapsed time is close to its dispatches was
busy. A phase far above them was waiting, and that difference is the one worth
acting on.

One thing the log cannot say, and that no amount of reading it will fix: both
ends of a dispatch are stamped when the *host* calls the harness. A sub-agent
that thought for seven minutes and a sub-agent that finished in two whose caller
reported at seven are the same two timestamps. Only whoever watched it can tell
those apart.
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys
from datetime import datetime

#: Payload keys this tool reads. Everything else in an event is left unread, so
#: the promise that nothing sensitive is printed holds by construction. Listed
#: rather than implied because it is the tool's contract with whoever runs it.
READS = ("phase", "agent_id", "kind", "turn.agent_id")


def _at(stamp: str) -> datetime | None:
    """One ISO timestamp, or ``None`` when it cannot be read.

    Never raises. A tool that dies on one malformed line is worse than one
    reporting a slightly shorter run: this is a convenience over the record, and
    the record is elsewhere.
    """
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _elapsed(start: str, end: str) -> float:
    first, last = _at(start), _at(end)
    if first is None or last is None:
        return 0.0
    return max(0.0, (last - first).total_seconds())


def read_log(path: pathlib.Path) -> list[dict[str, object]]:
    """Every readable line of an event log, skipping any that is not.

    A truncated final line is the ordinary case here: the log is append-only and
    may be copied while a run is still writing to it.
    """
    events: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
    return events


def summarise(events: list[dict[str, object]]) -> list[str]:
    """The report, as lines. Pure: it prints nothing and reads no files."""
    if not events:
        return ["the log is empty"]

    def stamp(event: dict[str, object]) -> str:
        return str(event.get("ts", ""))

    def payload(event: dict[str, object]) -> dict[str, object]:
        raw = event.get("payload")
        return raw if isinstance(raw, dict) else {}

    phase, phase_at = "", stamp(events[0])
    # A plain dict rather than a Counter: these are seconds, and Counter is
    # typed to int, which mypy catches before the rounding does.
    phases: dict[str, float] = {}
    # agent id -> (dispatched at, kind, the phase it was dispatched in)
    open_dispatch: dict[str, tuple[str, str, str]] = {}
    spans: list[tuple[str, str, float]] = []

    for event in events:
        body, now = payload(event), stamp(event)
        kind = str(event.get("type", ""))
        if kind == "phase_changed":
            if phase:
                phases[phase] = phases.get(phase, 0.0) + _elapsed(phase_at, now)
            phase, phase_at = str(body.get("phase", "")), now
        elif kind == "agent_dispatched":
            agent_id = str(body.get("agent_id", ""))
            if agent_id:
                open_dispatch[agent_id] = (now, str(body.get("kind", "?")), phase)
        elif kind == "turn_recorded":
            turn = body.get("turn")
            agent_id = str(turn.get("agent_id", "")) if isinstance(turn, dict) else ""
            started = open_dispatch.pop(agent_id, None)
            if started is not None:
                spans.append((started[1], started[2], _elapsed(started[0], now)))
    if phase:
        phases[phase] = phases.get(phase, 0.0) + _elapsed(phase_at, stamp(events[-1]))

    total = _elapsed(stamp(events[0]), stamp(events[-1]))
    dispatched = sum(span[2] for span in spans)
    out = [
        f"events            {len(events)}",
        f"total elapsed     {total:8.1f}s",
        f"in dispatches     {dispatched:8.1f}s  across {len(spans)}",
        f"never answered    {len(open_dispatch)}",
        "",
        "by phase                 elapsed   in dispatches      idle",
    ]
    for name, elapsed in sorted(phases.items(), key=lambda kv: -kv[1]):
        inside = sum(span[2] for span in spans if span[1] == name)
        out.append(f"  {name:<20} {elapsed:8.1f}s {inside:10.1f}s {elapsed - inside:9.1f}s")

    out += ["", "by kind             total    n    slowest"]
    by_kind: dict[str, list[float]] = collections.defaultdict(list)
    for agent_kind, _, seconds in spans:
        by_kind[agent_kind].append(seconds)
    for agent_kind, seconds_list in sorted(by_kind.items(), key=lambda kv: -sum(kv[1])):
        out.append(
            f"  {agent_kind:<16} {sum(seconds_list):7.1f}s "
            f"{len(seconds_list):4d} {max(seconds_list):9.1f}s"
        )
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    path = pathlib.Path(argv[1])
    if not path.is_file():
        print(f"no such event log: {path}", file=sys.stderr)
        return 2
    print("\n".join(summarise(read_log(path))))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main(sys.argv))
