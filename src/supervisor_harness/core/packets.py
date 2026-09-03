"""Assembling the brief an agent is given, and the packet that carries it.

The second of the layers `core/supervisor.py` was split into, and like
`core/reporting.py` it made **zero calls back into the supervisor** before the
split -- which is what made lifting it out safe rather than hopeful. It reads
the run's state, the lessons library and the agent registry, and returns a
:class:`WorkPacket`. It decides nothing about phases and writes nothing.

The bodies are the ones that were on ``Supervisor``, moved verbatim.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..agents.brief import (
    build_analysis_brief,
    build_execution_brief,
    build_verification_brief,
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

        if agent.kind is AgentKind.ANALYSIS:
            schema = ANALYSIS_TURN_SCHEMA
            brief = build_analysis_brief(
                state, agent, ROLES_BY_ID.get(agent.role), peers, schema,
                shared_context=render_context(
                    state.shared_context, state.facts, state.established
                ),
                lessons=self._lessons_for(agent) if self.config.policy.apply_lessons else [],
                tools=tools,
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
                    state.shared_context, state.facts, state.established
                ),
                lessons=self._lessons_for(agent) if self.config.policy.apply_lessons else [],
                supporting_findings=findings,
                tools=tools,
            )
        else:
            schema = VERIFICATION_SCHEMA
            task = state.tasks.get(agent.task_id or "")
            summary = self._change_summary(session, task)
            brief = build_verification_brief(
                state, agent, task or ExecutionTask(run_id=state.id, title=agent.title),
                schema, change_summary=summary, tools=tools,
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

        return WorkPacket(
            run_id=state.id,
            agent_id=agent.id,
            kind=str(agent.kind),
            title=agent.title,
            brief=brief,
            schema=schema,
            turn_index=turns_used,
            turns_remaining=max(0, agent.budget.max_turns - turns_used),
            host_agent_type=agent.host_agent_type,
            model=agent.binding.ref(),
            task_id=agent.task_id,
        )
    def _stage_packet(
        self,
        session: RunSession,
        agent: AgentSpec,
        system: str,
        user: str,
        schema: dict[str, Any],
        kind: str,
    ) -> WorkPacket:
        brief = f"{system}\n\n---\n\n{user}"
        session.emit(EventType.BRIEF_RENDERED, {"agent_id": agent.id, "brief": brief})
        session.emit(EventType.AGENT_DISPATCHED, {"agent_id": agent.id, "kind": kind})
        return WorkPacket(
            run_id=session.state.id,
            agent_id=agent.id,
            kind=kind,
            title=agent.title,
            brief=brief,
            schema=schema,
            turns_remaining=1,
            host_agent_type=agent.host_agent_type,
            model=agent.binding.ref(),
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
        """
        if host_agents:
            session.emit(EventType.HOST_AGENTS_DECLARED, {"agents": list(host_agents)})
        declared = host_agents or session.state.host_agents
        return AgentRegistry(self.workspace, self.host, declared)
