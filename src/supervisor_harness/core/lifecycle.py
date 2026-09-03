"""The life of an agent: spawned, attenuated, statused, abandoned, reaped.

The third layer lifted out of `core/supervisor.py`, and like the first two it
made **zero calls back into the supervisor** -- measured before the move, not
hoped for afterwards. It needs `config` and `router` and nothing else.

It also answers the routing questions that decide *where* a stage runs
(`_delegated`, `_host_routed_stages`, `_stage_for`), which belong here for the
same reason: they are facts about an agent and its stage, settled without asking
the phase machine anything.

`_spawn` is the attenuation point -- every child scope is narrowed to every
ceiling above it here, which is the run envelope's whole purpose. See
`docs/reasoning-control-plane.md`.

The bodies are the ones that were on ``Supervisor``, moved verbatim.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..agents.roles import ROLES_BY_ID
from ..config import KNOWN_STAGES, HarnessConfig
from ..contracts import (
    CHECKPOINT_SCHEMA,
    LESSONS_SCHEMA,
    PLANNING_SCHEMA,
    SYNTHESIS_SCHEMA,
)
from ..models import (
    ACTIVE_AGENT_STATUSES,
    AgentKind,
    AgentSpec,
    AgentStatus,
    Backend,
    Budget,
    RunState,
    Scope,
)
from ..providers.router import ModelRouter
from ..serde import to_jsonable
from ..store.events import EventType
from ..store.runstore import RunSession
from .envelope import Ceiling, attenuate, effective

# Which stage and contract each single-agent role answers on. Moved here with
# `_stage_for`, its only reader.
STAGE_ROLES = {
    "planner": ("planning", PLANNING_SCHEMA),
    "synthesizer": ("synthesis", SYNTHESIS_SCHEMA),
    "checkpointer": ("verification", CHECKPOINT_SCHEMA),
    "improver": ("improvement", LESSONS_SCHEMA),
}


def _seconds_since(ts: str) -> float | None:
    """Seconds elapsed since an event timestamp, or ``None`` if it is unusable."""
    if not ts:
        return None
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (datetime.now(UTC) - when).total_seconds()


class Lifecycle:
    """Creates, bounds, ends and reaps the agents of a run."""

    def __init__(self, config: HarnessConfig, router: ModelRouter) -> None:
        self.config = config
        self.router = router

    def _spawn(self, session: RunSession, specs: list[AgentSpec]) -> list[AgentSpec]:
        """Record each agent on the log, and hand back the state's own objects.

        The specs passed in are built from a plan; the ones in ``RunState`` are
        what the fold produced from the event. They used to be different objects,
        so a caller that kept driving the list it had built was driving copies
        that no status change would ever reach. Returning the state's own removes
        the question.

        Attenuation happens here, at the one place every agent in the run passes
        through, rather than in each of the four builders. A builder that forgets
        is then a builder that proposes too much, not one that grants too much --
        which is the difference between a fence with a hole in it and a fence.
        """
        for spec in specs:
            narrowed, notes = attenuate(spec.scope, self._ceilings(session.state, spec))
            if not notes:
                continue
            spec.scope = narrowed
            for text in notes:
                session.note(text, actor=spec.id, role=spec.role, kind=str(spec.kind))

        session.emit_many([
            (EventType.AGENT_SPAWNED, {"agent": to_jsonable(spec)}, "supervisor")
            for spec in specs
        ])
        return [session.state.agents[spec.id] for spec in specs]
    def _ceilings(self, state: RunState, spec: AgentSpec) -> list[Ceiling | None]:
        """Every bound above one agent, outermost first.

        The run's envelope always. The task's scope when the agent exists to
        work on a task, which is what stops a verifier being fenced more loosely
        than the work it judges. The spawning agent's scope where one is named,
        so authority attenuates along the chain rather than being reissued at
        full strength at each link.
        """
        task = state.tasks.get(spec.task_id or "")
        parent = state.agents.get(spec.parent_agent_id or "")
        return [
            Ceiling.of("run envelope", effective(state.envelope)),
            Ceiling.of("task scope", task.scope if task is not None else None),
            Ceiling.of(
                f"scope of `{parent.id}`" if parent is not None else "spawner scope",
                parent.scope if parent is not None else None,
            ),
        ]
    async def _set_status(self, session: RunSession, agent: AgentSpec, status: AgentStatus) -> None:
        if agent.status is status:
            return
        await session.aemit(EventType.AGENT_STATUS, {"agent_id": agent.id, "status": str(status)})
    async def _abandon_agent(self, session: RunSession, agent: AgentSpec, reason: str) -> None:
        """End an agent that will never report, naming it and the cause on the log.

        The same convention budget exhaustion and escalation already follow: a
        terminal transition is not just a status change, it is a note saying
        which agent ended and why.
        """
        await session.anote(
            f"agent `{agent.id}` abandoned: {reason}",
            actor=agent.id,
            role=agent.role,
            kind=str(agent.kind),
            task_id=agent.task_id or "",
        )
        await self._set_status(session, agent, AgentStatus.FAILED)
    def _abandonment_reason(self, agent: AgentSpec) -> str | None:
        """Why this agent should be given up on, or ``None`` to keep waiting."""
        policy = self.config.policy
        limit = policy.max_unreported_dispatches
        if limit > 0 and agent.unreported_dispatches >= limit:
            return (
                f"handed {agent.unreported_dispatches} packet(s) with no report back "
                f"({limit} allowed before the supervisor gives up)"
            )

        timeout = policy.agent_timeout_seconds
        elapsed = _seconds_since(agent.unreported_since)
        if timeout > 0 and elapsed is not None and elapsed >= timeout:
            return (
                f"no report {elapsed:.0f}s after its packet went out "
                f"(bound {timeout:.0f}s)"
            )
        return None
    async def _reap_unreported(
        self, session: RunSession, agents: list[AgentSpec]
    ) -> list[AgentSpec]:
        """Abandon the agents that are past their bound; return the rest.

        Only the host path needs this. An agent the harness drives itself either
        answers, raises or runs out of turns, and every one of those already ends
        it; a host agent has none of those paths, so silence is all there is.
        """
        if session.state.backend is Backend.AUTONOMOUS:
            return agents
        alive: list[AgentSpec] = []
        for agent in agents:
            reason = self._abandonment_reason(agent)
            if reason is None:
                alive.append(agent)
            else:
                await self._abandon_agent(session, agent, reason)
        return alive
    async def _stage_agent(
        self, session: RunSession, role: str, stage: str
    ) -> AgentSpec | None:
        """Find or create the pseudo-agent that carries out a supervisory stage.

        ``None`` means the stage has been given up on: either this call abandoned
        an agent that was past its bound, or an earlier one did. A replacement is
        deliberately not spawned -- it would put the same packet back on every
        advance for as long as the host cannot run it, which is the loop the
        bound exists to break. Each caller decides what its stage does without an
        answer.
        """
        state = session.state
        existing = [a for a in state.agents.values() if a.role == role]
        for agent in existing:
            if agent.status not in ACTIVE_AGENT_STATUSES:
                continue
            reason = self._abandonment_reason(agent)
            if reason is None:
                return agent
            await self._abandon_agent(session, agent, reason)
            return None
        if any(a.status is AgentStatus.FAILED for a in existing):
            return None
        spec = AgentSpec(
            run_id=state.id,
            role=role,
            kind=AgentKind.SYNTHESIS,
            title=role.capitalize(),
            brief=f"{role} stage",
            binding=self.config.binding_for(stage),
            backend=state.backend,
            budget=Budget(max_turns=1),
            scope=Scope(),
        )
        self._spawn(session, [spec])
        return session.state.agents[spec.id]
    def _stage_for(self, agent: AgentSpec) -> str:
        role = ROLES_BY_ID.get(agent.role)
        return role.stage if role else STAGE_ROLES.get(agent.role, ("default", None))[0]
    def _delegated(self, stage: str) -> bool:
        return self.router.is_host(stage)
    def _host_routed_stages(self) -> list[str]:
        """Every configured stage that resolves to the host provider."""
        return sorted(
            stage for stage in set(self.config.routing) | set(KNOWN_STAGES)
            if self._delegated(stage)
        )
