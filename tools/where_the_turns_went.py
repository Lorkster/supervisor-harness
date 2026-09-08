"""Why a run's agents ran out of turns, printed from its event log and nothing else.

    python tools/where_the_turns_went.py <workspace>/.supervisor/runs/<id>/events.jsonl

The companion to `where_the_time_went.py`, under the same promise and for the
same situation: a run on a machine whose data cannot leave it. That one answers
"where did the wall clock go". This one answers "why did agents stop having
turns left", which is a different failure and was not visible anywhere.

## What it prints, and what it refuses to

**Counts, agent kinds, directive kinds, statuses and phase names. Nothing else.**
No prompt, no brief, no finding, no file path, no agent id, no role.

Agent ids are left out on purpose even though they are random: an id is a join
key back to the run, and the point of this output is that it can be pasted into
a public issue. Agents appear as `analysis#1`, `analysis#2` -- their kind and
the order they were spawned in.

Roles are left out for a stronger reason. A lens's role is chosen by the
planning model out of the user's own prompt, so `role` can be any text at all,
and printing it would put a model's paraphrase of private work on the screen.
Kind is a fixed vocabulary; role is not.

## The columns

**sent** is how many packets were handed out to the agent, and **turns** is how
many came back, against the agent's budget. Equal turns and budget are an agent
that used everything it had, which is the case this tool is for. `sent` well
above `turns` is the other stall: a packet issued over and over to an agent that
never answered it, which costs no budget at all and looks identical from
outside.

**repeat** counts turns whose reported content is byte-for-byte a turn this
agent already reported. The harness cannot reject those: the turn contract
carries no turn identifier, so a second report of the same turn and a genuine
second turn are the same message (see `_stale_report_reason` in
`core/supervisor.py`). A budget consumed by repeats is an orchestrator reporting
work twice -- after a context compaction, most likely, where it lost track of
what it had already handed back -- and not an agent that needed more turns. The
content is hashed, compared, and thrown away; nothing derived from it is
printed but a count.

**said-done** counts turns where the agent claimed a terminal status for itself.
An agent that said it had finished and was driven again spent the rest of its
budget on a conversation it thought was over.

**directives** is what the supervisor sent back, in order, as a tally. This is
the column that separates the two ways a budget is spent: `continue x5` is an
agent that kept being told it was fine and was never accepted, while
`refocus x2, reject x1` is a supervisor that kept sending work back. They are
different bugs in different places.

## What it cannot tell you

Whether the turns were *useful*. Two turns that differ by a word are two turns
here, and the tool has no opinion about which of them was worth its budget.
"""

from __future__ import annotations

import collections
import hashlib
import json
import pathlib
import sys

#: Every field this tool reads out of a payload, as its contract. The promise
#: above is only as good as this list, and a reviewer should be able to check it
#: against the code in a minute.
READS = (
    "phase",
    "agent.id", "agent.kind", "agent.budget.max_turns",
    "agent_dispatched.agent_id",
    "turn.agent_id", "turn.claimed_status", "turn.output", "turn.reasoning",
    "directive.agent_id", "directive.kind",
    "agent_id", "status",
)

#: Statuses an agent claims when it believes it is finished with the work. The
#: harness's own list of the opposite (`ACTIVE_AGENT_STATUSES`) is not imported,
#: because this file must run where the package is not installed.
SETTLED = ("done", "blocked", "failed", "stopped")


def _payload(event: dict[str, object]) -> dict[str, object]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _sub(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _digest(turn: dict[str, object]) -> str:
    """A fingerprint of what an agent reported, so repeats can be counted.

    One way, and never printed. The alternative -- holding the outputs and
    comparing them -- would put the run's actual work in this process's memory
    for no gain, and one careless print away from the screen.
    """
    material = f"{turn.get('output', '')}\x00{turn.get('reasoning', '')}"
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()


class Agent:
    """One agent's turn history, as far as the log can say."""

    def __init__(self, label: str, budget: int) -> None:
        self.label = label
        self.budget = budget
        self.sent = 0
        self.turns = 0
        self.repeats = 0
        self.said_done = 0
        self.status = "unknown"
        self.directives: list[str] = []
        self._seen: set[str] = set()

    def record(self, turn: dict[str, object]) -> None:
        self.turns += 1
        digest = _digest(turn)
        if digest in self._seen:
            self.repeats += 1
        self._seen.add(digest)
        if str(turn.get("claimed_status", "")) in SETTLED:
            self.said_done += 1

    @property
    def exhausted(self) -> bool:
        return bool(self.budget) and self.turns >= self.budget


def fold(events: list[dict[str, object]]) -> tuple[dict[str, Agent], str]:
    """Every agent's turn history, keyed by id, plus the phase it ended in.

    Ids are the key and never the label: `Agent.label` is the kind and an
    ordinal, assigned in spawn order.
    """
    agents: dict[str, Agent] = {}
    seen_kinds: collections.Counter[str] = collections.Counter()
    phase = ""

    for event in events:
        kind_of_event = str(event.get("type", ""))
        payload = _payload(event)

        if kind_of_event == "phase_changed":
            phase = str(payload.get("phase", "")) or phase

        elif kind_of_event == "agent_spawned":
            spec = _sub(payload, "agent")
            agent_id = str(spec.get("id", ""))
            if not agent_id or agent_id in agents:
                continue
            kind = str(spec.get("kind", "")) or "agent"
            seen_kinds[kind] += 1
            budget = _sub(spec, "budget").get("max_turns", 0)
            agents[agent_id] = Agent(
                label=f"{kind}#{seen_kinds[kind]}",
                budget=int(budget) if isinstance(budget, int) else 0,
            )

        elif kind_of_event == "agent_dispatched":
            agent = agents.get(str(payload.get("agent_id", "")))
            if agent is not None:
                agent.sent += 1

        elif kind_of_event == "turn_recorded":
            turn = _sub(payload, "turn")
            agent = agents.get(str(turn.get("agent_id", "")))
            if agent is not None:
                agent.record(turn)

        elif kind_of_event == "directive_issued":
            directive = _sub(payload, "directive")
            agent = agents.get(str(directive.get("agent_id", "")))
            if agent is not None:
                agent.directives.append(str(directive.get("kind", "")))

        elif kind_of_event == "agent_status":
            agent = agents.get(str(payload.get("agent_id", "")))
            if agent is not None:
                agent.status = str(payload.get("status", ""))

    return agents, phase


def _tally(kinds: list[str]) -> str:
    """`continue x5, stop` -- what was sent back, in the order it was first sent."""
    counted: dict[str, int] = {}
    for kind in kinds:
        counted[kind] = counted.get(kind, 0) + 1
    return ", ".join(f"{k} x{n}" if n > 1 else k for k, n in counted.items()) or "-"


def read_log(path: pathlib.Path) -> list[dict[str, object]]:
    """Every readable line of an event log, skipping any that is not.

    The log is append-only and may be copied while a run is still writing it, so
    a half-written last line is the ordinary case rather than corruption.
    """
    events: list[dict[str, object]] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def summarise(events: list[dict[str, object]]) -> list[str]:
    """The report, as lines. Pure: it prints nothing and reads no files."""
    if not events:
        return ["the log is empty"]

    agents, phase = fold(events)
    ordered = list(agents.values())
    turns = sum(a.turns for a in ordered)
    repeats = sum(a.repeats for a in ordered)
    exhausted = [a for a in ordered if a.exhausted]

    out = [
        f"events            {len(events)}",
        f"last phase        {phase or 'unknown'}",
        f"agents            {len(ordered)}",
        f"turns recorded    {turns}",
        f"repeat turns      {repeats}  identical to an earlier turn of the same agent",
        f"out of turns      {len(exhausted)}  agent(s) that used the whole budget",
    ]
    if exhausted:
        wasted = sum(a.repeats for a in exhausted)
        out.append(
            f"  of their turns  {wasted} of {sum(a.turns for a in exhausted)} were repeats"
        )

    unanswered = [a for a in ordered if a.sent > a.turns]
    if unanswered:
        out.append(
            f"never answered    {len(unanswered)}  agent(s) handed a packet back "
            "that never returned"
        )

    out += ["", "by agent            sent   turns  repeat  said-done  status      directives"]
    for agent in ordered:
        budget = str(agent.budget) if agent.budget else "-"
        out.append(
            f"  {agent.label:<18} {agent.sent:>3} {agent.turns:>5}/{budget:<3} "
            f"{agent.repeats:>6} {agent.said_done:>10}  {agent.status:<10}  "
            f"{_tally(agent.directives)}"
        )

    every: list[str] = []
    for agent in ordered:
        every += agent.directives
    if every:
        out += ["", f"directives        {_tally(every)}"]
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    path = pathlib.Path(argv[1])
    if not path.is_file():
        print(f"no such event log: {path}", file=sys.stderr)
        return 2
    for line in summarise(read_log(path)):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
