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
many came back, against the agent's budget. `sent` well above `turns` is one
stall: a packet issued over and over to an agent that never answered it, which
costs no budget at all and looks identical from outside.

An agent at its budget is only counted as **out of turns** if it was also never
settled -- no terminal directive, no finishing status. Synthesizers and
verifiers have a budget of one, so counting every agent at its budget made two
thirds of a normal run look like a stall, which is a count that cannot report
the thing it was written for.

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

**drift signals** is the section to read when the directive tally is full of
corrections. A directive says what the supervisor did; the signal says what it
was reacting to -- a scope violation, an objective left uncovered, a turn
repeating the last one. The kinds are fixed strings in `core/drift.py`; the
`detail` beside them names files and is never read here.

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
    "assessment.signals[].kind",
    "agent_id", "status",
)

#: Statuses an agent claims when it believes it is finished with the work. The
#: harness's own list of the opposite (`ACTIVE_AGENT_STATUSES`) is not imported,
#: because this file must run where the package is not installed.
SETTLED = ("done", "blocked", "failed", "stopped")

#: Directives that end an agent. An agent whose last directive is one of these
#: was settled deliberately, however much of its budget it had used.
TERMINAL = ("accept", "stop", "escalate")


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
        self.signals: list[str] = []
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
        """Used the whole budget *and* was never settled.

        The first version of this called any agent at its budget exhausted,
        which made every one-turn agent -- every synthesizer, every verifier --
        a suspect. On the first real log it read that was eight of twenty
        agents, none of which had run out of anything: a budget of one, spent
        once, is the design working. A count that fires on the normal case
        cannot report the abnormal one.

        Settled means a terminal directive, or, for the agents that are never
        issued one, a status that says they finished.
        """
        if not self.budget or self.turns < self.budget:
            return False
        if self.directives:
            return self.directives[-1] not in TERMINAL
        return self.status not in SETTLED

    @property
    def killed(self) -> bool:
        """Stopped by the supervisor rather than by its own budget."""
        return bool(self.directives) and self.directives[-1] == "stop"


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

        else:
            _attach(agents, kind_of_event, payload)

    return agents, phase


def _attach(
    agents: dict[str, Agent], kind_of_event: str, payload: dict[str, object]
) -> None:
    """Fold one agent-scoped event into the agent it names.

    Every branch here shares the same two steps -- find the agent id, find the
    agent -- and an event naming an agent this log never spawned is dropped: a
    log copied mid-run can begin after a spawn, and half an agent's history is
    worse than none of it.
    """
    if kind_of_event == "agent_dispatched":
        agent = agents.get(str(payload.get("agent_id", "")))
        if agent is not None:
            agent.sent += 1
        return

    if kind_of_event == "turn_recorded":
        turn = _sub(payload, "turn")
        agent = agents.get(str(turn.get("agent_id", "")))
        if agent is not None:
            agent.record(turn)
        return

    if kind_of_event == "directive_issued":
        directive = _sub(payload, "directive")
        agent = agents.get(str(directive.get("agent_id", "")))
        if agent is not None:
            agent.directives.append(str(directive.get("kind", "")))
        return

    if kind_of_event == "drift_assessed":
        agent = agents.get(str(payload.get("agent_id", "")))
        signals = _sub(payload, "assessment").get("signals")
        if agent is not None and isinstance(signals, list):
            agent.signals += [
                str(s.get("kind", "")) for s in signals if isinstance(s, dict)
            ]
        return

    if kind_of_event == "agent_status":
        agent = agents.get(str(payload.get("agent_id", "")))
        if agent is not None:
            agent.status = str(payload.get("status", ""))


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

    killed = [a for a in ordered if a.killed]
    out = [
        f"events            {len(events)}",
        f"last phase        {phase or 'unknown'}",
        f"agents            {len(ordered)}",
        f"turns recorded    {turns}",
        f"repeat turns      {repeats}  identical to an earlier turn of the same agent",
        f"out of turns      {len(exhausted)}  agent(s) that spent the budget "
        "without settling",
        f"stopped           {len(killed)}  agent(s) halted by the supervisor",
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

    # Why the corrections were issued, which the directive tally cannot say. A
    # run full of `refocus` is a supervisor that kept sending work back, and the
    # question it raises immediately is what it was reacting to.
    signalled = [a for a in ordered if a.signals]
    if signalled:
        counts: dict[str, list[str]] = {}
        for agent in signalled:
            for signal in agent.signals:
                counts.setdefault(signal, []).append(agent.label)
        out += ["", "drift signals         n   agents"]
        for signal, labels in sorted(counts.items(), key=lambda kv: -len(kv[1])):
            out.append(f"  {signal:<18} {len(labels):>3} {len(set(labels)):>8}")
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
