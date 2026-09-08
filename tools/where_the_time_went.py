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

**busy** is the wall time covered by at least one dispatch, and **idle** is
what is left of the phase: the supervisor's own deliberation, plus any time the
run spent waiting on a host that had gone quiet.

**agent-s** is the sum of the dispatches, which is a different quantity and the
reason this table has both. Two agents working for five minutes each is ten
agent-seconds over five minutes busy. The ratio of the two is **conc**, the
average number of agents actually running at once -- and it is the column that
says whether a fan-out fanned out. A phase that dispatched four independent
lenses and reports `conc 1.0` ran them one after another, whatever it was asked
to do, and that is worth far more than any amount of idle time.

The first version of this tool printed `elapsed - sum(dispatches)` as idle,
which is fine until dispatches overlap and then goes negative -- reporting
parallelism as a mystery. The union is the honest figure and the sum is the
useful one beside it.

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


def _union(spans: list[tuple[str, str, str, str]]) -> float:
    """Wall time covered by at least one of these dispatches.

    Not their sum. Two agents working for five minutes each is ten
    agent-seconds over five minutes of wall clock, and subtracting the sum from
    a phase's elapsed time -- which is what this tool did first -- reports that
    as minus five minutes of idle.
    """
    intervals = sorted(
        (start, end) for _, _, start, end in spans if _at(start) and _at(end)
    )
    covered, cursor = 0.0, ""
    for start, end in intervals:
        begin = start if not cursor or start > cursor else cursor
        if not cursor or end > cursor:
            covered += _elapsed(begin, end) if end > begin else 0.0
            cursor = end
    return covered


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


def _stamp(event: dict[str, object]) -> str:
    return str(event.get("ts", ""))


def _payload(event: dict[str, object]) -> dict[str, object]:
    raw = event.get("payload")
    return raw if isinstance(raw, dict) else {}


def fold(
    events: list[dict[str, object]],
) -> tuple[dict[str, float], list[tuple[str, str, str, str]], int]:
    """Phase durations, dispatch spans, and how many packets are still out.

    Separate from the rendering because the two change for different reasons --
    and because together they tripped the complexity gate this project holds
    itself to, which is the sort of thing that gate is for.

    A dispatch is attributed to the phase it was *handed out* in, even if its
    answer lands in the next one. That is the honest attribution: the phase that
    asked for the work owns the time, and a run that spends ten minutes in
    `analyzing` should say so whether or not the last lens replied after the
    phase had moved on.
    """
    phase, phase_at = "", _stamp(events[0])
    phase_spans: list[tuple[str, str, str]] = []      # phase, start, end
    # agent id -> (dispatched at, kind, the phase it was dispatched in)
    open_dispatch: dict[str, tuple[str, str, str]] = {}
    spans: list[tuple[str, str, str, str]] = []       # kind, phase, start, end

    for event in events:
        body, now = _payload(event), _stamp(event)
        kind = str(event.get("type", ""))
        if kind == "phase_changed":
            if phase:
                phase_spans.append((phase, phase_at, now))
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
                spans.append((started[1], started[2], started[0], now))
    if phase:
        phase_spans.append((phase, phase_at, _stamp(events[-1])))

    phases: dict[str, float] = {}
    for name, start, end in phase_spans:
        phases[name] = phases.get(name, 0.0) + _elapsed(start, end)
    return phases, spans, len(open_dispatch)


def summarise(events: list[dict[str, object]]) -> list[str]:
    """The report, as lines. Pure: it prints nothing and reads no files."""
    if not events:
        return ["the log is empty"]

    phases, spans, unanswered = fold(events)
    total = _elapsed(_stamp(events[0]), _stamp(events[-1]))
    agent_seconds = sum(_elapsed(s[2], s[3]) for s in spans)
    out = [
        f"events            {len(events)}",
        f"total elapsed     {total:8.1f}s",
        f"busy              {_union(spans):8.1f}s  wall time with at least one agent out",
        f"agent-seconds     {agent_seconds:8.1f}s  across {len(spans)} dispatch(es)",
        f"never answered    {unanswered}",
        "",
        "by phase                 elapsed      busy      idle   agent-s   conc",
    ]
    for name, elapsed in sorted(phases.items(), key=lambda kv: -kv[1]):
        mine = [s for s in spans if s[1] == name]
        busy = _union(mine)
        seconds = sum(_elapsed(s[2], s[3]) for s in mine)
        conc = seconds / busy if busy else 0.0
        out.append(
            f"  {name:<20} {elapsed:8.1f}s {busy:8.1f}s {max(0.0, elapsed - busy):8.1f}s "
            f"{seconds:8.1f}s {conc:6.2f}"
        )

    out += ["", "by kind             total    n    slowest"]
    by_kind: dict[str, list[float]] = collections.defaultdict(list)
    for agent_kind, _, start, end in spans:
        by_kind[agent_kind].append(_elapsed(start, end))
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
