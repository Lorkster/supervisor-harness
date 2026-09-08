"""Assembling the brief an agent is given, and the packet that carries it.

The second of the layers `core/supervisor.py` was split into, and like
`core/reporting.py` it made **zero calls back into the supervisor** before the
split -- which is what made lifting it out safe rather than hopeful. It reads
the run's state, the lessons library and the agent registry, and returns a
:class:`WorkPacket`. It decides nothing about phases.

## Where a packet is carried, and why it is not carried inline

It used to write nothing. It now writes the brief, the answer's schema and the
run's rules into the run directory, and puts paths in the packet instead of
text -- because a host-delegated run makes the orchestrator the message bus and
the orchestrator is not the reader.

A native sub-agent is a context firewall: it burns its own window and the caller
pays for the final report. Here the supervisor has to see every brief, every
result and every directive in order to supervise them, so nothing is firewalled,
and a run of thirty round trips pushes all thirty briefs and all thirty answers
through one context. Measured on an analysis brief: about 9,000 characters of
which roughly 5,000 is the pretty-printed schema -- the same 5,000 for every
analysis agent in every run.

Writing them out changes who pays for them. The supervisor still records the
brief on the log and still scores drift against it; the orchestrator holds a few
lines and a path, and the sub-agent -- which has file tools, being an agent --
reads the file. `BRIEF_RENDERED` carries the by-reference text, not the inline
text it replaced, because drift is scored against what the agent was actually
given and scoring it against a brief it never saw would be worse than not
scoring it. The file additionally carries any outstanding directive, which the
log records separately and has always kept out of the drift anchor.

Two things stay inline. An autonomous run has no second process to read a file:
the harness feeds the brief straight to a provider, so `Backend.AUTONOMOUS`
always carries text. And ``inline_briefs`` in the configuration forces the old
form for a host that cannot read files -- it costs context, not correctness.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agents.brief import (
    BriefRefs,
    build_analysis_brief,
    build_execution_brief,
    build_rules_document,
    build_verification_brief,
    render_contract,
    render_directive,
)
from ..agents.registry import AgentRegistry
from ..agents.roles import ROLES_BY_ID
from ..config import HarnessConfig
from ..contracts import (
    ANALYSIS_TURN_SCHEMA,
    EXECUTION_TURN_SCHEMA,
    VERIFICATION_SCHEMA,
)
from ..host.detect import HostInfo
from ..models import (
    AgentKind,
    AgentSpec,
    AgentTurn,
    Backend,
    Directive,
    DirectiveKind,
    ExecutionTask,
    Lesson,
    RunState,
)
from ..store.events import EventType
from ..store.runstore import RunSession, RunStore
from .blackboard import render_context
from .responses import WorkPacket
from .tools import render_tools_section

#: The contract file each kind of answer is written to, once per run. Keyed by
#: the packet's ``kind`` so a stage packet and an agent packet name theirs the
#: same way, and named after the contract rather than the agent so two analysis
#: lenses share one file instead of writing the same schema twice.
CONTRACT_FILENAMES = {
    "analysis": "analysis-turn.json",
    "execution": "execution-turn.json",
    "verification": "verification.json",
}

#: Where the run's rules live, relative to the run directory. In ``packets``
#: rather than beside them because it is brief material: written once, pointed
#: at by every brief in the run.
RULES_FILENAME = "RULES.md"


@dataclass(frozen=True)
class _Handoff:
    """The files one by-reference packet uses, absolute and as brief text."""

    brief: Path
    contract: Path
    result: Path
    refs: BriefRefs


def _relative_to(path: Path, base: Path) -> str:
    """``path`` as the shortest thing that still resolves for the reader.

    Relative to the workspace when the run directory is inside it, which is the
    default layout and reads far better in a brief; absolute otherwise, because
    ``SUPERVISOR_HOME`` can put the run directory anywhere and a relative path
    out of a sub-agent's working directory is a failed dispatch rather than an
    untidy one.
    """
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return str(path)


# Directives that leave the agent owing another turn. The rest (accept, stop,
# escalate) settle it, so there is nothing outstanding to re-issue.
CONTINUATION_DIRECTIVES = frozenset({
    DirectiveKind.CONTINUE,
    DirectiveKind.REFOCUS,
    DirectiveKind.NARROW,
    DirectiveKind.DEEPEN,
    DirectiveKind.ANSWER,
    DirectiveKind.REJECT,
})


class Packets:
    """Builds what an agent is asked to do. Reads state; changes none."""

    def __init__(
        self,
        config: HarnessConfig,
        store: RunStore,
        workspace: Path,
        host: HostInfo,
    ) -> None:
        self.config = config
        self.store = store
        self.workspace = workspace
        self.host = host

    def _agent_packet(
        self, session: RunSession, agent: AgentSpec, directive: Directive | None = None
    ) -> WorkPacket:
        state = session.state
        peers = [a for a in state.agents.values() if a.kind is agent.kind]
        turns_used = state.turn_counts.get(agent.id, 0)

        # A host-run agent uses the host's own tools and must not be told about
        # the harness's; only an agent the harness drives itself gets this.
        tools = (
            render_tools_section(agent, self.config.policy)
            if agent.backend is Backend.AUTONOMOUS
            else ""
        )

        handoff = (
            None if self._inline(agent)
            else self._handoff(session, agent.id, str(agent.kind), turns_used)
        )
        refs = handoff.refs if handoff else BriefRefs()

        if agent.kind is AgentKind.ANALYSIS:
            schema = ANALYSIS_TURN_SCHEMA
            brief = build_analysis_brief(
                state, agent, ROLES_BY_ID.get(agent.role), peers, schema,
                shared_context=render_context(
                    state.shared_context, state.facts, state.established,
                    workspace=self.workspace,
                ),
                lessons=self._lessons_for(agent) if self.config.policy.apply_lessons else [],
                tools=tools,
                refs=refs,
            )
        elif agent.kind is AgentKind.EXECUTION:
            schema = EXECUTION_TURN_SCHEMA
            task = state.tasks.get(agent.task_id or "")
            findings = [
                f"[{f.severity.value}] {f.title}: {f.detail}"
                for f in state.findings
                if task and f.id in task.rationale_refs
            ]
            brief = build_execution_brief(
                state, agent, task or ExecutionTask(run_id=state.id, title=agent.title),
                ROLES_BY_ID.get(agent.role), peers, schema,
                shared_context=render_context(
                    state.shared_context, state.facts, state.established,
                    workspace=self.workspace,
                ),
                lessons=self._lessons_for(agent) if self.config.policy.apply_lessons else [],
                supporting_findings=findings,
                tools=tools,
                refs=refs,
            )
        else:
            schema = VERIFICATION_SCHEMA
            task = state.tasks.get(agent.task_id or "")
            summary = self._change_summary(session, task)
            brief = build_verification_brief(
                state, agent, task or ExecutionTask(run_id=state.id, title=agent.title),
                schema, change_summary=summary, tools=tools, refs=refs,
            )

        # The brief is rendered once and reused, so it stays a stable anchor for
        # drift scoring. Persisted for the same reason: a process that could not
        # see it scored the same turn quite differently.
        stored = state.briefs.get(agent.id)
        if stored:
            brief = stored
        else:
            session.emit(EventType.BRIEF_RENDERED, {"agent_id": agent.id, "brief": brief})

        # A continuation carries the brief *and* the directive. The directive
        # alone ends with "reply with the same contract as before", which assumes
        # the agent still remembers its brief -- true while the host keeps it
        # alive, false after a resume, when the host spawns a fresh agent. A
        # packet has to stand on its own, as the protocol says it does.
        if directive is not None:
            brief = f"{brief}\n\n---\n\n{render_directive(directive, agent)}"

        return self._carry(
            WorkPacket(
                run_id=state.id,
                agent_id=agent.id,
                kind=str(agent.kind),
                title=agent.title,
                brief=brief,
                schema=schema,
                turn_index=turns_used,
                turns_remaining=max(0, agent.budget.max_turns - turns_used),
                host_agent_type=agent.host_agent_type,
                host_agent_reason=agent.host_agent_reason,
                model=agent.binding.ref(),
                task_id=agent.task_id,
            ),
            agent=agent,
            handoff=handoff,
        )

    # -- carrying a packet by reference ------------------------------------

    def _inline(self, agent: AgentSpec) -> bool:
        """Whether this packet carries its brief instead of pointing at it."""
        return self.config.inline_briefs or agent.backend is Backend.AUTONOMOUS

    def _handoff(
        self, session: RunSession, agent_id: str, kind: str, turn_index: int
    ) -> _Handoff:
        """The three files this packet points at: where they are, and how named.

        The paths are computed once and used twice -- as relative text inside
        the brief, where a reader is a model and someone's disk layout does not
        belong, and as absolute paths on the packet, where the reader is a host
        whose working directory the harness does not control.

        The rules document is written here rather than once per run: it is
        deterministic for the run, so the repeated write is idempotent, and a
        guard would be a cache to invalidate in exchange for one small write per
        dispatch.
        """
        run_id = session.state.id
        rules = self.store.write_run_file(
            run_id, "packets", RULES_FILENAME, build_rules_document(session.state)
        )
        contract_name = CONTRACT_FILENAMES.get(kind, f"{kind}.json")
        contract = self.store.run_file(run_id, "contracts", contract_name)
        result = self.store.run_file(run_id, "results", f"{agent_id}.t{turn_index}.json")
        # The results directory has to exist before an agent is told to write
        # into it: a sub-agent that has to create a directory first is a
        # sub-agent given one more way to put the file somewhere else.
        result.parent.mkdir(parents=True, exist_ok=True)
        return _Handoff(
            brief=self.store.run_file(run_id, "packets", f"{agent_id}.t{turn_index}.md"),
            contract=contract,
            result=result,
            refs=BriefRefs(
                rules=_relative_to(rules, self.workspace),
                contract=_relative_to(contract, self.workspace),
                result=_relative_to(result, self.workspace),
            ),
        )

    def _carry(
        self,
        packet: WorkPacket,
        *,
        agent: AgentSpec,
        handoff: _Handoff | None,
    ) -> WorkPacket:
        """Move the brief and schema out of the packet and into the run directory.

        Returns the packet unchanged when it is carried inline. Otherwise both
        are written out and then *emptied* on the packet -- emptied rather than
        left populated, because a packet carrying both forms saves nothing and
        invites a reader to use whichever one it happens to notice.
        """
        if handoff is None:
            return packet

        self.store.write_run_file(
            packet.run_id, "packets", handoff.brief.name, packet.brief
        )
        self.store.write_run_file(
            packet.run_id, "contracts", handoff.contract.name,
            json.dumps(packet.schema, indent=2),
        )

        packet.brief_path = str(handoff.brief)
        packet.contract_path = str(handoff.contract)
        packet.result_path = str(handoff.result)
        packet.brief_digest = self._digest(packet, agent)
        packet.brief = ""
        packet.schema = {}
        return packet

    def _digest(self, packet: WorkPacket, agent: AgentSpec) -> str:
        """A few lines so the orchestrator can dispatch and narrate, and no more.

        Deliberately not a summary of the brief. The supervisor scores drift
        against the brief's exact text, so a digest good enough to work *from*
        would be a second, unmeasured brief -- which is the failure the "do not
        summarise a packet before dispatching it" rule already exists to stop.
        This names the job and says which file to read.
        """
        turn = f"turn {packet.turn_index + 1}"
        if agent.budget.max_turns:
            turn += f" of {agent.budget.max_turns}"
        lines = [f"{packet.title} -- {packet.kind}, {turn}"]
        if agent.objectives:
            shown = "; ".join(agent.objectives[:3])
            more = f" (+{len(agent.objectives) - 3} more)" if len(agent.objectives) > 3 else ""
            lines.append(f"Objectives: {shown}{more}")
        lines.append(
            "Scope: " + (", ".join(agent.scope.paths) if agent.scope.paths else "the workspace")
        )
        lines.append(
            f"Brief: {packet.brief_path} -- give the sub-agent this file in full; "
            "do not summarise it."
        )
        lines.append(
            f"Answer: write the object described by {packet.contract_path} to "
            f"{packet.result_path}, then report that path."
        )
        return "\n".join(lines)
    def _stage_packet(
        self,
        session: RunSession,
        agent: AgentSpec,
        system: str,
        user: str,
        schema: dict[str, Any],
        kind: str,
    ) -> WorkPacket:
        handoff = None if self._inline(agent) else self._handoff(session, agent.id, kind, 0)
        brief = f"{system}\n\n---\n\n{user}"
        if handoff is not None:
            # A stage prompt describes its answer in prose and relies on the
            # packet's `schema` for the shape. Emptying that without saying where
            # it went would leave the stage with no contract at all, so the
            # pointer is part of the brief before the brief is recorded.
            brief += "\n\n## Output contract\n" + render_contract(
                schema, handoff.refs.contract, handoff.refs.result
            )
        # Recorded before the packet is stripped, so the log holds what the
        # agent was actually given rather than the packet it arrived in.
        session.emit(EventType.BRIEF_RENDERED, {"agent_id": agent.id, "brief": brief})
        session.emit(EventType.AGENT_DISPATCHED, {"agent_id": agent.id, "kind": kind})
        return self._carry(
            WorkPacket(
                run_id=session.state.id,
                agent_id=agent.id,
                kind=kind,
                title=agent.title,
                brief=brief,
                schema=schema,
                turns_remaining=1,
                host_agent_type=agent.host_agent_type,
                host_agent_reason=agent.host_agent_reason,
                model=agent.binding.ref(),
            ),
            agent=agent,
            handoff=handoff,
        )
    def _dispatch_packet(self, session: RunSession, agent: AgentSpec) -> WorkPacket:
        """Packet for an agent being (re-)dispatched, carrying any open directive.

        Every packet handed to the host is recorded, because the count of them
        is the only evidence the supervisor has that an agent which never
        answers has been asked more than once.
        """
        packet = self._agent_packet(
            session, agent,
            directive=self._outstanding_directive(session.state, agent),
        )
        session.emit(EventType.AGENT_DISPATCHED, {"agent_id": agent.id, "kind": packet.kind})
        return packet
    @staticmethod
    def _outstanding_directive(state: RunState, agent: AgentSpec) -> Directive | None:
        """The directive this agent was issued and has not yet answered.

        Without this, resuming a run re-briefed every agent from scratch and
        dropped the correction it was mid-way through applying -- the agent had
        no idea it had been told to narrow its scope, and the supervisor had no
        idea it had said so.
        """
        if state.turn_counts.get(agent.id, 0) == 0:
            return None
        for directive in reversed(state.directives):
            if directive.agent_id == agent.id:
                return directive if directive.kind in CONTINUATION_DIRECTIVES else None
        return None
    def _change_summary(self, session: RunSession, task: ExecutionTask | None) -> str:
        if task is None:
            return ""
        state = session.state
        parts: list[str] = []
        for turn in state.turns:
            agent = state.agents.get(turn.agent_id)
            if agent is None or agent.task_id != task.id:
                continue
            if turn.output:
                parts.append(turn.output)
            if turn.files_touched:
                parts.append("Files touched: " + ", ".join(turn.files_touched))
        return "\n\n".join(parts[-4:])
    def _lessons_for(self, agent: AgentSpec) -> list[Lesson]:
        """The lessons this agent's brief should carry.

        Both keyword arguments were previously left at their defaults by every
        caller, which meant two behaviours existed and never ran: `lessons_for`
        ranks a lesson learned in this workspace above one borrowed from another
        at equal strength, and it drops lessons past an age cap that
        `policy.lesson_max_age_days` is supposed to set. A workspace configuring
        that cap changed nothing, and borrowed experience sorted level with
        local. Passing both here is the whole fix.
        """
        policy = self.config.policy
        return self.store.lessons_for(
            [agent.role],
            policy.max_lessons_in_brief,
            workspace=str(self.workspace),
            max_age_days=policy.lesson_max_age_days,
        )
    @staticmethod
    def _previous_turns(
        session: RunSession, agent_id: str, before: str | None = None
    ) -> list[AgentTurn]:
        """This agent's turns, from the folded state rather than from the log.

        All three readers here used to re-parse the whole of ``events.jsonl``,
        because the fold kept the turn *count* and discarded the body. This one
        is called once per supervised turn, from ``_supervise``, so the cost of
        supervising a run was quadratic in the length of the run being
        supervised -- and the log is the largest file the harness writes.
        """
        return [
            turn for turn in session.state.turns
            if turn.agent_id == agent_id and not (before and turn.id == before)
        ]
    def _schema_for(self, agent: AgentSpec) -> dict[str, Any]:
        return {
            AgentKind.ANALYSIS: ANALYSIS_TURN_SCHEMA,
            AgentKind.EXECUTION: EXECUTION_TURN_SCHEMA,
            AgentKind.VERIFICATION: VERIFICATION_SCHEMA,
        }.get(agent.kind, ANALYSIS_TURN_SCHEMA)
    def _registry_for(
        self, session: RunSession, host_agents: list[dict[str, Any]] | None
    ) -> AgentRegistry:
        """Build the registry, persisting any newly declared host agents.

        The host declares what it can spawn when it starts or advances a run.
        That declaration is recorded on the run so every later phase can still
        match roles to real subagent types, including after a resume in a
        different session.

        An *empty* declaration is recorded too, and that is the point of the
        ``is not None`` rather than a truth test. "The host was asked and said it
        can spawn nothing" and "the host was never asked" produce identical runs
        and identical generic briefs, and until this was on the log there was no
        way to tell them apart afterwards -- which is exactly the question you
        ask when no local agent was used.
        """
        if host_agents is not None:
            session.emit(EventType.HOST_AGENTS_DECLARED, {"agents": list(host_agents)})
        declared = host_agents or session.state.host_agents
        return AgentRegistry(self.workspace, self.host, declared)
