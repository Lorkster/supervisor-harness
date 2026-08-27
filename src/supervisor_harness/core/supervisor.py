"""The supervisor: the state machine that drives a run.

One object serves both execution backends, because the difference between them
is narrow. In **host-delegated** mode a phase produces :class:`WorkPacket`
objects for Claude Code or Cursor to execute, and the host reports each turn
back through :meth:`Supervisor.report`. In **autonomous** mode the supervisor
runs the same packets itself against a model provider. The supervision applied
to a turn -- drift assessment, directive, message routing, event emission -- is
identical either way, so behaviour does not change with the backend.

Every phase transition and every turn is an event on the log first and an
in-memory change second, which is what makes a run resumable from any point.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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
from ..config import HarnessConfig, load_config
from ..contracts import (
    ANALYSIS_TURN_SCHEMA,
    CHECKPOINT_SCHEMA,
    DRIFT_SCHEMA,
    EXECUTION_TURN_SCHEMA,
    LESSONS_SCHEMA,
    PLANNING_SCHEMA,
    SYNTHESIS_SCHEMA,
    VERIFICATION_SCHEMA,
    parse_checkpoint,
    parse_drift,
    parse_findings,
    parse_lessons,
    parse_messages,
    parse_status,
    parse_tasks,
    parse_tool_calls,
)
from ..host.detect import HostInfo, detect_host
from ..ids import now_iso
from ..models import (
    ACTIVE_AGENT_STATUSES,
    AgentKind,
    AgentSpec,
    AgentStatus,
    AgentTurn,
    Backend,
    Budget,
    Checkpoint,
    CriterionStatus,
    Decision,
    Directive,
    DirectiveKind,
    DriftAssessment,
    ExecutionTask,
    Phase,
    RunMode,
    RunState,
    Scope,
    TaskDecision,
    TaskStatus,
    Usage,
)
from ..providers.base import ChatMessage, CompletionRequest
from ..providers.router import ModelRouter
from ..serde import to_jsonable
from ..store.events import EventType
from ..store.runstore import RunSession, RunStore
from . import phases
from .blackboard import Blackboard
from .dod import verify_criterion
from .tools import Toolbox, render_results, render_tools_section
from .drift import (
    TurnContext,
    assess_heuristically,
    decide_directive,
    merge_assessments,
    should_escalate,
    status_after,
)

# Stage agents are ordinary agents so that planning, synthesis, the checkpoint
# and the improvement pass all flow through the same report/supervise path.
# Tool calls are cheap relative to a supervised turn, but an agent that keeps
# reading without answering is its own kind of drift, so rounds are capped.
MAX_TOOL_ROUNDS = 6

STAGE_ROLES = {
    "planner": ("planning", PLANNING_SCHEMA),
    "synthesizer": ("synthesis", SYNTHESIS_SCHEMA),
    "checkpointer": ("verification", CHECKPOINT_SCHEMA),
    "improver": ("improvement", LESSONS_SCHEMA),
}


@dataclass
class WorkPacket:
    """One unit of work for the host (or the harness) to execute."""

    run_id: str
    agent_id: str
    kind: str
    title: str
    brief: str
    schema: dict[str, Any]
    turn_index: int = 0
    turns_remaining: int = 0
    host_agent_type: str | None = None
    model: str = "host"
    task_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass
class SupervisorResponse:
    """What the caller should do next."""

    run_id: str
    phase: str
    action: str          # dispatch | await_reports | await_approval | complete | failed
    message: str = ""
    packets: list[WorkPacket] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    task_notes: dict[str, list[str]] = field(default_factory=dict)
    directive: dict[str, Any] | None = None
    checkpoint: dict[str, Any] | None = None
    report_markdown: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


class Supervisor:
    """Drives runs: plan, analyse, synthesise, execute, verify, check, learn."""

    def __init__(
        self,
        workspace: Path | str = ".",
        config: HarnessConfig | None = None,
        store: RunStore | None = None,
        host: HostInfo | None = None,
        router: ModelRouter | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.config = config or load_config(self.workspace)
        self.store = store or RunStore.discover(self.workspace)
        self.host = host or detect_host(self.workspace)
        self.router = router or ModelRouter(self.config, host_name=self.host.name)
        self._boards: dict[str, Blackboard] = {}
        self._briefs: dict[str, str] = {}   # agent_id -> the brief it was given
        self.toolbox = Toolbox(self.workspace, self.config.policy)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        prompt: str,
        *,
        mode: RunMode = RunMode.AUTO,
        backend: Backend | None = None,
        host_agents: list[dict[str, Any]] | None = None,
    ) -> SupervisorResponse:
        """Create a run and take it as far as the first delegation point."""
        state = RunState(
            prompt=prompt.strip(),
            workspace=str(self.workspace),
            mode=mode,
            backend=backend or self.config.backend,
            host=self.host.name,
        )
        session = self.store.create(state)
        self._registry_for(session, host_agents)
        self._boards[session.state.id] = Blackboard(session.state.id)
        session.note(
            "run created",
            host=self.host.name,
            backend=str(session.state.backend),
            routing={k: v for k, v in self.config.routing.items()},
        )
        return await self._advance(session)

    async def advance(self, run_id: str, host_agents: list[dict[str, Any]] | None = None) -> SupervisorResponse:
        session = self.store.open(run_id)
        self._registry_for(session, host_agents)
        return await self._advance(session)

    async def resume(self, run_id: str) -> SupervisorResponse:
        """Reopen a persisted run and continue from wherever it stopped."""
        session = self.store.open(run_id)
        self._registry_for(session, None)
        session.note("run resumed", phase=str(session.state.phase))
        return await self._advance(session)

    def status(self, run_id: str) -> dict[str, Any]:
        state = self.store.load_state(run_id)
        return {
            "run_id": state.id,
            "phase": str(state.phase),
            "mode": str(state.mode),
            "backend": str(state.backend),
            "prompt": state.prompt,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
            "agents": [
                {
                    "id": a.id,
                    "role": a.role,
                    "kind": str(a.kind),
                    "title": a.title,
                    "status": str(a.status),
                    "turns": state.turn_counts.get(a.id, 0),
                    "drift": state.drift[a.id].score if a.id in state.drift else None,
                    "model": a.binding.ref(),
                }
                for a in state.agents.values()
            ],
            "findings": len(state.findings),
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": str(t.status),
                    "decision": str(t.decision) if t.decision else None,
                    "dod": f"{sum(1 for c in t.mandatory_criteria if c.status is CriterionStatus.PASS)}"
                           f"/{len(t.mandatory_criteria)}",
                    "satisfied": t.dod_satisfied(),
                }
                for t in state.tasks.values()
            ],
            "checkpoints": [
                {"iteration": c.iteration, "passed": c.passed, "quality": c.quality,
                 "scope_fidelity": c.scope_fidelity, "completeness": c.completeness}
                for c in state.checkpoints
            ],
            "lessons": len(state.lessons),
            "usage": to_jsonable(state.total_usage()),
            "error": state.error,
        }

    # ------------------------------------------------------------------
    # Phase machine
    # ------------------------------------------------------------------

    async def _advance(self, session: RunSession) -> SupervisorResponse:
        """Move the run forward until it needs something from outside."""
        guard = 0
        while guard < 12:
            guard += 1
            state = session.state
            phase = state.phase

            if phase is Phase.CREATED:
                response = await self._begin_planning(session)
            elif phase is Phase.ANALYZING:
                response = await self._continue_analysis(session)
            elif phase is Phase.SYNTHESIZING:
                response = await self._run_synthesis(session)
            elif phase is Phase.AWAITING_APPROVAL:
                response = self._await_approval(session)
            elif phase is Phase.EXECUTING:
                response = await self._continue_execution(session)
            elif phase is Phase.VERIFYING:
                response = await self._continue_verification(session)
            elif phase is Phase.CHECKPOINT:
                response = await self._run_checkpoint(session)
            elif phase is Phase.IMPROVING:
                response = await self._run_improvement(session)
            else:
                response = self._final_response(session)

            if response is not None:
                session.sync_index()
                return response
        return self._error(session, "phase machine did not settle")

    def _transition(self, session: RunSession, phase: Phase, **payload: Any) -> None:
        session.emit(EventType.PHASE_CHANGED, {"phase": str(phase), **payload})

    # -- planning ------------------------------------------------------

    async def _begin_planning(self, session: RunSession) -> SupervisorResponse | None:
        state = session.state
        registry = self._registry_for(session, None)
        lenses = phases.plan_lenses(state, self.config)
        fallback = phases.build_analysis_agents(state, self.config, registry, lenses)

        system, user = phases.planning_prompt(state, registry, lenses)
        stage = "planning"

        if self._delegated(stage):
            agent = self._stage_agent(session, "planner", stage)
            self._transition(session, Phase.ANALYZING)
            packet = self._stage_packet(session, agent, system, user, PLANNING_SCHEMA, "planning")
            return SupervisorResponse(
                run_id=state.id, phase=str(state.phase), action="dispatch",
                message=(
                    "Planning stage. Run this one packet, then report it with "
                    "supervisor_report. Analysis agents are dispatched after it lands."
                ),
                packets=[packet],
            )

        plan = await self._call(stage, system, user, PLANNING_SCHEMA)
        self._apply_plan(session, plan, fallback, registry)
        return None

    def _apply_plan(
        self,
        session: RunSession,
        plan: dict[str, Any],
        fallback: list[AgentSpec],
        registry: AgentRegistry,
    ) -> None:
        state = session.state
        specs, shared, mode = phases.apply_plan(state, plan, self.config, registry, fallback)

        board = self._board(state.id)
        board.set_context(shared or "")
        if plan.get("restated_goal"):
            board.record_fact("restated goal", str(plan["restated_goal"]))

        if state.mode is RunMode.AUTO and mode is not RunMode.AUTO:
            session.emit(EventType.NOTE, {"text": f"planning selected mode {mode.value}",
                                          "mode": str(mode)})
            state.mode = mode

        self._spawn(session, specs)
        if state.phase is not Phase.ANALYZING:
            self._transition(session, Phase.ANALYZING)

    # -- analysis ------------------------------------------------------

    async def _continue_analysis(self, session: RunSession) -> SupervisorResponse | None:
        state = session.state
        analysts = [a for a in state.agents.values() if a.kind is AgentKind.ANALYSIS]

        if not analysts:
            registry = self._registry_for(session, None)
            lenses = phases.plan_lenses(state, self.config)
            self._spawn(session, phases.build_analysis_agents(state, self.config, registry, lenses))
            analysts = [a for a in state.agents.values() if a.kind is AgentKind.ANALYSIS]

        pending = [a for a in analysts if a.status in ACTIVE_AGENT_STATUSES]
        if not pending:
            self._transition(session, Phase.SYNTHESIZING)
            return None

        if state.backend is Backend.AUTONOMOUS:
            await self._run_agents_autonomously(session, pending)
            return None

        packets = [self._agent_packet(session, agent) for agent in pending]
        return SupervisorResponse(
            run_id=state.id, phase=str(state.phase), action="dispatch",
            message=(
                f"Analysis phase: {len(packets)} agent(s) to run in parallel. Dispatch "
                "them together, then report each one's result with supervisor_report "
                "as it finishes. Call supervisor_advance when all have been reported."
            ),
            packets=packets,
        )

    # -- synthesis -----------------------------------------------------

    async def _run_synthesis(self, session: RunSession) -> SupervisorResponse | None:
        state = session.state
        stage = "synthesis"
        system, user = phases.synthesis_prompt(state, state.mode)

        if self._delegated(stage):
            agent = self._stage_agent(session, "synthesizer", stage)
            if state.agents[agent.id].status is AgentStatus.DONE:
                return self._error(session, "synthesis agent finished without producing tasks")
            packet = self._stage_packet(session, agent, system, user, SYNTHESIS_SCHEMA, "synthesis")
            return SupervisorResponse(
                run_id=state.id, phase=str(state.phase), action="dispatch",
                message="Synthesis stage. Merge the findings, then report with supervisor_report.",
                packets=[packet],
            )

        data = await self._call(stage, system, user, SYNTHESIS_SCHEMA)
        self._apply_synthesis(session, data)
        return None

    def _apply_synthesis(self, session: RunSession, data: dict[str, Any]) -> None:
        state = session.state
        report = phases.build_report(state, data)
        session.emit(EventType.REPORT_WRITTEN, {"report": to_jsonable(report)})

        tasks = parse_tasks(data, state.id)
        wants_execution = (
            state.mode is RunMode.EXECUTE
            or (state.mode is RunMode.AUTO and report.recommended_mode is RunMode.EXECUTE)
        )

        if not tasks or not wants_execution:
            self._write_report_artifact(session)
            self._transition(session, Phase.IMPROVING)
            return

        tasks, notes = phases.prepare_tasks(tasks, self.config.policy)
        for task in tasks:
            session.emit(EventType.TASK_PROPOSED, {"task": to_jsonable(task),
                                                   "notes": notes.get(task.id, [])})
        session.emit(EventType.NOTE, {"text": "tasks proposed", "notes": notes})
        self._transition(session, Phase.AWAITING_APPROVAL)

    def _await_approval(self, session: RunSession) -> SupervisorResponse | None:
        state = session.state
        proposed = [t for t in state.tasks.values() if t.status is TaskStatus.PROPOSED]
        if not proposed:
            self._transition(session, Phase.EXECUTING)
            return None

        notes: dict[str, list[str]] = {}
        for event in session.events():
            if event.type is EventType.TASK_PROPOSED and event.payload.get("notes"):
                task = event.payload.get("task") or {}
                notes[str(task.get("id"))] = list(event.payload["notes"])

        return SupervisorResponse(
            run_id=state.id,
            phase=str(state.phase),
            action="await_approval",
            message=(
                f"{len(proposed)} task(s) proposed. Present them to the user with their "
                "actions, motivations and definitions of done, and ask which to approve, "
                "modify or reject. Then call supervisor_approve with the decisions."
            ),
            tasks=[self._task_view(t) for t in proposed],
            task_notes=notes,
        )

    # -- execution -----------------------------------------------------

    async def _continue_execution(self, session: RunSession) -> SupervisorResponse | None:
        state = session.state
        active = [
            a for a in state.agents.values()
            if a.kind is AgentKind.EXECUTION and a.status in ACTIVE_AGENT_STATUSES
        ]

        if not active:
            registry = self._registry_for(session, None)
            fresh: list[AgentSpec] = []
            for task in phases.runnable_tasks(state):
                if task.assigned_agent_id and task.status is not TaskStatus.FAILED:
                    continue
                agent = phases.build_execution_agent(state, task, self.config, registry)
                fresh.append(agent)
                task.assigned_agent_id = agent.id
                task.status = TaskStatus.IN_PROGRESS
                task.attempts += 1
                task.updated_at = now_iso()
                session.emit(EventType.TASK_UPDATED, {"task": to_jsonable(task)})
            if fresh:
                self._spawn(session, fresh)
                active = fresh
            else:
                self._transition(session, Phase.VERIFYING)
                return None

        if state.backend is Backend.AUTONOMOUS:
            await self._run_agents_autonomously(session, active)
            return None

        packets = [self._agent_packet(session, agent) for agent in active]
        return SupervisorResponse(
            run_id=state.id, phase=str(state.phase), action="dispatch",
            message=(
                f"Execution phase: {len(packets)} approved task(s). Dispatch the agents "
                "in parallel where their scopes do not overlap, and report each turn."
            ),
            packets=packets,
        )

    # -- verification --------------------------------------------------

    async def _continue_verification(self, session: RunSession) -> SupervisorResponse | None:
        state = session.state

        # Prove what can be proven here before asking anyone to look at it.
        self._verify_mechanically(session)

        active = [
            a for a in state.agents.values()
            if a.kind is AgentKind.VERIFICATION and a.status in ACTIVE_AGENT_STATUSES
        ]
        if not active:
            registry = self._registry_for(session, None)
            fresh: list[AgentSpec] = []
            for task in state.tasks.values():
                if task.status is not TaskStatus.AWAITING_VERIFICATION:
                    continue
                if any(a.task_id == task.id and a.kind is AgentKind.VERIFICATION
                       for a in state.agents.values()):
                    continue
                fresh.append(phases.build_verification_agent(state, task, self.config, registry))
            if fresh:
                self._spawn(session, fresh)
                active = fresh
            else:
                self._settle_tasks(session)
                self._transition(session, Phase.CHECKPOINT)
                return None

        if state.backend is Backend.AUTONOMOUS:
            await self._run_agents_autonomously(session, active)
            return None

        packets = [self._agent_packet(session, agent) for agent in active]
        return SupervisorResponse(
            run_id=state.id, phase=str(state.phase), action="dispatch",
            message=(
                f"Verification phase: {len(packets)} task(s) to verify. Run each command "
                "for real and report the actual output -- a criterion is not proven by "
                "an implementer's assurance."
            ),
            packets=packets,
        )

    def _verify_mechanically(self, session: RunSession) -> None:
        """Close the criteria the harness can prove without asking anyone."""
        state = session.state
        allow = self.config.policy.allow_command_execution
        for task in state.tasks.values():
            if task.status not in (TaskStatus.AWAITING_VERIFICATION, TaskStatus.IN_PROGRESS):
                continue
            for crit in task.dod:
                if crit.status is not CriterionStatus.UNVERIFIED:
                    continue
                outcome = verify_criterion(
                    crit, self.workspace, self.config.policy, allow_commands=allow
                )
                if outcome is None:
                    continue
                session.emit(
                    EventType.CRITERION_VERIFIED,
                    {
                        "task_id": task.id,
                        "criterion_id": crit.id,
                        "status": str(outcome.status),
                        "evidence": outcome.evidence,
                    },
                    actor="harness",
                )

    def _settle_tasks(self, session: RunSession) -> None:
        state = session.state
        for task in state.tasks.values():
            if task.status not in (TaskStatus.AWAITING_VERIFICATION, TaskStatus.IN_PROGRESS):
                continue
            task.status = TaskStatus.VERIFIED if task.dod_satisfied() else TaskStatus.FAILED
            task.updated_at = now_iso()
            session.emit(EventType.TASK_UPDATED, {"task": to_jsonable(task)})

    # -- checkpoint ----------------------------------------------------

    async def _run_checkpoint(self, session: RunSession) -> SupervisorResponse | None:
        state = session.state
        iteration = state.checkpoint_iteration + 1
        deterministic = phases.deterministic_checkpoint(state, self.config.policy, iteration)
        stage = "verification"

        if self._delegated(stage):
            agent = self._stage_agent(session, "checkpointer", stage, iteration=iteration)
            system, user = phases.checkpoint_prompt(state, deterministic)
            packet = self._stage_packet(session, agent, system, user, CHECKPOINT_SCHEMA, "checkpoint")
            return SupervisorResponse(
                run_id=state.id, phase=str(state.phase), action="dispatch",
                message=(
                    "Checkpoint stage. Judge quality, scope fidelity and completeness "
                    "against the mechanical scoring, then report."
                ),
                packets=[packet],
                checkpoint=to_jsonable(deterministic),
            )

        system, user = phases.checkpoint_prompt(state, deterministic)
        data = await self._call(stage, system, user, CHECKPOINT_SCHEMA)
        judged = parse_checkpoint(data, state.id, iteration)
        self._apply_checkpoint(session, deterministic, judged)
        return None

    def _apply_checkpoint(
        self, session: RunSession, deterministic: Checkpoint, judged: Checkpoint
    ) -> None:
        state = session.state
        merged = phases.merge_checkpoint(deterministic, judged, self.config.policy)
        session.emit(EventType.CHECKPOINT_RECORDED, {"checkpoint": to_jsonable(merged)})

        if merged.passed or merged.iteration >= self.config.policy.max_checkpoint_iterations:
            if not merged.passed:
                session.note(
                    "checkpoint not passed and remediation budget exhausted",
                    iteration=merged.iteration,
                )
            self._transition(session, Phase.IMPROVING)
            return

        # Send the failing tasks back with the checkpoint's own corrections.
        remediated = self._remediate(session, merged)
        if not remediated:
            session.note("checkpoint failed but produced no actionable remediation")
            self._transition(session, Phase.IMPROVING)
            return
        self._transition(session, Phase.EXECUTING)

    def _remediate(self, session: RunSession, checkpoint: Checkpoint) -> int:
        """Reopen the tasks that fell short, carrying the corrections into their brief."""
        state = session.state
        count = 0
        corrections = checkpoint.remediation or checkpoint.gaps
        for task in state.tasks.values():
            if task.status is not TaskStatus.FAILED or task.attempts >= self.config.policy.max_task_attempts:
                continue
            task.status = TaskStatus.APPROVED
            task.assigned_agent_id = None
            task.action = (
                f"{task.action}\n\nCheckpoint corrections from attempt {task.attempts}:\n"
                + "\n".join(f"- {c}" for c in corrections[:6])
            )
            task.updated_at = now_iso()
            session.emit(EventType.TASK_UPDATED, {"task": to_jsonable(task)})
            count += 1

        for agent in state.agents.values():
            if agent.kind in (AgentKind.EXECUTION, AgentKind.VERIFICATION):
                if agent.status in ACTIVE_AGENT_STATUSES:
                    self._set_status(session, agent, AgentStatus.STOPPED)
        return count

    # -- improvement ---------------------------------------------------

    async def _run_improvement(self, session: RunSession) -> SupervisorResponse | None:
        state = session.state
        if not self.config.policy.learn_from_failures:
            return self._complete(session)

        for lesson in phases.mechanical_lessons(state):
            self._record_lesson(session, lesson)

        stage = "improvement"
        checkpoint = state.checkpoints[-1] if state.checkpoints else None

        already_ran = any(
            a.role == "improver" and a.status is AgentStatus.DONE for a in state.agents.values()
        )
        if already_ran:
            return self._complete(session)

        if self._delegated(stage):
            agent = self._stage_agent(session, "improver", stage)
            system, user = phases.lessons_prompt(state, checkpoint)
            packet = self._stage_packet(session, agent, system, user, LESSONS_SCHEMA, "improvement")
            return SupervisorResponse(
                run_id=state.id, phase=str(state.phase), action="dispatch",
                message="Improvement stage. Extract reusable lessons from this run, then report.",
                packets=[packet],
            )

        system, user = phases.lessons_prompt(state, checkpoint)
        try:
            data = await self._call(stage, system, user, LESSONS_SCHEMA)
        except Exception as exc:  # noqa: BLE001 - never fail a run over the learning pass
            session.note(f"improvement stage skipped: {exc}")
            return self._complete(session)

        for lesson in parse_lessons(data, state.id):
            self._record_lesson(session, lesson)
        return self._complete(session)

    def _record_lesson(self, session: RunSession, lesson: Any) -> None:
        stored = self.store.add_lesson(lesson)
        session.emit(EventType.LESSON_LEARNED, {"lesson": to_jsonable(stored)})

    # ------------------------------------------------------------------
    # Turn reporting and supervision
    # ------------------------------------------------------------------

    async def report(
        self, run_id: str, agent_id: str, payload: dict[str, Any]
    ) -> SupervisorResponse:
        """Record one agent turn, supervise it, and say what happens next."""
        session = self.store.open(run_id)
        self._registry_for(session, None)
        state = session.state

        agent = state.agents.get(agent_id)
        if agent is None:
            return self._error(session, f"unknown agent {agent_id!r} for run {run_id}")

        if agent.kind is AgentKind.SYNTHESIS:
            return await self._report_stage(session, agent, payload)

        if agent.kind is AgentKind.VERIFICATION:
            return self._report_verification(session, agent, payload)

        turn = self._record_turn(session, agent, payload)
        directive = self._supervise(session, agent, turn)

        if agent.kind is AgentKind.EXECUTION and directive.kind in (
            DirectiveKind.ACCEPT, DirectiveKind.STOP
        ):
            self._mark_task_awaiting_verification(session, agent, payload)

        return self._after_directive(session, agent, directive)

    def _record_turn(
        self, session: RunSession, agent: AgentSpec, payload: dict[str, Any]
    ) -> AgentTurn:
        state = session.state
        seq = state.turn_counts.get(agent.id, 0) + 1
        lens = agent.role

        turn = AgentTurn(
            run_id=state.id,
            agent_id=agent.id,
            seq=seq,
            reasoning=str(payload.get("reasoning", "")),
            output=str(payload.get("output", "")),
            findings=parse_findings(payload, agent.id, lens),
            artifacts=[str(a) for a in (payload.get("artifacts") or [])],
            files_touched=[
                str(f) for f in (payload.get("files_touched") or payload.get("files_examined") or [])
            ],
            messages=parse_messages(payload, state.id, agent.id),
            claimed_status=parse_status(payload.get("status")),
            self_assessment=str(payload.get("self_assessment", "")),
            blocked_on=str(payload.get("blocked_on", "")),
            usage=self._usage_from(payload),
        )

        session.emit(EventType.TURN_RECORDED, {"turn": to_jsonable(turn)}, actor=agent.id)
        for finding in turn.findings:
            session.emit(EventType.FINDING_ADDED, {"finding": to_jsonable(finding)}, actor=agent.id)
        self._route_messages(session, turn)
        return turn

    def _route_messages(self, session: RunSession, turn: AgentTurn) -> None:
        board = self._board(session.state.id)
        for message in turn.messages:
            routing = board.route(message, session.state)
            session.emit(
                EventType.MESSAGE_SENT,
                {"message": to_jsonable(routing.message), "deliver_to": routing.deliver_to,
                 "escalated": routing.escalate},
                actor=turn.agent_id,
            )

    def _supervise(self, session: RunSession, agent: AgentSpec, turn: AgentTurn) -> Directive:
        """Assess the turn and issue the directive that governs the next one."""
        state = session.state
        turns_used = state.turn_counts.get(agent.id, 0)
        brief = self._briefs.get(agent.id, agent.brief)

        previous = self._previous_turns(session, agent.id, before=turn.id)

        ctx = TurnContext(
            agent=agent,
            turn=turn,
            previous_turns=previous,
            brief=brief,
            task_prompt=state.prompt,
            turn_index=max(0, turns_used - 1),
        )
        assessment = assess_heuristically(ctx)
        session.emit(
            EventType.DRIFT_ASSESSED,
            {"agent_id": agent.id, "assessment": to_jsonable(assessment)},
        )

        inbox = Blackboard.inbox_for(agent.id, state)
        prior_corrections = sum(
            1 for d in state.directives
            if d.agent_id == agent.id
            and d.kind in (DirectiveKind.REFOCUS, DirectiveKind.NARROW, DirectiveKind.REJECT)
        )
        directive = decide_directive(
            assessment, agent, turn, self.config.policy, turns_used,
            inbox=inbox, prior_corrections=prior_corrections,
        )
        session.emit(EventType.DIRECTIVE_ISSUED, {"directive": to_jsonable(directive)})
        if inbox:
            session.emit(EventType.MESSAGE_DELIVERED, {"message_ids": [m.id for m in inbox]})

        self._set_status(session, agent, status_after(directive))
        return directive

    async def supervise_with_model(
        self, run_id: str, agent_id: str
    ) -> dict[str, Any]:
        """Escalate the last turn's drift assessment to the drift-stage model.

        Exposed separately so the host can decide to pay for a second opinion,
        and so autonomous runs can escalate inline.
        """
        session = self.store.open(run_id)
        state = session.state
        agent = state.agents.get(agent_id)
        heuristic = state.drift.get(agent_id)
        if agent is None or heuristic is None:
            return {"error": "no assessment to escalate"}

        turns = self._previous_turns(session, agent_id)
        if not turns:
            return {"error": "no turns recorded"}
        last = turns[-1]

        system = (
            "You supervise agents for scope drift. Decide whether the agent is still "
            "doing the job it was briefed to do. Judge the substance, not the tone: an "
            "agent working carefully on the wrong thing is drifting; an agent working "
            "roughly on the right thing is not."
        )
        user = (
            f"# The agent's objectives\n" + "\n".join(f"- {o}" for o in agent.objectives)
            + f"\n\n# Explicitly out of scope\n"
            + ("\n".join(f"- {o}" for o in agent.scope.out_of_scope) or "(nothing listed)")
            + f"\n\n# The overall task\n{state.prompt}"
            + f"\n\n# What the agent just reported\n{last.output[:4000]}"
            + f"\n\n# Mechanical signals already detected\n"
            + ("\n".join(f"- {s.kind}: {s.detail}" for s in heuristic.signals) or "(none)")
        )
        data = await self._call("drift", system, user, DRIFT_SCHEMA)
        model_view = parse_drift(data, checked_by=self.router.binding("drift").ref())
        merged = merge_assessments(heuristic, model_view)
        session.emit(
            EventType.DRIFT_ASSESSED,
            {"agent_id": agent_id, "assessment": to_jsonable(merged)},
        )
        session.sync_index()
        return to_jsonable(merged)

    def _after_directive(
        self, session: RunSession, agent: AgentSpec, directive: Directive
    ) -> SupervisorResponse:
        """Either hand the agent a continuation packet, or let the phase move on."""
        state = session.state
        response = SupervisorResponse(
            run_id=state.id,
            phase=str(state.phase),
            action="await_reports",
            directive=to_jsonable(directive),
            message=directive.rationale,
        )

        if directive.kind in (DirectiveKind.ACCEPT, DirectiveKind.STOP, DirectiveKind.ESCALATE):
            remaining = [a for a in state.agents.values()
                         if a.kind is agent.kind and a.status in ACTIVE_AGENT_STATUSES]
            response.message = (
                f"Agent `{agent.id}` finished ({directive.kind.value}). "
                + (f"{len(remaining)} agent(s) still running."
                   if remaining else "Call supervisor_advance to continue the run.")
            )
            response.detail = {"agents_remaining": len(remaining)}
            return response

        packet = self._agent_packet(session, agent, directive=directive)
        response.action = "dispatch"
        response.packets = [packet]
        response.message = (
            f"Agent `{agent.id}`: {directive.kind.value}. Run the continuation packet "
            "and report again."
        )
        return response

    def _mark_task_awaiting_verification(
        self, session: RunSession, agent: AgentSpec, payload: dict[str, Any]
    ) -> None:
        state = session.state
        task = state.tasks.get(agent.task_id or "")
        if task is None:
            return
        for claim in payload.get("criteria_progress") or []:
            if not isinstance(claim, dict):
                continue
            session.note(
                "implementer claim recorded (not a verification)",
                task_id=task.id,
                criterion_id=str(claim.get("criterion_id", "")),
                claim=str(claim.get("claim", "")),
                evidence=str(claim.get("evidence", ""))[:500],
                actor=agent.id,
            )
        task.status = TaskStatus.AWAITING_VERIFICATION
        task.updated_at = now_iso()
        session.emit(EventType.TASK_UPDATED, {"task": to_jsonable(task)})

    def _report_verification(
        self, session: RunSession, agent: AgentSpec, payload: dict[str, Any]
    ) -> SupervisorResponse:
        state = session.state
        task = state.tasks.get(agent.task_id or "")
        if task is None:
            return self._error(session, f"verification agent {agent.id} has no task")

        by_id = {c.id: c for c in task.dod}
        applied = 0
        for result in payload.get("results") or []:
            if not isinstance(result, dict):
                continue
            crit = by_id.get(str(result.get("criterion_id", "")))
            if crit is None:
                continue
            raw = str(result.get("status", "")).lower()
            status = {
                "pass": CriterionStatus.PASS,
                "fail": CriterionStatus.FAIL,
                "blocked": CriterionStatus.BLOCKED,
            }.get(raw)
            if status is None:
                continue
            # Evidence is not optional: a pass with no evidence is not a pass.
            evidence = str(result.get("evidence", "")).strip()
            if status is CriterionStatus.PASS and not evidence:
                status = CriterionStatus.FAIL
                evidence = "marked passed without evidence; rejected by the supervisor"
            session.emit(
                EventType.CRITERION_VERIFIED,
                {"task_id": task.id, "criterion_id": crit.id,
                 "status": str(status), "evidence": evidence},
                actor=agent.id,
            )
            applied += 1

        self._set_status(session, agent, AgentStatus.DONE)
        task = state.tasks[task.id]
        task.status = TaskStatus.VERIFIED if task.dod_satisfied() else TaskStatus.FAILED
        task.updated_at = now_iso()
        session.emit(EventType.TASK_UPDATED, {"task": to_jsonable(task)})
        session.sync_index()

        unmet = task.unmet_criteria()
        return SupervisorResponse(
            run_id=state.id, phase=str(state.phase), action="await_reports",
            message=(
                f"Verified {applied} criterion result(s) for `{task.id}`: "
                + ("all mandatory criteria proven." if not unmet
                   else f"{len(unmet)} still unmet -- " + "; ".join(c.statement for c in unmet[:3]))
            ),
            detail={"task_id": task.id, "satisfied": task.dod_satisfied(),
                    "unmet": [c.statement for c in unmet]},
        )

    async def _report_stage(
        self, session: RunSession, agent: AgentSpec, payload: dict[str, Any]
    ) -> SupervisorResponse:
        """Apply the result of a planning, synthesis, checkpoint or improvement turn."""
        state = session.state
        session.emit(
            EventType.TURN_RECORDED,
            {"turn": to_jsonable(AgentTurn(
                run_id=state.id, agent_id=agent.id, seq=1,
                reasoning=str(payload.get("reasoning", "")),
                output=str(payload.get("summary") or payload.get("restated_goal") or "")[:4000],
                usage=self._usage_from(payload),
            ))},
            actor=agent.id,
        )
        self._set_status(session, agent, AgentStatus.DONE)

        if agent.role == "planner":
            registry = self._registry_for(session, None)
            lenses = phases.plan_lenses(state, self.config)
            fallback = phases.build_analysis_agents(state, self.config, registry, lenses)
            self._apply_plan(session, payload, fallback, registry)
        elif agent.role == "synthesizer":
            self._apply_synthesis(session, payload)
        elif agent.role == "checkpointer":
            iteration = state.checkpoint_iteration + 1
            deterministic = phases.deterministic_checkpoint(state, self.config.policy, iteration)
            self._apply_checkpoint(session, deterministic,
                                   parse_checkpoint(payload, state.id, iteration))
        elif agent.role == "improver":
            for lesson in parse_lessons(payload, state.id):
                self._record_lesson(session, lesson)

        return await self._advance(session)

    # ------------------------------------------------------------------
    # Approval
    # ------------------------------------------------------------------

    async def approve(
        self, run_id: str, decisions: list[dict[str, Any]] | list[TaskDecision]
    ) -> SupervisorResponse:
        """Apply the user's decisions on proposed tasks."""
        session = self.store.open(run_id)
        self._registry_for(session, None)
        state = session.state

        applied = 0
        for raw in decisions:
            decision = raw if isinstance(raw, TaskDecision) else _coerce_decision(raw)
            task = state.tasks.get(decision.task_id)
            if task is None:
                continue
            _apply_modifications(task, decision.modifications)
            task.decision = decision.decision
            task.decision_note = decision.note
            task.status = {
                Decision.APPROVE: TaskStatus.APPROVED,
                Decision.MODIFY: TaskStatus.APPROVED,
                Decision.REJECT: TaskStatus.REJECTED,
                Decision.DEFER: TaskStatus.DEFERRED,
            }[decision.decision]
            task.updated_at = now_iso()
            session.emit(EventType.TASK_DECIDED, {"task": to_jsonable(task)})
            applied += 1

        approved = [t for t in state.tasks.values() if t.status is TaskStatus.APPROVED]
        if not approved:
            session.note("no tasks approved; ending with the analysis report")
            self._write_report_artifact(session)
            self._transition(session, Phase.IMPROVING)
        else:
            self._transition(session, Phase.EXECUTING)

        response = await self._advance(session)
        response.detail = {**response.detail, "decisions_applied": applied,
                           "approved": len(approved)}
        return response

    # ------------------------------------------------------------------
    # Autonomous execution
    # ------------------------------------------------------------------

    async def _run_agents_autonomously(
        self, session: RunSession, agents: list[AgentSpec]
    ) -> None:
        """Run agents in parallel against their bound models, supervising each turn."""
        limit = asyncio.Semaphore(self.config.policy.max_parallel_agents)

        async def drive(agent: AgentSpec) -> None:
            async with limit:
                await self._drive_agent(session, agent)

        await asyncio.gather(*(drive(a) for a in agents), return_exceptions=True)

    async def _drive_agent(self, session: RunSession, agent: AgentSpec) -> None:
        """Run one agent to completion against its bound model.

        Two nested loops. The inner one services tool calls, which do not consume
        the agent's turn budget -- reading three files to answer a question is one
        piece of work, not three. The outer one is the supervised loop: each real
        answer is recorded, assessed for drift, and answered with a directive.
        """
        packet = self._agent_packet(session, agent)
        history: list[ChatMessage] = [ChatMessage("user", packet.brief)]

        for _ in range(agent.budget.max_turns):
            payload: dict[str, Any] | None = None
            raw_text = ""

            for tool_round in range(MAX_TOOL_ROUNDS + 1):
                try:
                    response = await self.router.complete(
                        self._stage_for(agent),
                        CompletionRequest(
                            messages=history,
                            system="You are a supervised agent. Answer only with the JSON "
                                   "object your brief specifies.",
                            json_schema=packet.schema,
                            max_tokens=4096,
                        ),
                        binding=agent.binding,
                    )
                except Exception as exc:  # noqa: BLE001 - one agent must not kill the run
                    session.note(f"agent failed: {exc}", actor=agent.id)
                    self._set_status(session, agent, AgentStatus.FAILED)
                    return

                payload = response.json(required=False)
                raw_text = response.text
                payload.setdefault("usage", to_jsonable(response.usage))
                if response.reasoning and not payload.get("reasoning"):
                    payload["reasoning"] = response.reasoning

                calls = parse_tool_calls(payload)
                if not calls:
                    break
                if tool_round >= MAX_TOOL_ROUNDS:
                    history.append(ChatMessage(
                        "user",
                        f"You have used all {MAX_TOOL_ROUNDS} tool rounds for this turn. "
                        "Answer now with what you have, and say plainly what you could "
                        "not establish.",
                    ))
                    continue

                results = [self.toolbox.call(name, args, agent) for name, args in calls]
                session.note(
                    "tools called",
                    actor=agent.id,
                    tools=[name for name, _ in calls],
                    failures=[r.tool for r in results if not r.ok],
                )
                history = [
                    ChatMessage("user", packet.brief),
                    ChatMessage("assistant", raw_text[:4000]),
                    ChatMessage("user", render_results(results)),
                ]

            if payload is None:
                self._set_status(session, agent, AgentStatus.FAILED)
                return

            if agent.kind is AgentKind.VERIFICATION:
                self._report_verification(session, agent, payload)
                return

            turn = self._record_turn(session, agent, payload)
            directive = self._supervise(session, agent, turn)

            if should_escalate(
                session.state.drift.get(agent.id, DriftAssessment()),
                self.config.policy,
                turn.seq - 1,
            ):
                try:
                    await self.supervise_with_model(session.state.id, agent.id)
                except Exception:  # noqa: BLE001 - a failed second opinion is not fatal
                    pass

            if directive.kind in (DirectiveKind.ACCEPT, DirectiveKind.STOP, DirectiveKind.ESCALATE):
                if agent.kind is AgentKind.EXECUTION:
                    self._mark_task_awaiting_verification(session, agent, payload)
                return

            history = [
                ChatMessage("user", packet.brief),
                ChatMessage("assistant", raw_text[:6000]),
                ChatMessage("user", render_directive(directive, agent)),
            ]

    async def run(
        self,
        prompt: str,
        *,
        mode: RunMode = RunMode.AUTO,
        auto_approve: bool = False,
    ) -> SupervisorResponse:
        """Drive a run to completion without a host. Requires a non-host backend.

        ``auto_approve`` accepts every proposed task, which is only appropriate
        for unattended use where the user has already accepted that trade.
        """
        response = await self.start(prompt, mode=mode, backend=Backend.AUTONOMOUS)
        while response.action not in ("complete", "failed"):
            if response.action == "await_approval":
                if not auto_approve:
                    return response
                response = await self.approve(
                    response.run_id,
                    [{"task_id": t["id"], "decision": "approve"} for t in response.tasks],
                )
                continue
            response = await self.advance(response.run_id)
        return response

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _delegated(self, stage: str) -> bool:
        return self.router.is_host(stage)

    def _stage_for(self, agent: AgentSpec) -> str:
        role = ROLES_BY_ID.get(agent.role)
        return role.stage if role else STAGE_ROLES.get(agent.role, ("default", None))[0]

    async def _call(
        self, stage: str, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        response = await self.router.complete(
            stage,
            CompletionRequest(
                system=system,
                messages=[ChatMessage("user", user)],
                json_schema=schema,
                max_tokens=8192,
            ),
        )
        return response.json()

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

    def _board(self, run_id: str) -> Blackboard:
        if run_id not in self._boards:
            self._boards[run_id] = Blackboard(run_id)
        return self._boards[run_id]

    def _spawn(self, session: RunSession, specs: list[AgentSpec]) -> None:
        for spec in specs:
            session.emit(EventType.AGENT_SPAWNED, {"agent": to_jsonable(spec)})

    def _set_status(self, session: RunSession, agent: AgentSpec, status: AgentStatus) -> None:
        if agent.status is status:
            return
        session.emit(EventType.AGENT_STATUS, {"agent_id": agent.id, "status": str(status)})

    def _stage_agent(
        self, session: RunSession, role: str, stage: str, **extra: Any
    ) -> AgentSpec:
        """Find or create the pseudo-agent that carries out a supervisory stage."""
        state = session.state
        for agent in state.agents.values():
            if agent.role == role and agent.status in ACTIVE_AGENT_STATUSES:
                return agent
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
        self._briefs[agent.id] = brief
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

    def _agent_packet(
        self, session: RunSession, agent: AgentSpec, directive: Directive | None = None
    ) -> WorkPacket:
        state = session.state
        board = self._board(state.id)
        peers = [a for a in state.agents.values() if a.kind is agent.kind]
        turns_used = state.turn_counts.get(agent.id, 0)

        # A host-run agent uses the host's own tools and must not be told about
        # the harness's; only an agent the harness drives itself gets this.
        tools = (
            render_tools_section(agent, self.config.policy)
            if agent.backend is Backend.AUTONOMOUS
            else ""
        )

        if directive is not None:
            brief = render_directive(directive, agent)
            schema = self._schema_for(agent)
        elif agent.kind is AgentKind.ANALYSIS:
            schema = ANALYSIS_TURN_SCHEMA
            brief = build_analysis_brief(
                state, agent, ROLES_BY_ID.get(agent.role), peers, schema,
                shared_context=board.context_for(agent),
                lessons=self.store.lessons_for([agent.role], self.config.policy.max_lessons_in_brief)
                if self.config.policy.apply_lessons else [],
                tools=tools,
            )
            self._briefs[agent.id] = brief
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
                shared_context=board.context_for(agent),
                lessons=self.store.lessons_for([agent.role], self.config.policy.max_lessons_in_brief)
                if self.config.policy.apply_lessons else [],
                supporting_findings=findings,
                tools=tools,
            )
            self._briefs[agent.id] = brief
        else:
            schema = VERIFICATION_SCHEMA
            task = state.tasks.get(agent.task_id or "")
            summary = self._change_summary(session, task)
            brief = build_verification_brief(
                state, agent, task or ExecutionTask(run_id=state.id, title=agent.title),
                schema, change_summary=summary, tools=tools,
            )
            self._briefs[agent.id] = brief

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

    def _schema_for(self, agent: AgentSpec) -> dict[str, Any]:
        return {
            AgentKind.ANALYSIS: ANALYSIS_TURN_SCHEMA,
            AgentKind.EXECUTION: EXECUTION_TURN_SCHEMA,
            AgentKind.VERIFICATION: VERIFICATION_SCHEMA,
        }.get(agent.kind, ANALYSIS_TURN_SCHEMA)

    def _change_summary(self, session: RunSession, task: ExecutionTask | None) -> str:
        if task is None:
            return ""
        parts: list[str] = []
        for event in session.events():
            if event.type is not EventType.TURN_RECORDED:
                continue
            turn = event.payload.get("turn") or {}
            agent = session.state.agents.get(str(turn.get("agent_id", "")))
            if agent is None or agent.task_id != task.id:
                continue
            if turn.get("output"):
                parts.append(str(turn["output"]))
            if turn.get("files_touched"):
                parts.append("Files touched: " + ", ".join(str(f) for f in turn["files_touched"]))
        return "\n\n".join(parts[-4:])

    def _previous_turns(
        self, session: RunSession, agent_id: str, before: str | None = None
    ) -> list[AgentTurn]:
        from ..serde import from_jsonable

        turns: list[AgentTurn] = []
        for event in session.events():
            if event.type is not EventType.TURN_RECORDED:
                continue
            raw = event.payload.get("turn") or {}
            if raw.get("agent_id") != agent_id:
                continue
            if before and raw.get("id") == before:
                continue
            turns.append(from_jsonable(raw, AgentTurn))
        return turns

    @staticmethod
    def _usage_from(payload: dict[str, Any]) -> Usage:
        raw = payload.get("usage")
        if isinstance(raw, dict):
            return Usage(
                input_tokens=int(raw.get("input_tokens", 0) or 0),
                output_tokens=int(raw.get("output_tokens", 0) or 0),
                seconds=float(raw.get("seconds", 0) or 0),
                tool_calls=int(raw.get("tool_calls", 0) or 0),
            )
        return Usage()

    @staticmethod
    def _task_view(task: ExecutionTask) -> dict[str, Any]:
        return {
            "id": task.id,
            "title": task.title,
            "action": task.action,
            "motivation": task.motivation,
            "risk": str(task.risk),
            "effort": task.effort,
            "suggested_role": task.suggested_role,
            "depends_on": task.depends_on,
            "scope": to_jsonable(task.scope),
            "definition_of_done": [
                {
                    "id": c.id,
                    "statement": c.statement,
                    "method": str(c.method),
                    "command": c.command,
                    "expect": c.expect,
                    "rubric": c.rubric,
                    "mandatory": c.mandatory,
                }
                for c in task.dod
            ],
        }

    def _write_report_artifact(self, session: RunSession) -> None:
        markdown = phases.final_report_markdown(session.state)
        path = self.store.write_artifact(session.state.id, "report.md", markdown)
        session.emit(EventType.ARTIFACT_WRITTEN, {"path": str(path), "kind": "report"})

    def _complete(self, session: RunSession) -> SupervisorResponse:
        self._write_report_artifact(session)
        session.emit(EventType.RUN_ENDED, {"phase": str(Phase.COMPLETE)})
        session.sync_index()
        return self._final_response(session)

    def _final_response(self, session: RunSession) -> SupervisorResponse:
        state = session.state
        markdown = phases.final_report_markdown(state)
        satisfied = [t for t in state.tasks.values() if t.dod_satisfied()]
        executed = state.approved_tasks()
        return SupervisorResponse(
            run_id=state.id,
            phase=str(state.phase),
            action="failed" if state.phase is Phase.FAILED else "complete",
            message=(
                f"Run {state.phase.value}. "
                f"{len(satisfied)}/{len(executed)} task(s) meet their definition of done."
                if executed
                else f"Run {state.phase.value} with an analysis report."
            ),
            report_markdown=markdown,
            checkpoint=to_jsonable(state.checkpoints[-1]) if state.checkpoints else None,
            detail={
                "artifact": str(self.store.run_dir(state.id) / "artifacts" / "report.md"),
                "findings": len(state.findings),
                "lessons": len(state.lessons),
                "dod_satisfied": [t.id for t in satisfied],
                "dod_unmet": {
                    t.id: [c.statement for c in t.unmet_criteria()]
                    for t in executed if not t.dod_satisfied()
                },
            },
        )

    def _error(self, session: RunSession, message: str) -> SupervisorResponse:
        session.emit(EventType.RUN_ENDED, {"phase": str(Phase.FAILED), "error": message})
        session.sync_index()
        return SupervisorResponse(
            run_id=session.state.id, phase=str(Phase.FAILED), action="failed", message=message
        )

    async def aclose(self) -> None:
        await self.router.aclose()


# --------------------------------------------------------------------------
# Decision helpers
# --------------------------------------------------------------------------


def _coerce_decision(raw: dict[str, Any]) -> TaskDecision:
    from ..contracts import parse_decision

    return TaskDecision(
        task_id=str(raw.get("task_id", "")),
        decision=parse_decision(raw.get("decision")),
        note=str(raw.get("note", "")),
        modifications=dict(raw.get("modifications") or {}),
    )


_MODIFIABLE = {"title", "action", "motivation", "effort"}


def _apply_modifications(task: ExecutionTask, modifications: dict[str, Any]) -> None:
    """Apply the user's edits to an approved task.

    Only descriptive fields can be edited freely. Criteria may be replaced
    wholesale but never silently dropped, because weakening a definition of done
    after the fact is exactly how verification stops meaning anything.
    """
    for key, value in modifications.items():
        if key in _MODIFIABLE and isinstance(value, str):
            setattr(task, key, value)
        elif key == "dod" and isinstance(value, list):
            from ..contracts import parse_dod

            replacement = parse_dod(value)
            if replacement:
                task.dod = replacement
        elif key == "scope_paths" and isinstance(value, list):
            task.scope.paths = [str(v) for v in value]
