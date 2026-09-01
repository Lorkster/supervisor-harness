"""The decision journal: why each directive was issued to each agent.

Every input to every supervisory decision has been on the log since batch 5b.
What was missing was a reader that assembles them: nothing answered "why was
*this* directive issued to *this* agent" in one place, so the answer was
reconstructed by hand from `supervisor events` every time anyone needed it.

This is that reader. It is a pure projection -- no new events, no model call,
and nothing here writes anything.

## Why it reads the log rather than ``RunState``

The plan that scheduled this work said all of it was already in ``RunState``.
That is true of the briefs, the turns, the messages and the notes. It is *not*
true of the drift assessments, which is the one input that matters most here:
``RunState.drift`` is a ``dict`` keyed by agent id, so the fold keeps only each
agent's **newest** assessment and every earlier one is overwritten. Measured on
a run with one three-turn agent: twelve assessments on the log, eight in
``RunState``, and the agent that peaked at 0.85 read as 0.4 in the snapshot.

An assessment that has been overwritten cannot explain the directive it
produced. So the journal is built from the event log, which is authoritative
and ordered, and ``RunState`` is used only for the things the fold keeps whole.
That split is the right one anyway: ``status`` answers "where is this run now"
from the snapshot, and ``explain`` answers "how did it get here" from the log.

## How an episode is assembled

A supervised turn appends a contiguous run of events, in this order:

    TURN_RECORDED -> DRIFT_ASSESSED -> DIRECTIVE_ISSUED -> [MESSAGE_DELIVERED]
                                                        -> [NOTE] -> [AGENT_STATUS]

so walking the log in sequence order and attaching each event to the open
episode of the agent it names reconstructs them exactly. Where the payload
carries a ``turn_id`` -- everything written since this module existed -- the
association is a lookup instead, and a mismatch is reported rather than
silently preferred. Both paths are needed: the fallback is what makes a log
recorded by an earlier build explainable at all.

Not every episode has every part. A verifier is assessed but never issued a
directive, because its own verdict settles it. Notes emitted against an agent
before its first turn -- the scope narrowings recorded at spawn -- belong to no
turn at all, and are collected in an opening episode whose ``turn`` is ``None``
rather than being dropped for not fitting the shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import (
    AgentSpec,
    AgentTurn,
    Directive,
    DriftAssessment,
    Message,
    Note,
    RunState,
    ScopeEnvelope,
)
from ..serde import from_jsonable
from ..store.events import Event, EventType


@dataclass
class Episode:
    """One supervised turn, with everything that decided what followed it.

    ``turn`` is ``None`` for the opening episode, which carries whatever was
    recorded against the agent before it had answered anything.
    """

    turn: AgentTurn | None = None
    assessments: list[DriftAssessment] = field(default_factory=list)
    directive: Directive | None = None
    inbox: list[Message] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    status_changes: list[str] = field(default_factory=list)
    # Where the log's order and its ``turn_id`` fields disagree, or where a part
    # is missing that the shape would normally have. Reported, not resolved:
    # a journal that quietly picks one of two answers is worse than one that
    # says the record is ambiguous.
    anomalies: list[str] = field(default_factory=list)

    @property
    def escalated(self) -> bool:
        """Whether a model was asked for a second opinion on this turn."""
        return any(a.checked_by != "heuristics" for a in self.assessments)


@dataclass
class AgentJournal:
    """One agent's whole history, in the order it happened."""

    agent: AgentSpec | None = None
    brief: str = ""
    episodes: list[Episode] = field(default_factory=list)

    @property
    def turns(self) -> int:
        return sum(1 for e in self.episodes if e.turn is not None)


@dataclass
class RunJournal:
    """A run's decisions, by agent."""

    run_id: str = ""
    prompt: str = ""
    phase: str = ""
    mode: str = ""
    envelope: ScopeEnvelope | None = None
    # Every envelope the run established, oldest first, so the provenance chain
    # is readable and not just its last link.
    envelope_history: list[ScopeEnvelope] = field(default_factory=list)
    agents: list[AgentJournal] = field(default_factory=list)
    # Notes belonging to the run rather than to any agent: planning fell back,
    # the envelope was narrowed, an index projection failed.
    run_notes: list[Note] = field(default_factory=list)
    # Events naming an agent the log never spawned. The fold reports these too,
    # under `orphaned_events`; counted here so a journal missing an agent's
    # episodes says why.
    unattributed: int = 0


def build_journal(
    state: RunState, events: list[Event], agent_id: str = ""
) -> RunJournal:
    """Assemble the journal for a run, or for one agent of it.

    ``state`` supplies the agents, their briefs and the run's own facts;
    ``events`` supplies the decisions, because the fold does not keep them all.
    Filtering by ``agent_id`` narrows the episodes, never the run-level facts --
    an agent's scope is only meaningful beside the envelope above it.
    """
    journal = RunJournal(
        run_id=state.id,
        prompt=state.prompt,
        phase=str(state.phase),
        mode=str(state.mode),
        envelope=state.envelope,
    )

    wanted = {agent_id} if agent_id else set(state.agents)
    journals: dict[str, AgentJournal] = {
        aid: AgentJournal(agent=state.agents.get(aid), brief=state.briefs.get(aid, ""))
        for aid in wanted
        if aid in state.agents
    }
    # The episode currently open per agent, so an event attaches to the turn it
    # followed rather than to the agent as a whole.
    open_episode: dict[str, Episode] = {}
    messages: dict[str, Message] = {}

    def episode_for(aid: str) -> Episode | None:
        """The open episode for ``aid``, opening the pre-turn one if needed."""
        entry = journals.get(aid)
        if entry is None:
            return None
        current = open_episode.get(aid)
        if current is None:
            current = Episode()
            entry.episodes.append(current)
            open_episode[aid] = current
        return current

    for event in sorted(events, key=lambda e: e.seq):
        payload = event.payload
        kind = event.type

        if kind is EventType.ENVELOPE_SET:
            journal.envelope_history.append(
                from_jsonable(payload.get("envelope", {}), ScopeEnvelope)
            )

        elif kind is EventType.MESSAGE_SENT:
            message = from_jsonable(payload["message"], Message)
            messages[message.id] = message

        elif kind is EventType.TURN_RECORDED:
            turn = from_jsonable(payload["turn"], AgentTurn)
            entry = journals.get(turn.agent_id)
            if entry is None:
                journal.unattributed += 1
                continue
            current = open_episode.get(turn.agent_id)
            # An opening episode that recorded nothing is an artefact of the
            # walk, and the first turn claims it. One that recorded something --
            # a scope narrowed at spawn, a status change -- describes a moment
            # before any turn existed, and folding it into turn 0 would date
            # those facts to a turn that had not happened yet.
            if current is not None and current.turn is None and _is_empty(current):
                current.turn = turn
            else:
                current = Episode(turn=turn)
                entry.episodes.append(current)
                open_episode[turn.agent_id] = current

        elif kind is EventType.DRIFT_ASSESSED:
            aid = payload.get("agent_id", "")
            current = episode_for(aid)
            if current is None:
                journal.unattributed += 1
                continue
            assessment = from_jsonable(payload["assessment"], DriftAssessment)
            _check_turn_link(current, assessment.turn_id, "drift assessment")
            current.assessments.append(assessment)

        elif kind is EventType.DIRECTIVE_ISSUED:
            directive = from_jsonable(payload["directive"], Directive)
            current = episode_for(directive.agent_id)
            if current is None:
                journal.unattributed += 1
                continue
            _check_turn_link(current, directive.turn_id, "directive")
            if current.directive is not None:
                current.anomalies.append(
                    f"a second directive ({directive.kind.value}) was issued for "
                    f"the same turn as {current.directive.kind.value}"
                )
            current.directive = directive
            # A directive carries the inbox it delivered. Prefer the message the
            # log recorded over the copy embedded in the directive, since the
            # former accumulates delivery state.
            current.inbox = [messages.get(m.id, m) for m in directive.inbox]

        elif kind is EventType.MESSAGE_DELIVERED:
            aid = payload.get("agent_id", "")
            current = episode_for(aid)
            if current is None:
                journal.unattributed += 1
                continue
            known = {m.id for m in current.inbox}
            for mid in payload.get("message_ids", []):
                if mid not in known and mid in messages:
                    current.inbox.append(messages[mid])

        elif kind is EventType.AGENT_STATUS:
            aid = payload.get("agent_id", "")
            current = episode_for(aid)
            if current is None:
                journal.unattributed += 1
                continue
            current.status_changes.append(str(payload.get("status", "")))

        elif kind is EventType.NOTE:
            note = Note(
                id=event.id,
                text=str(payload.get("text", "")),
                actor=event.actor,
                ts=event.ts,
                context={
                    k: str(v) for k, v in payload.items()
                    if k != "text" and not isinstance(v, (dict, list))
                },
            )
            # A note names its subject by actor, or by an ``agent_id`` in its
            # context; anything else belongs to the run.
            subject = note.actor if note.actor in journals else note.context.get("agent_id", "")
            current = episode_for(subject) if subject in journals else None
            if current is not None:
                current.notes.append(note)
            elif not agent_id:
                journal.run_notes.append(note)

    journal.agents = [journals[aid] for aid in state.agents if aid in journals]
    return journal


def _is_empty(episode: Episode) -> bool:
    """Whether an episode records nothing, and so is only a placeholder."""
    return not (
        episode.assessments
        or episode.directive
        or episode.inbox
        or episode.notes
        or episode.status_changes
        or episode.anomalies
    )


def _check_turn_link(episode: Episode, turn_id: str, what: str) -> None:
    """Record a disagreement between a payload's ``turn_id`` and the log's order.

    An empty ``turn_id`` is not a disagreement: it is a log written before the
    field existed, where order is the only evidence there is.
    """
    if not turn_id or episode.turn is None:
        return
    if turn_id != episode.turn.id:
        episode.anomalies.append(
            f"the {what} names turn {turn_id}, but the log's order places it "
            f"after turn {episode.turn.id}"
        )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_journal(journal: RunJournal, width: int = 96) -> str:
    """The journal as text, for `supervisor explain`."""
    out: list[str] = [
        f"run {journal.run_id}  [{journal.phase}]  mode={journal.mode}",
        f"  {journal.prompt}",
    ]

    if journal.envelope_history:
        out.append("")
        out.append("  envelope")
        for envelope in journal.envelope_history:
            out.append(
                f"    {envelope.source or '(unnamed)':<16} "
                f"{_globs(envelope.paths)}"
                + (f"  never {_globs(envelope.forbidden_paths)}"
                   if envelope.forbidden_paths else "")
            )
    elif journal.envelope is None:
        out.append("\n  envelope: none established")

    for entry in journal.agents:
        out.append("")
        out.extend(_render_agent(entry, width))

    if journal.run_notes:
        out.append("")
        out.append("  run notes")
        for note in journal.run_notes:
            out.append(f"    {_wrap(note.text, width, 6)}")

    if journal.unattributed:
        out.append("")
        out.append(
            f"  {journal.unattributed} event(s) named an agent this run never "
            "spawned, and are not shown"
        )
    return "\n".join(out)


def _render_agent(entry: AgentJournal, width: int) -> list[str]:
    agent = entry.agent
    if agent is None:
        return []

    out = [
        f"  {agent.id}  {agent.role} ({agent.kind})  {agent.status}",
        f"    {agent.title}",
        f"    scope: {_globs(agent.scope.paths)}"
        + (f"  never {_globs(agent.scope.forbidden_paths)}"
           if agent.scope.forbidden_paths else ""),
        f"    model: {agent.binding.ref()}   turns: {entry.turns}/{agent.budget.max_turns}",
    ]
    if agent.parent_agent_id:
        out.append(f"    authority from: {agent.parent_agent_id}")

    # Numbered by turn, not by episode: an opening episode is not a turn, and
    # numbering it as one makes the agent's first answer read as its second.
    ordinal = 0
    for episode in entry.episodes:
        out.append("")
        out.extend(_render_episode(episode, ordinal, width))
        if episode.turn is not None:
            ordinal += 1
    return out


def _render_episode(episode: Episode, index: int, width: int) -> list[str]:
    if episode.turn is None:
        out = ["    before the first turn"]
    else:
        turn = episode.turn
        out = [f"    turn {index} | {turn.ts} | claimed {turn.claimed_status}"]
        if turn.output:
            out.append(f"      said      {_wrap(turn.output, width, 16)}")
        if turn.files_touched:
            out.append(f"      touched   {', '.join(turn.files_touched)}")
        if turn.blocked_on:
            out.append(f"      blocked   {_wrap(turn.blocked_on, width, 16)}")
        if turn.self_assessment:
            out.append(f"      admits    {_wrap(turn.self_assessment, width, 16)}")

    for assessment in episode.assessments:
        out.append(
            f"      drift     {assessment.score:.2f} "
            f"({'on brief' if assessment.on_task else 'off brief'}) "
            f"by {assessment.checked_by}"
        )
        for signal in assessment.signals:
            out.append(f"                - {signal.kind} [{signal.severity}]: "
                       f"{_wrap(signal.detail, width, 18)}")
        if assessment.summary:
            out.append(f"                {_wrap(assessment.summary, width, 16)}")

    for message in episode.inbox:
        out.append(f"      inbox     from {message.sender} ({message.kind}): "
                   f"{_wrap(message.subject or message.content, width, 16)}")

    if episode.directive is not None:
        directive = episode.directive
        out.append(f"      DIRECTIVE {directive.kind.value.upper()}"
                   f"   ({directive.turns_remaining} turn(s) left)")
        if directive.rationale:
            out.append(f"                because {_wrap(directive.rationale, width, 24)}")
        for correction in directive.corrections:
            out.append(f"                fix: {_wrap(correction, width, 21)}")
        for focus in directive.focus:
            out.append(f"                focus: {_wrap(focus, width, 23)}")
        for forbidden in directive.forbidden:
            out.append(f"                not: {_wrap(forbidden, width, 21)}")
    elif episode.turn is not None:
        out.append("      DIRECTIVE none -- this turn settled the agent itself")

    for note in episode.notes:
        out.append(f"      note      {_wrap(note.text, width, 16)}")
    for status in episode.status_changes:
        out.append(f"      status    -> {status}")
    for anomaly in episode.anomalies:
        out.append(f"      ANOMALY   {_wrap(anomaly, width, 16)}")
    return out


def _globs(patterns: list[str]) -> str:
    from .paths import NOTHING

    if not patterns:
        return "the whole workspace"
    if list(patterns) == [NOTHING]:
        return "no path at all"
    return ", ".join(patterns)


def _wrap(text: str, width: int, indent: int) -> str:
    """``text`` on one line if it fits, else wrapped under a hanging indent."""
    import textwrap

    flat = " ".join(str(text).split())
    if len(flat) <= width - indent:
        return flat
    return ("\n" + " " * indent).join(
        textwrap.wrap(flat, max(20, width - indent)) or [""]
    )


def journal_to_dict(journal: RunJournal) -> dict[str, Any]:
    """The journal as JSON, for `--json` and for the MCP tool."""
    from ..serde import to_jsonable

    return {
        "run_id": journal.run_id,
        "prompt": journal.prompt,
        "phase": journal.phase,
        "mode": journal.mode,
        "envelope": to_jsonable(journal.envelope) if journal.envelope else None,
        "envelope_history": [to_jsonable(e) for e in journal.envelope_history],
        "agents": [
            {
                "id": entry.agent.id if entry.agent else "",
                "role": entry.agent.role if entry.agent else "",
                "kind": str(entry.agent.kind) if entry.agent else "",
                "status": str(entry.agent.status) if entry.agent else "",
                "title": entry.agent.title if entry.agent else "",
                "scope": to_jsonable(entry.agent.scope) if entry.agent else None,
                "parent_agent_id": entry.agent.parent_agent_id if entry.agent else "",
                "brief": entry.brief,
                "turns": entry.turns,
                "episodes": [
                    {
                        "turn": to_jsonable(ep.turn) if ep.turn else None,
                        "assessments": [to_jsonable(a) for a in ep.assessments],
                        "escalated": ep.escalated,
                        "directive": to_jsonable(ep.directive) if ep.directive else None,
                        "inbox": [to_jsonable(m) for m in ep.inbox],
                        "notes": [to_jsonable(n) for n in ep.notes],
                        "status_changes": list(ep.status_changes),
                        "anomalies": list(ep.anomalies),
                    }
                    for ep in entry.episodes
                ],
            }
            for entry in journal.agents
        ],
        "run_notes": [to_jsonable(n) for n in journal.run_notes],
        "unattributed": journal.unattributed,
    }
