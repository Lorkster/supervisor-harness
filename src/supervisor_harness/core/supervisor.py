"""The supervisor: the state machine that drives a run.

One object serves both execution backends. In **host-delegated** mode a phase
produces :class:`WorkPacket` objects for Claude Code or Cursor to execute, and
the host reports each turn back through :meth:`Supervisor.report`. In
**autonomous** mode the supervisor runs the same packets itself against a model
provider.

Each reported turn goes through the same supervision either way: it is recorded,
assessed for drift, answered with a directive, and its messages are routed. Two
differences remain, and it is worth being exact about what each turns on, since
this paragraph used to call both "properties of the backend" and only one is:

* Drift escalation to a model is a property of the **stage's routing**, not of
  the backend. It needs the harness to make a model call, so it is skipped when
  the ``drift`` stage is itself routed to the host -- there is no one to ask
  without another round trip through the caller. A host-backend run that routes
  ``drift`` to a model provider does escalate, which is a supported and useful
  configuration, and it is why :meth:`_delegated` asks about routing rather than
  about ``state.backend``. The two cannot disagree in the other direction:
  :meth:`run` refuses to create an autonomous run with any stage routed to the
  host.
* Tool use, budget enforcement in wall-clock terms, and failure capture are
  properties of the **backend**. They apply only to agents the harness drives; a
  host-run agent uses the host's tools and fails in the host's own way.

Every phase transition and every turn is an event on the log first and an
in-memory change second, which is what makes a run resumable from any point.
"""

from __future__ import annotations

import asyncio
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..agents.brief import (
    render_directive,
)
from ..agents.registry import AgentRegistry
from ..agents.roles import ROLES_BY_ID
from ..config import KNOWN_STAGES, HarnessConfig, Policy, load_config
from ..contracts import (
    CHECKPOINT_SCHEMA,
    DRIFT_SCHEMA,
    LESSONS_SCHEMA,
    PLANNING_SCHEMA,
    SYNTHESIS_SCHEMA,
    parse_checkpoint,
    parse_drift,
    parse_established,
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
    BASELINE_FACT,
    SUPERVISOR,
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
    DoDCriterion,
    DriftAssessment,
    ExecutionTask,
    Phase,
    RunMode,
    RunState,
    Scope,
    ScopeEnvelope,
    TaskDecision,
    TaskStatus,
)
from ..providers.base import ChatMessage, CompletionRequest
from ..providers.router import ModelRouter
from ..serde import to_jsonable
from ..store.events import EventType
from ..store.runstore import RunSession, RunStore
from . import phases
from .baseline import git_baseline
from .blackboard import (
    Blackboard,
    answer_from_record,
)
from .dod import verify_criterion
from .drift import (
    TurnContext,
    assess_heuristically,
    decide_directive,
    merge_assessments,
    should_escalate,
    status_after,
)
from .envelope import Ceiling, attenuate, effective, establish, render, stale_reason
from .journal import RunJournal
from .packets import Packets
from .reporting import Reporting
from .responses import SupervisorResponse
from .tools import Toolbox, render_results

# Stage agents are ordinary agents so that planning, synthesis, the checkpoint
# and the improvement pass all flow through the same report/supervise path.
# Tool calls are cheap relative to a supervised turn, but an agent that keeps
# reading without answering is its own kind of drift, so rounds are capped.
MAX_TOOL_ROUNDS = 6

#: How many of an agent's questions the supervisor answers in one directive.
#: A directive an agent cannot read is not an answer, and an agent asking more
#: than this per turn has a briefing problem rather than a question.
MAX_QUESTIONS_PER_TURN = 3

#: Bounds on what one turn's tool history carries back into the next round.
#: Results accumulate across a turn now, so each block is capped rather than the
#: whole history being thrown away -- the rounds are already bounded by
#: MAX_TOOL_ROUNDS, so the total is too.
TOOL_ECHO_CHARS = 4000
TOOL_RESULT_CHARS = 8000

STAGE_ROLES = {
    "planner": ("planning", PLANNING_SCHEMA),
    "synthesizer": ("synthesis", SYNTHESIS_SCHEMA),
    "checkpointer": ("verification", CHECKPOINT_SCHEMA),
    "improver": ("improvement", LESSONS_SCHEMA),
}






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
        # Own what you made: a store passed in belongs to the caller and is not
        # ours to close, but one constructed here has no other owner and would
        # otherwise hold its index connection until the process ended.
        self._owns_store = store is None
        self.store = store or RunStore.discover(self.workspace)
        self.host = host or detect_host(self.workspace)
        self.router = router or ModelRouter(self.config, host_name=self.host.name)
        self.toolbox = Toolbox(self.workspace, self.config.policy, self.store.root)
        # The layers below the phase machine. Neither calls back into it, which
        # is what made them separable at all -- see docs/quality-assessment.md.
        self.reporting = Reporting(self.config, self.store)
        self.packets = Packets(self.config, self.store, self.workspace, self.host)

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
        self.packets._registry_for(session, host_agents)
        await session.anote(
            "run created",
            host=self.host.name,
            backend=str(session.state.backend),
            routing={k: v for k, v in self.config.routing.items()},
        )

        # Fixed at the start, before any agent has written anything, so every
        # brief in the run names the same commit. Agents share one working tree;
        # without a fixed point, a whole-repository criterion is measured against
        # whatever the other agents happened to have written by then.
        baseline = git_baseline(self.workspace)
        if baseline:
            await session.aemit(EventType.CONTEXT_SET, {"facts": {BASELINE_FACT: baseline}})

        # The run's grant, before any model has been asked anything. It is
        # recorded here rather than only at planning so that it exists for
        # every path out of this method -- including the ones where planning is
        # abandoned and the derived lens plan runs instead. The plan narrows it
        # afterwards; nothing widens it.
        self._set_envelope(session, self._configured_envelope(), source="configuration")

        return await self._advance(session)

    async def advance(self, run_id: str, host_agents: list[dict[str, Any]] | None = None) -> SupervisorResponse:
        session = self.store.open(run_id)
        self.packets._registry_for(session, host_agents)
        return await self._advance(session)

    async def resume(self, run_id: str) -> SupervisorResponse:
        """Reopen a persisted run and continue from wherever it stopped."""
        session = self.store.open(run_id)
        self._check_resume_fidelity(session)
        self.packets._registry_for(session, None)
        await session.anote("run resumed", phase=str(session.state.phase))
        return await self._advance(session)

    async def abandon(
        self, run_id: str, agent_id: str, reason: str = ""
    ) -> SupervisorResponse:
        """Give up on a host-run agent, and let its phase settle.

        The host is the only party that can know one of its subagents crashed or
        was cancelled: a host agent reports through the caller, so from here the
        difference between "still working" and "gone" is invisible. Without this
        the agent stays in ``ACTIVE_AGENT_STATUSES`` and its packet is re-emitted
        on every ``advance``, for as long as anyone keeps asking.
        """
        session = self.store.open(run_id)
        self._check_resume_fidelity(session)
        self.packets._registry_for(session, None)
        state = session.state

        agent = state.agents.get(agent_id)
        if agent is None:
            # As with a report for an unknown id: the caller's mistake must not
            # cost the run the work it has already done.
            await session.anote("abandon for an unknown agent was rejected", agent_id=agent_id)
            return SupervisorResponse(
                run_id=state.id,
                phase=str(state.phase),
                action="await_reports",
                message=(
                    f"No agent {agent_id!r} in this run; nothing was changed. "
                    f"Known agents: {', '.join(sorted(state.agents)) or '(none)'}."
                ),
                detail={"error": "unknown_agent", "known_agents": sorted(state.agents)},
            )

        if agent.status not in ACTIVE_AGENT_STATUSES:
            return SupervisorResponse(
                run_id=state.id,
                phase=str(state.phase),
                action="await_reports",
                message=(
                    f"Agent `{agent_id}` had already finished ({agent.status.value}); "
                    "nothing was changed."
                ),
                detail={"error": "already_settled", "status": str(agent.status)},
            )

        await self._abandon_agent(
            session, agent, reason.strip() or "the host reported it as gone"
        )
        return await self._advance(session)



    # ------------------------------------------------------------------
    # Phase machine
    # ------------------------------------------------------------------

    # -- reporting -------------------------------------------------------
    #
    # Kept here as one-line delegations because they are public API: the CLI,
    # the MCP server and the tests all call `supervisor.status(...)`. The bodies
    # are in `core/reporting.py`.

    def status(self, run_id: str) -> dict[str, Any]:
        return self.reporting.status(run_id)

    def explain(self, run_id: str, agent_id: str = "") -> RunJournal:
        return self.reporting.explain(run_id, agent_id)

    async def _advance(self, session: RunSession) -> SupervisorResponse:
        """Move the run forward until it needs something from outside."""
        # Each remediation cycle costs several internal steps, so the ceiling
        # has to scale with the checkpoint budget policy allows. A fixed 12 was
        # smaller than the shipped defaults require, and a fully-remediated run
        # was marked FAILED after doing all of its work.
        limit = 12 + 6 * max(1, self.config.policy.max_checkpoint_iterations)
        guard = 0
        while guard < limit:
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
                response = self.reporting._final_response(session)

            if response is not None:
                session.sync_index()
                return response
        return self.reporting._error(
            session,
            f"phase machine did not settle after {limit} steps; last phase "
            f"{session.state.phase.value}",
        )

    def _transition(self, session: RunSession, phase: Phase, **payload: Any) -> None:
        session.emit(EventType.PHASE_CHANGED, {"phase": str(phase), **payload})

    # -- planning ------------------------------------------------------

    async def _begin_planning(self, session: RunSession) -> SupervisorResponse | None:
        state = session.state
        registry = self.packets._registry_for(session, None)
        lenses = phases.plan_lenses(state, self.config)
        fallback = phases.build_analysis_agents(state, self.config, registry, lenses)

        system, user = phases.planning_prompt(state, registry, lenses)
        stage = "planning"

        if self._delegated(stage):
            agent = await self._stage_agent(session, "planner", stage)
            if agent is None:
                # Planning was abandoned. The lenses the harness derives itself
                # are a poorer plan than a model's, but they are a plan, and the
                # findings are what the run is for: it continues on them.
                await session.anote("planning abandoned; continuing on the derived lens plan")
                self._spawn(session, fallback)
                self._transition(session, Phase.ANALYZING)
                return None
            # The phase stays CREATED until the plan actually lands. It used to
            # move to ANALYZING here, before the planner had answered, which left
            # a window an ordinary call sequence walks straight into: an
            # ``advance`` while the planner packet is still out found the run in
            # ANALYZING with no analysts, and spawned the derived fallback fleet.
            # The planner's report then called ``_apply_plan``, which spawned the
            # model's fleet on top -- and the guard there tests the phase, which
            # was already ANALYZING, so nothing noticed. Two full analysis fleets
            # for one run. Re-entering planning re-issues the same packet to the
            # same agent instead, which the dispatch bound already accounts for.
            packet = self.packets._stage_packet(
                session, agent, system, user, PLANNING_SCHEMA, "planning"
            )
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

        facts = {}
        if plan.get("restated_goal"):
            facts["restated goal"] = str(plan["restated_goal"])
        if shared or facts:
            session.emit(
                EventType.CONTEXT_SET, {"shared_context": shared or "", "facts": facts}
            )

        # Recorded as its own event, not a note plus an in-memory assignment:
        # the fold has no branch for notes, so the resolved mode was lost on
        # every reopen and an EXECUTE run silently reverted to AUTO.
        if state.mode is RunMode.AUTO and mode is not RunMode.AUTO:
            session.emit(EventType.RUN_MODE_SET, {"mode": str(mode)})

        # The plan may narrow what the run may touch, and only narrow it. This
        # sits beside the mode because it is the same kind of fact: the shape of
        # the run, fixed once, before any task exists to argue about.
        envelope, refusals = establish(
            self._configured_envelope(),
            [str(x) for x in (plan.get("envelope_paths") or [])],
            [str(x) for x in (plan.get("envelope_forbidden_paths") or [])],
        )
        for text in refusals:
            session.note(text)
        self._set_envelope(session, envelope, source="run plan")

        # A fleet already running is the answer to this question, whoever asked
        # it. The plan can arrive after the derived fallback fleet has been
        # spawned -- planning abandoned, then reported late -- and adding the
        # model's lenses on top would run the analysis twice rather than better.
        # This is a second lock: the phase no longer moves to ANALYZING before
        # the plan lands, so the common route into the collision is closed above.
        running = [a for a in state.agents.values() if a.kind is AgentKind.ANALYSIS]
        if running:
            session.note(
                f"plan applied after {len(running)} analysis agent(s) were already "
                "spawned; keeping the running fleet rather than adding to it"
            )
        else:
            self._spawn(session, specs)

        if state.phase is not Phase.ANALYZING:
            self._transition(session, Phase.ANALYZING)

    # -- analysis ------------------------------------------------------

    async def _continue_analysis(self, session: RunSession) -> SupervisorResponse | None:
        state = session.state
        analysts = [a for a in state.agents.values() if a.kind is AgentKind.ANALYSIS]

        if not analysts:
            registry = self.packets._registry_for(session, None)
            lenses = phases.plan_lenses(state, self.config)
            self._spawn(session, phases.build_analysis_agents(state, self.config, registry, lenses))
            analysts = [a for a in state.agents.values() if a.kind is AgentKind.ANALYSIS]

        pending = await self._reap_unreported(
            session, [a for a in analysts if a.status in ACTIVE_AGENT_STATUSES]
        )
        if not pending:
            self._transition(session, Phase.SYNTHESIZING)
            return None

        if state.backend is Backend.AUTONOMOUS:
            await self._run_agents_autonomously(session, pending)
            return None

        packets = [self.packets._dispatch_packet(session, agent) for agent in pending]
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
            # Checked before ``_stage_agent`` rather than after it, which is what
            # made this dead code. ``_stage_agent`` only ever returns an agent in
            # ACTIVE_AGENT_STATUSES, so ``agent.status is DONE`` could not hold
            # on anything it handed back -- and a synthesizer that had finished
            # fell past it to a fresh spawn, with its own AGENT_SPAWNED event and
            # its own dispatched packet, on every ``advance``. Unbounded, because
            # nothing about the run had changed to stop the next one.
            #
            # The guard belongs here rather than in ``_stage_agent``: the
            # checkpointer legitimately needs a new agent per remediation
            # iteration under the same role, so refusing every finished stage
            # agent centrally would break the remediation loop.
            if any(
                a.role == "synthesizer" and a.status is AgentStatus.DONE
                for a in state.agents.values()
            ):
                return self.reporting._error(
                    session, "synthesis agent finished without producing tasks"
                )
            agent = await self._stage_agent(session, "synthesizer", stage)
            if agent is None:
                # Nothing downstream exists without synthesis: no report, no
                # tasks. The findings stay on the log, and the run says why it
                # stopped rather than re-issuing a packet nobody will run.
                return self.reporting._error(
                    session,
                    "synthesis was abandoned without being reported; the run has "
                    "findings but no merged report or tasks",
                )
            packet = self.packets._stage_packet(
                session, agent, system, user, SYNTHESIS_SCHEMA, "synthesis"
            )
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
            self.reporting._write_run_artifacts(session)
            self._transition(session, Phase.IMPROVING)
            return

        # Before anything reads depends_on: the model names dependencies by
        # title, and runnable_tasks matches on id. Unresolved, a dependent task
        # is approved and then never dispatched.
        dep_notes = phases.resolve_dependencies(tasks)
        # And before anything reads rationale_refs: the model is asked for
        # finding ids and often answers with titles, and a task that names no
        # finding leaves the end of the run unable to say what it closed.
        ref_notes = phases.resolve_rationale_refs(tasks, state.findings)
        tasks, notes = phases.prepare_tasks(tasks, self.config.policy, self.workspace)
        for extra in (dep_notes, ref_notes):
            for task_id, entries in extra.items():
                notes.setdefault(task_id, []).extend(entries)
        # Before the tasks are shown to anyone: a proposed scope wider than the
        # run's envelope is narrowed to the intersection, not refused. A model
        # proposing too much is ordinary; losing the task over it is not. The
        # narrowing is recorded against the task, which is what puts it in front
        # of the user at approval alongside the definition of done.
        for task in tasks:
            task.scope, clamped = attenuate(
                task.scope, [Ceiling.of("run envelope", effective(state.envelope))]
            )
            notes.setdefault(task.id, []).extend(clamped)
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

        notes = {t.id: list(state.task_notes[t.id]) for t in proposed if t.id in state.task_notes}

        return SupervisorResponse(
            run_id=state.id,
            phase=str(state.phase),
            action="await_approval",
            message=(
                f"{len(proposed)} task(s) proposed. Present them to the user with their "
                "actions, motivations and definitions of done, and ask which to approve, "
                "modify or reject. Then call supervisor_approve with the decisions."
            ),
            tasks=[self.reporting._task_view(t) for t in proposed],
            task_notes=notes,
            detail={"envelope": to_jsonable(effective(state.envelope))},
        )

    def _await_envelope_renewal(
        self, session: RunSession, reason: str
    ) -> SupervisorResponse:
        """Stop before execution and ask the user to re-grant the envelope.

        Deliberately shaped like the task-approval pause rather than like a
        failure. The run is not broken and nothing is wrong with it; the harness
        is applying its own rule -- nothing touches your code until you approve
        it -- to consent that has gone stale rather than to consent that was
        never given. Analysis and reporting reached here freely; only writing is
        held.
        """
        state = session.state
        envelope = effective(state.envelope)
        return SupervisorResponse(
            run_id=state.id,
            phase=str(state.phase),
            action="await_approval",
            message=(
                f"{reason}. Show the user what this run may modify and ask whether "
                "it still stands. Call supervisor_approve with renew_envelope=true "
                "to re-grant it, or leave the run as it is."
            ),
            detail={
                "envelope": to_jsonable(envelope),
                "needs": "envelope_renewal",
                "granted_at": envelope.granted_at,
            },
        )

    # -- execution -----------------------------------------------------

    async def _continue_execution(self, session: RunSession) -> SupervisorResponse | None:
        state = session.state
        active = await self._reap_unreported(session, [
            a for a in state.agents.values()
            if a.kind is AgentKind.EXECUTION and a.status in ACTIVE_AGENT_STATUSES
        ])

        # Before any *new* execution agent, and only new ones: an agent already
        # mid-flight was spawned under a grant that was current then, and ending
        # it would throw away work to make a point about a clock.
        stale = stale_reason(
            state.envelope, state.created_at, self.config.policy.envelope_max_age_days
        )
        if not active and stale is not None:
            return self._await_envelope_renewal(session, stale)

        if not active:
            registry = self.packets._registry_for(session, None)
            fresh: list[AgentSpec] = []
            for task in phases.runnable_tasks(state):
                if task.assigned_agent_id and task.status is not TaskStatus.FAILED:
                    continue
                # Count the attempt before building, so the agent is stamped
                # with the attempt it is actually working -- the verifier for
                # this attempt is matched against the same number.
                task.attempts += 1
                agent = phases.build_execution_agent(state, task, self.config, registry)
                fresh.append(agent)
                task.assigned_agent_id = agent.id
                task.status = TaskStatus.IN_PROGRESS
                task.updated_at = now_iso()
                await session.aemit(EventType.TASK_UPDATED, {"task": to_jsonable(task)})
            if fresh:
                # The state's own objects, not the ones just built: a status
                # change reaches these, and this loop drives them for the rest
                # of the phase.
                active = self._spawn(session, fresh)
            else:
                self._transition(session, Phase.VERIFYING)
                return None

        if state.backend is Backend.AUTONOMOUS:
            await self._run_agents_autonomously(session, active)
            return None

        packets = [self.packets._dispatch_packet(session, agent) for agent in active]
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

        active = await self._reap_unreported(session, [
            a for a in state.agents.values()
            if a.kind is AgentKind.VERIFICATION and a.status in ACTIVE_AGENT_STATUSES
        ])
        if not active:
            registry = self.packets._registry_for(session, None)
            fresh: list[AgentSpec] = []
            for task in state.tasks.values():
                if task.status is not TaskStatus.AWAITING_VERIFICATION:
                    continue
                # Match the attempt too. A retired verifier stays in
                # state.agents -- the fold replays it -- so without this the
                # first attempt's verifier suppresses every later one and a
                # remediated task is closed on stale evidence.
                if any(a.task_id == task.id and a.kind is AgentKind.VERIFICATION
                       and a.attempt == task.attempts
                       for a in state.agents.values()):
                    continue
                verifier = phases.build_verification_agent(state, task, self.config, registry)
                # The agent whose work this verifier judges, so attenuation can
                # hold the verifier to no more than the executor was given.
                executor = next(
                    (a for a in state.agents.values()
                     if a.task_id == task.id and a.kind is AgentKind.EXECUTION
                     and a.attempt == task.attempts),
                    None,
                )
                if executor is not None:
                    verifier.parent_agent_id = executor.id
                fresh.append(verifier)
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

        packets = [self.packets._dispatch_packet(session, agent) for agent in active]
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
            agent = await self._stage_agent(session, "checkpointer", stage)
            if agent is None:
                # The judged half of the checkpoint is gone, but the mechanical
                # half was computed here and stands on its own: the run is scored
                # on what the harness proved rather than left unjudged.
                await session.anote(
                    "checkpoint judgement abandoned; the mechanical scoring stands",
                    iteration=iteration,
                )
                # The mechanical scoring standing in for the judgement it did
                # not get: same numbers, and its gaps counted once.
                await self._apply_checkpoint(session, deterministic, replace(deterministic, gaps=[]))
                return None
            system, user = phases.checkpoint_prompt(state, deterministic)
            packet = self.packets._stage_packet(
                session, agent, system, user, CHECKPOINT_SCHEMA, "checkpoint"
            )
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
        await self._apply_checkpoint(session, deterministic, judged)
        return None

    async def _apply_checkpoint(
        self, session: RunSession, deterministic: Checkpoint, judged: Checkpoint
    ) -> None:
        merged = phases.merge_checkpoint(deterministic, judged, self.config.policy)
        await session.aemit(EventType.CHECKPOINT_RECORDED, {"checkpoint": to_jsonable(merged)})

        if merged.passed or merged.iteration >= self.config.policy.max_checkpoint_iterations:
            if not merged.passed:
                await session.anote(
                    "checkpoint not passed and remediation budget exhausted",
                    iteration=merged.iteration,
                )
            self._transition(session, Phase.IMPROVING)
            return

        # Send the failing tasks back with the checkpoint's own corrections.
        remediated = await self._remediate(session, merged)
        if not remediated:
            await session.anote("checkpoint failed but produced no actionable remediation")
            self._transition(session, Phase.IMPROVING)
            return
        self._transition(session, Phase.EXECUTING)

    async def _remediate(self, session: RunSession, checkpoint: Checkpoint) -> int:
        """Reopen the tasks that fell short, carrying the corrections into their brief."""
        state = session.state
        count = 0
        corrections = checkpoint.remediation or checkpoint.gaps
        tasks = list(state.tasks.values())
        for task in tasks:
            if task.status is not TaskStatus.FAILED or task.attempts >= self.config.policy.max_task_attempts:
                continue
            task.status = TaskStatus.APPROVED
            task.assigned_agent_id = None
            mine = corrections_for_task(task, corrections, tasks)
            if mine:
                task.action = (
                    f"{task.action}\n\nCheckpoint corrections from attempt {task.attempts}:\n"
                    + "\n".join(f"- {c}" for c in mine[:6])
                )
            task.updated_at = now_iso()
            await session.aemit(EventType.TASK_UPDATED, {"task": to_jsonable(task)})
            count += 1

        for agent in state.agents.values():
            if agent.kind in (AgentKind.EXECUTION, AgentKind.VERIFICATION):
                if agent.status in ACTIVE_AGENT_STATUSES:
                    await self._set_status(session, agent, AgentStatus.STOPPED)
        return count

    # -- improvement ---------------------------------------------------

    async def _run_improvement(self, session: RunSession) -> SupervisorResponse | None:
        state = session.state
        if not self.config.policy.learn_from_failures:
            return self.reporting._complete(session)

        for lesson in phases.mechanical_lessons(state):
            self._record_lesson(session, lesson)

        stage = "improvement"
        checkpoint = state.checkpoints[-1] if state.checkpoints else None

        already_ran = any(
            a.role == "improver" and a.status is AgentStatus.DONE for a in state.agents.values()
        )
        if already_ran:
            return self.reporting._complete(session)

        if self._delegated(stage):
            agent = await self._stage_agent(session, "improver", stage)
            if agent is None:
                # Same rule as a failed learning pass: never end a run badly over
                # the lessons it did not get to write down.
                await session.anote("improvement stage abandoned; ending with the mechanical lessons")
                return self.reporting._complete(session)
            system, user = phases.lessons_prompt(state, checkpoint)
            packet = self.packets._stage_packet(
                session, agent, system, user, LESSONS_SCHEMA, "improvement"
            )
            return SupervisorResponse(
                run_id=state.id, phase=str(state.phase), action="dispatch",
                message="Improvement stage. Extract reusable lessons from this run, then report.",
                packets=[packet],
            )

        system, user = phases.lessons_prompt(state, checkpoint)
        try:
            data = await self._call(stage, system, user, LESSONS_SCHEMA)
        except Exception as exc:  # noqa: BLE001 - never fail a run over the learning pass
            await session.anote(f"improvement stage skipped: {exc}")
            return self.reporting._complete(session)

        for lesson in parse_lessons(data, state.id, state.workspace):
            self._record_lesson(session, lesson)
        return self.reporting._complete(session)

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
        self._check_resume_fidelity(session)
        self.packets._registry_for(session, None)
        state = session.state

        agent = state.agents.get(agent_id)
        if agent is None:
            # A mistyped id is the caller's error, not the run's. Failing the
            # whole run here discarded every finding it had gathered.
            await session.anote("report for an unknown agent was rejected", agent_id=agent_id)
            return SupervisorResponse(
                run_id=state.id,
                phase=str(state.phase),
                action="await_reports",
                message=(
                    f"No agent {agent_id!r} in this run; nothing was recorded. "
                    f"Known agents: {', '.join(sorted(state.agents)) or '(none)'}."
                ),
                detail={"error": "unknown_agent", "known_agents": sorted(state.agents)},
            )

        # Existing was the only thing asked about it. Not its status, and not
        # whether this turn had already been reported -- so a packet reported
        # twice was recorded twice, counted twice in ``turn_counts``, supervised
        # twice and answered with a second directive, all for one piece of work.
        # A host that retries a call it is unsure landed produces exactly that.
        stale = self._stale_report_reason(state, agent)
        if stale is not None:
            await session.anote("duplicate report rejected", agent_id=agent.id, reason=stale)
            return SupervisorResponse(
                run_id=state.id,
                phase=str(state.phase),
                action="await_reports",
                message=(
                    f"Agent {agent_id!r} {stale}; nothing was recorded. If this was "
                    "a retry, the first report landed and no second one is needed."
                ),
                detail={"error": "duplicate_report", "agent_status": str(agent.status)},
            )

        if agent.kind is AgentKind.SYNTHESIS:
            return await self._report_stage(session, agent, payload)

        if agent.kind is AgentKind.VERIFICATION:
            # Recorded and assessed like any other turn before its verdict is
            # applied. This path used to jump straight to the verdict, so the one
            # agent whose whole job is judgement produced no turn event at all:
            # its reasoning, self-assessment, blocked_on and usage reached
            # nothing, and it was the only agent in a run with no drift score.
            await self._assess_drift(session, agent, await self._record_turn(session, agent, payload))
            return await self._report_verification(session, agent, payload)

        turn = await self._record_turn(session, agent, payload)
        directive = await self._supervise(session, agent, turn)

        # The same second opinion the autonomous loop takes. Skipped only when
        # the drift stage is itself host-routed, since the harness cannot then
        # run it without asking the host for another round trip.
        if not self._delegated("drift") and should_escalate(
            session.state.drift.get(agent.id, DriftAssessment()),
            self.config.policy,
            max(0, turn.seq - 1),
        ):
            try:
                await self.supervise_with_model(session.state.id, agent.id)
            except Exception:  # noqa: BLE001 - a failed second opinion is not fatal
                pass

        if agent.kind is AgentKind.EXECUTION and directive.kind in (
            DirectiveKind.ACCEPT, DirectiveKind.STOP, DirectiveKind.ESCALATE
        ):
            self._mark_task_awaiting_verification(session, agent, payload)

        return self._after_directive(session, agent, directive)

    @staticmethod
    def _stale_report_reason(state: RunState, agent: AgentSpec) -> str | None:
        """Why this agent cannot report again, or ``None`` if it can.

        Two bounds, because they catch different mistakes.

        An agent that is no longer active has finished: ACTIVE_AGENT_STATUSES is
        the harness's own definition of "can still be driven from", and a report
        against anything outside it is late, duplicated or addressed to an agent
        the supervisor has already given up on.

        An agent still active but out of turns is the subtler case: the turn
        contract carries no turn identifier, so two reports of the *same* turn
        are indistinguishable from two genuine turns while the budget allows
        more. The budget is the bound that does not need one -- past it, another
        report cannot be work this agent was asked to do.
        """
        if agent.status not in ACTIVE_AGENT_STATUSES:
            return f"is {agent.status.value} and is not accepting reports"
        used = state.turn_counts.get(agent.id, 0)
        if agent.budget.max_turns and used >= agent.budget.max_turns:
            return (
                f"has already reported {used} of its {agent.budget.max_turns} "
                "permitted turn(s)"
            )
        return None

    async def _record_turn(
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
            open_questions=[
                str(q).strip() for q in (payload.get("open_questions") or []) if str(q).strip()
            ],
            usage=self.reporting._usage_from(payload),
        )

        # Only analysis agents establish facts for the run. An execution agent
        # reports what it changed, which its turn and its findings already
        # carry; a verifier writing into the record it is judging against is the
        # conflict of interest batch 7 was about, in a different costume.
        established = (
            parse_established(payload, agent)
            if agent.kind is AgentKind.ANALYSIS else []
        )

        # One batch, one lock acquisition, one fsync. This was an `emit` per
        # event -- the turn, then every finding on it, then every message it
        # routed -- and each of those takes the log's advisory lock and syncs to
        # disk while the event loop waits. A turn carrying eight findings paid
        # for nine of them, and parallel autonomous agents queue behind each
        # other for every one.
        events: list[tuple[EventType, dict[str, Any], str]] = [
            (EventType.TURN_RECORDED, {"turn": to_jsonable(turn)}, agent.id)
        ]
        events += [
            (EventType.FINDING_ADDED, {"finding": to_jsonable(finding)}, agent.id)
            for finding in turn.findings
        ]
        events += [
            (EventType.FACT_ESTABLISHED, {"fact": to_jsonable(fact)}, agent.id)
            for fact in established
        ]
        events += self._message_events(session, turn)
        await session.aemit_many(events)
        return turn

    def _message_events(
        self, session: RunSession, turn: AgentTurn
    ) -> list[tuple[EventType, dict[str, Any], str]]:
        """The MESSAGE_SENT events this turn routes, ready to batch.

        Routing is a pure decision over run state; the blackboard holds no
        per-run memory any more, so a fresh one is equivalent to a cached one,
        and deciding before emitting rather than while emitting is what lets the
        whole turn go to disk under one lock.
        """
        board = Blackboard(session.state.id)
        out: list[tuple[EventType, dict[str, Any], str]] = []
        for message in turn.messages:
            routing = board.route(message, session.state)
            out.append((
                EventType.MESSAGE_SENT,
                {"message": to_jsonable(routing.message), "deliver_to": routing.deliver_to,
                 "escalated": routing.escalate},
                turn.agent_id,
            ))
        return out

    async def _answer_questions(
        self, session: RunSession, agent: AgentSpec, directive: Directive
    ) -> Directive:
        """Answer this agent's questions to the supervisor, from the run's record.

        ``Blackboard.route`` has always accepted a message addressed to the
        supervisor, flagged it, and stored it -- and nothing ever read it. An
        agent could ask, and the question went nowhere: `supervisor_inbox` had no
        caller and `DirectiveKind.ANSWER` was constructed by no code path, while
        sitting in ``CONTINUATION_DIRECTIVES`` waiting to be. The agent's only
        other options were to guess or to escalate, and escalating ends it.

        What the supervisor answers with is the run's own record --
        :func:`answer_from_record` -- because that is the whole of what it knows.
        A question the record does not cover is said to be uncovered rather than
        guessed at; the agent keeps working and is told to record the uncertainty
        where the checkpoint will see it.

        The answer never displaces a correction. It changes the directive's
        *kind* only when the assessment said CONTINUE, which is the case where a
        pending question is the most useful thing to address; otherwise it rides
        along in the corrections, so a drifting agent is still corrected and
        still gets its answer.
        """
        questions = Blackboard.questions_for_supervisor(agent.id, session.state)
        if not questions:
            return directive

        answered: list[str] = []
        for question in questions[:MAX_QUESTIONS_PER_TURN]:
            found = answer_from_record(question, agent, session.state)
            asked = question.subject or question.content[:120]
            if found:
                answered.append(f"On your question ({asked}): " + " ".join(found))
            else:
                answered.append(
                    f"On your question ({asked}): the run's record does not cover it. "
                    "Proceed on your stated scope and put what you could not "
                    "establish in self_assessment."
                )
                await session.anote(
                    "an agent asked something the run's record could not answer",
                    actor=agent.id,
                    question=asked,
                )

        # Marked answered so the next turn does not answer them again.
        await session.aemit(
            EventType.MESSAGE_DELIVERED,
            {"message_ids": [q.id for q in questions[:MAX_QUESTIONS_PER_TURN]],
             "agent_id": SUPERVISOR},
        )

        directive.corrections = [*answered, *directive.corrections]
        if directive.kind is DirectiveKind.CONTINUE:
            directive.kind = DirectiveKind.ANSWER
            directive.rationale = (
                "Answered from the run's own record. " + (directive.rationale or "")
            ).strip()
        return directive

    async def _assess_drift(
        self, session: RunSession, agent: AgentSpec, turn: AgentTurn
    ) -> DriftAssessment:
        """Score one turn against the brief the agent was given, and record it.

        Separate from :meth:`_supervise` because a verification turn wants this
        half and not the other. A verifier is settled by its own verdict, so
        issuing it a directive would compute an instruction nothing acts on --
        the pattern this codebase keeps finding and removing. It still gets
        assessed: a verifier judging things outside the task it was handed is
        exactly the drift worth catching, and it was the one agent in a run with
        no assessment at all.
        """
        state = session.state
        turns_used = state.turn_counts.get(agent.id, 0)
        ctx = TurnContext(
            agent=agent,
            turn=turn,
            previous_turns=self.packets._previous_turns(session, agent.id, before=turn.id),
            brief=state.briefs.get(agent.id) or agent.brief,
            task_prompt=state.prompt,
            turn_index=max(0, turns_used - 1),
            # The run records the workspace it was created against; this
            # process's own may be a resumed cwd that the agents never saw.
            workspace=str(state.workspace),
        )
        assessment = assess_heuristically(ctx)
        # Which turn was judged. `RunState.drift` is keyed by agent, so it keeps
        # only this agent's newest assessment; the log keeps them all, and the
        # journal needs to know which turn each one was about.
        assessment.turn_id = turn.id
        await session.aemit(
            EventType.DRIFT_ASSESSED,
            {"agent_id": agent.id, "assessment": to_jsonable(assessment)},
        )
        return assessment

    async def _supervise(self, session: RunSession, agent: AgentSpec, turn: AgentTurn) -> Directive:
        """Assess the turn and issue the directive that governs the next one."""
        state = session.state
        turns_used = state.turn_counts.get(agent.id, 0)
        assessment = await self._assess_drift(session, agent, turn)

        inbox = Blackboard.inbox_for(agent.id, state)
        prior_corrections = sum(
            1 for d in state.directives
            if d.agent_id == agent.id
            and d.kind in (DirectiveKind.REFOCUS, DirectiveKind.NARROW, DirectiveKind.REJECT)
        )
        directive = decide_directive(
            assessment, agent, turn, self.config.policy, turns_used,
            inbox=inbox, prior_corrections=prior_corrections,
            # What the agent has spent so far, including the turn just recorded.
            # Without it only the turn ceiling was ever checked.
            usage=state.usage.get(agent.id),
        )
        directive = await self._answer_questions(session, agent, directive)
        # The turn this answers, so "why was this directive issued" is a lookup
        # rather than an inference from the order events were appended in.
        directive.turn_id = turn.id
        await session.aemit(EventType.DIRECTIVE_ISSUED, {"directive": to_jsonable(directive)})
        if inbox:
            await session.aemit(
                EventType.MESSAGE_DELIVERED,
                {"message_ids": [m.id for m in inbox], "agent_id": agent.id},
            )

        # A terminal directive ends the agent on both backends, so the reason it
        # ended belongs on the log rather than only in the response the host is
        # handed.
        status = status_after(directive)
        if status not in ACTIVE_AGENT_STATUSES:
            await session.anote(
                f"agent `{agent.id}` finished ({directive.kind.value}): "
                f"{directive.rationale or 'no rationale given'}",
                actor=agent.id,
            )
        await self._set_status(session, agent, status)
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

        turns = self.packets._previous_turns(session, agent_id)
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
            "# The agent's objectives\n" + "\n".join(f"- {o}" for o in agent.objectives)
            + "\n\n# Explicitly out of scope\n"
            + ("\n".join(f"- {o}" for o in agent.scope.out_of_scope) or "(nothing listed)")
            + f"\n\n# The overall task\n{state.prompt}"
            + f"\n\n# What the agent just reported\n{last.output[:4000]}"
            + "\n\n# Mechanical signals already detected\n"
            + ("\n".join(f"- {s.kind}: {s.detail}" for s in heuristic.signals) or "(none)")
        )
        data = await self._call("drift", system, user, DRIFT_SCHEMA)
        model_view = parse_drift(data, checked_by=self.router.binding("drift").ref())
        merged = merge_assessments(heuristic, model_view)
        # A second opinion on the same turn, not on a new one.
        merged.turn_id = heuristic.turn_id or last.id
        await session.aemit(
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

        packet = self.packets._agent_packet(session, agent, directive=directive)
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

    async def _report_verification(
        self, session: RunSession, agent: AgentSpec, payload: dict[str, Any]
    ) -> SupervisorResponse:
        state = session.state
        task = state.tasks.get(agent.task_id or "")
        if task is None:
            return self.reporting._error(session, f"verification agent {agent.id} has no task")

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

            # A verdict the harness proved by running the real check outranks an
            # agent's account of it. Only a criterion the harness could not
            # settle -- blocked or never checked -- is open to judgement.
            if (
                crit.verified_by == "harness"
                and crit.status in (CriterionStatus.PASS, CriterionStatus.FAIL)
                and status is not crit.status
            ):
                await session.anote(
                    "verification agent contradicted a mechanical result; "
                    "the mechanical result stands",
                    task_id=task.id,
                    criterion_id=crit.id,
                    mechanical=str(crit.status),
                    claimed=str(status),
                    claimed_evidence=evidence[:400],
                    actor=agent.id,
                )
                continue

            await session.aemit(
                EventType.CRITERION_VERIFIED,
                {"task_id": task.id, "criterion_id": crit.id,
                 "status": str(status), "evidence": evidence},
                actor=agent.id,
            )
            applied += 1

        await self._set_status(session, agent, AgentStatus.DONE)
        # This used to re-fetch the task from state, because emitting replaced
        # the object the caller held and the criterion verdicts above landed on
        # the replacement. The fold updates in place now, so `task` is that
        # object; the re-fetch was the only place that compensated, and the fix
        # is in the fold rather than repeated at each call site.
        task.status = TaskStatus.VERIFIED if task.dod_satisfied() else TaskStatus.FAILED
        task.updated_at = now_iso()
        await session.aemit(EventType.TASK_UPDATED, {"task": to_jsonable(task)})
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
        await session.aemit(
            EventType.TURN_RECORDED,
            {"turn": to_jsonable(AgentTurn(
                run_id=state.id, agent_id=agent.id, seq=1,
                reasoning=str(payload.get("reasoning", "")),
                output=str(payload.get("summary") or payload.get("restated_goal") or "")[:4000],
                usage=self.reporting._usage_from(payload),
            ))},
            actor=agent.id,
        )
        await self._set_status(session, agent, AgentStatus.DONE)

        if agent.role == "planner":
            registry = self.packets._registry_for(session, None)
            lenses = phases.plan_lenses(state, self.config)
            fallback = phases.build_analysis_agents(state, self.config, registry, lenses)
            self._apply_plan(session, payload, fallback, registry)
        elif agent.role == "synthesizer":
            self._apply_synthesis(session, payload)
        elif agent.role == "checkpointer":
            iteration = state.checkpoint_iteration + 1
            deterministic = phases.deterministic_checkpoint(state, self.config.policy, iteration)
            await self._apply_checkpoint(session, deterministic,
                                   parse_checkpoint(payload, state.id, iteration))
        elif agent.role == "improver":
            for lesson in parse_lessons(payload, state.id, state.workspace):
                self._record_lesson(session, lesson)

        return await self._advance(session)

    # ------------------------------------------------------------------
    # Approval
    # ------------------------------------------------------------------

    async def approve(
        self,
        run_id: str,
        decisions: list[dict[str, Any]] | list[TaskDecision],
        *,
        renew_envelope: bool = False,
    ) -> SupervisorResponse:
        """Apply the user's decisions on proposed tasks.

        ``renew_envelope`` re-grants the run's scope envelope, which a resume
        past ``policy.envelope_max_age_days`` requires before it will spawn an
        execution agent. It renews the grant's *date*, never its extent: the
        paths are carried over unchanged, and widening them is the thing this
        method has never been allowed to do.
        """
        session = self.store.open(run_id)
        self._check_resume_fidelity(session)
        self.packets._registry_for(session, None)
        state = session.state

        if renew_envelope:
            # Synchronous emission: `approve` is a phase boundary with nothing
            # in flight, which is exactly where the docstring on `RunSession`
            # says the blocking form is the right one.
            self._set_envelope(
                session, effective(state.envelope), source="renewed on resume"
            )

        applied = 0
        for raw in decisions:
            decision = raw if isinstance(raw, TaskDecision) else _coerce_decision(raw)
            task = state.tasks.get(decision.task_id)
            if task is None:
                continue
            for text in _apply_modifications(
                task, decision.modifications, self.config.policy, self.workspace,
                envelope=effective(state.envelope),
            ):
                await session.anote(text, task_id=task.id)
            task.decision = decision.decision
            task.decision_note = decision.note
            task.status = {
                Decision.APPROVE: TaskStatus.APPROVED,
                Decision.MODIFY: TaskStatus.APPROVED,
                Decision.REJECT: TaskStatus.REJECTED,
                Decision.DEFER: TaskStatus.DEFERRED,
            }[decision.decision]
            task.updated_at = now_iso()
            await session.aemit(EventType.TASK_DECIDED, {"task": to_jsonable(task)})
            applied += 1

        approved = [t for t in state.tasks.values() if t.status is TaskStatus.APPROVED]
        if not approved:
            await session.anote("no tasks approved; ending with the analysis report")
            self.reporting._write_run_artifacts(session)
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

        results = await asyncio.gather(
            *(drive(a) for a in agents), return_exceptions=True
        )

        # Exceptions were previously gathered and discarded. An agent that blew
        # up stayed active, so the phase never settled and the run failed with a
        # generic "did not settle" and an event log that said nothing about why.
        for agent, result in zip(agents, results, strict=False):
            if not isinstance(result, BaseException):
                continue
            await session.anote(
                f"agent raised {type(result).__name__}: {result}",
                actor=agent.id,
                traceback="".join(
                    traceback.format_exception(type(result), result, result.__traceback__)
                )[-2000:],
            )
            await self._set_status(session, agent, AgentStatus.FAILED)

    async def _drive_agent(self, session: RunSession, agent: AgentSpec) -> None:
        """Run one agent to completion against its bound model.

        Two nested loops. The inner one services tool calls, which do not consume
        the agent's turn budget -- reading three files to answer a question is one
        piece of work, not three. The outer one is the supervised loop: each real
        answer is recorded, assessed for drift, and answered with a directive.
        """
        packet = self.packets._agent_packet(session, agent)
        history: list[ChatMessage] = [ChatMessage("user", packet.brief)]

        for _ in range(agent.budget.max_turns):
            payload: dict[str, Any] | None = None
            raw_text = ""
            # Tool results accumulate across the rounds of one turn and are
            # dropped at the end of it, where the directive replaces them. This
            # used to be reassigned to three messages on every round, so the
            # agent saw only the results of its most recent call: an agent that
            # read one file, then a second, could not compare them, and answering
            # a question that needed both meant reading the first one again --
            # from a tool budget the re-reading was spending.
            turn_history = list(history)

            for tool_round in range(MAX_TOOL_ROUNDS + 1):
                # The last pass through is the answering round, not another
                # tool-serving one. The nudge below used to be appended here and
                # then `continue`d on the final iteration -- which ended the
                # loop, so it was composed, never sent, and the tool-call payload
                # it was meant to replace fell through to `_record_turn` as the
                # agent's answer for the turn.
                final_round = tool_round == MAX_TOOL_ROUNDS
                if final_round:
                    turn_history.append(ChatMessage(
                        "user",
                        f"You have used all {MAX_TOOL_ROUNDS} tool rounds for this turn. "
                        "Answer now with what you have, and say plainly what you could "
                        "not establish.",
                    ))

                try:
                    response = await self.router.complete(
                        self._stage_for(agent),
                        CompletionRequest(
                            messages=turn_history,
                            system="You are a supervised agent. Answer only with the JSON "
                                   "object your brief specifies.",
                            json_schema=packet.schema,
                            max_tokens=4096,
                        ),
                        binding=agent.binding,
                    )
                except Exception as exc:  # noqa: BLE001 - one agent must not kill the run
                    await session.anote(f"agent failed: {exc}", actor=agent.id)
                    await self._set_status(session, agent, AgentStatus.FAILED)
                    return

                payload = response.json(required=False)
                raw_text = response.text
                payload.setdefault("usage", to_jsonable(response.usage))
                if response.reasoning and not payload.get("reasoning"):
                    payload["reasoning"] = response.reasoning

                calls = parse_tool_calls(payload)
                if not calls:
                    break
                if final_round:
                    # It was asked plainly for an answer and asked for tools
                    # instead. Whatever else the payload carries is the best
                    # account of the turn there is going to be, so it is recorded
                    # -- but not silently, which is how this looked before.
                    await session.anote(
                        "tool budget spent; the agent asked for tools again after "
                        "being told to answer, and its answer is recorded as it stands",
                        actor=agent.id,
                        tools=[name for name, _ in calls],
                    )
                    break

                results = [self.toolbox.call(name, args, agent) for name, args in calls]
                await session.anote(
                    "tools called",
                    actor=agent.id,
                    tools=[name for name, _ in calls],
                    failures=[r.tool for r in results if not r.ok],
                )
                turn_history.append(ChatMessage("assistant", raw_text[:TOOL_ECHO_CHARS]))
                turn_history.append(
                    ChatMessage("user", render_results(results)[:TOOL_RESULT_CHARS])
                )

            if payload is None:
                await self._set_status(session, agent, AgentStatus.FAILED)
                return

            if agent.kind is AgentKind.VERIFICATION:
                # Same as the host path: the turn is recorded and assessed
                # before the verdict is applied, so a verifier's work is visible
                # on both backends rather than on neither.
                await self._assess_drift(session, agent, await self._record_turn(session, agent, payload))
                await self._report_verification(session, agent, payload)
                return

            turn = await self._record_turn(session, agent, payload)
            directive = await self._supervise(session, agent, turn)

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

        # Falling out of the loop means the supervised turns ran out without a
        # terminal directive: every turn drew a continuation, or the budget
        # allowed none. Without a terminal status the agent stayed in
        # ACTIVE_AGENT_STATUSES, so the phase drove it again from turn one until
        # the run was failed for not settling.
        turns_used = session.state.turn_counts.get(agent.id, 0)
        await session.anote(
            f"agent `{agent.id}` stopped: turn budget exhausted "
            f"({turns_used}/{agent.budget.max_turns}) without a terminal directive",
            actor=agent.id,
        )
        await self._set_status(session, agent, AgentStatus.STOPPED)

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

        A stage routed to the host is rejected before the run is created: there
        is no host here to execute the packet it would produce.
        """
        delegated = self._host_routed_stages()
        if delegated:
            raise ValueError(
                "an autonomous run needs every stage routed to a model provider; "
                f"routed to the host: {', '.join(delegated)}"
            )

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
            if response.action in ("dispatch", "await_reports"):
                # Nothing here runs a host packet. Advancing again would re-enter
                # the same phase, emit the same agents and briefs, and never
                # terminate -- so the run ends here, naming the phase and cause.
                delegated = self._host_routed_stages()
                session = self.store.open(response.run_id)
                return self.reporting._error(
                    session,
                    f"autonomous run cannot consume a {response.action!r} response in "
                    f"phase {response.phase}; "
                    + (f"host-routed stage(s): {', '.join(delegated)}" if delegated
                       else f"{len(response.packets)} packet(s) need a host"),
                )
            response = await self.advance(response.run_id)
        return response

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _delegated(self, stage: str) -> bool:
        return self.router.is_host(stage)

    def _host_routed_stages(self) -> list[str]:
        """Every configured stage that resolves to the host provider."""
        return sorted(
            stage for stage in set(self.config.routing) | set(KNOWN_STAGES)
            if self._delegated(stage)
        )

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


    def _check_resume_fidelity(self, session: RunSession) -> None:
        """Note when this process is not the one the run was started under.

        A run records the host it was created against and the workspace it was
        rooted in, and neither was ever compared with what the resuming process
        actually has. Everything that judges an agent -- the drift thresholds,
        the quality bars, which model answers a stage -- comes from *this*
        process's configuration, so a run resumed under a different setup is
        supervised by rules its earlier turns were never held to, and nothing
        anywhere said so.

        This does not make a resume faithful; making it faithful means recording
        the resolved configuration on the log and replaying against it, which is
        a larger change than a note. What it removes is the silence: the
        divergence is on the log and in ``status``, so a run whose second half
        was judged differently from its first says which.

        Recorded once per run. A resume that keeps being resumed should not fill
        the log with the same sentence.
        """
        state = session.state
        divergences: list[str] = []
        if state.host and state.host != self.host.name:
            divergences.append(f"host {state.host!r} -> {self.host.name!r}")
        if state.workspace and str(self.workspace) != str(state.workspace):
            divergences.append(f"workspace {state.workspace!r} -> {str(self.workspace)!r}")
        if not divergences:
            return

        already = f"resumed under a different environment: {'; '.join(divergences)}"
        if any(n.text == already for n in state.notes):
            return
        session.note(already)

    def _configured_envelope(self) -> ScopeEnvelope:
        """The envelope the user's configuration grants, before any model speaks."""
        return ScopeEnvelope(
            paths=list(self.config.policy.scope_envelope),
            forbidden_paths=list(self.config.policy.scope_envelope_forbidden),
            source="configuration",
        )

    def _set_envelope(
        self, session: RunSession, envelope: ScopeEnvelope, *, source: str
    ) -> None:
        # Stamped here, not by the caller: emitting ENVELOPE_SET *is* making the
        # grant, so the date and the provenance are set in the same place. The
        # first draft carried `granted_at` over from the envelope handed in,
        # which meant renewing an aged grant renewed everything about it except
        # its age.
        envelope = replace(envelope, source=source, granted_at=now_iso())
        session.emit(EventType.ENVELOPE_SET, {"envelope": to_jsonable(envelope)})
        session.note(
            f"run envelope ({source}): may modify {render(envelope.paths)}"
            + (
                f"; never {render(envelope.forbidden_paths)}"
                if envelope.forbidden_paths else ""
            )
        )

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















    async def aclose(self) -> None:
        await self.router.aclose()
        if self._owns_store:
            self.store.close()


# --------------------------------------------------------------------------
# Decision helpers
# --------------------------------------------------------------------------


def _seconds_since(ts: str) -> float | None:
    """Seconds elapsed since an event timestamp, or ``None`` if it is unusable."""
    if not ts:
        return None
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


def corrections_for_task(
    task: ExecutionTask, corrections: list[str], tasks: list[ExecutionTask]
) -> list[str]:
    """The checkpoint corrections this task should be briefed with.

    A correction is an instruction, not a note, so handing every reopened task
    the whole list tells each agent to redo the others' work as well. In a real
    run two remediated tasks were each briefed with the other's corrections
    plus two concerning a third task, and a human had to filter them by hand.

    A correction belongs to the task it names -- by id, as the mechanical gaps
    and the checkpoint prompt both write it, or failing that by title. One that
    names no task at all is addressed to the run rather than to a task, so
    every reopened task gets it.
    """
    mine: list[str] = []
    for correction in corrections:
        named = _tasks_named_by(correction, tasks)
        if not named or task.id in named:
            mine.append(correction)
    return mine


def _tasks_named_by(correction: str, tasks: list[ExecutionTask]) -> set[str]:
    """The ids of the tasks a correction is about; empty if it names none."""
    text = correction.lower()
    by_id = {t.id for t in tasks if t.id.lower() in text}
    if by_id:
        # An id is unambiguous, and a title can appear inside another task's
        # title ("add a limiter" inside "add a limiter test"), so once any task
        # is named by id the titles are not consulted.
        return by_id
    return {t.id for t in tasks if t.title.strip() and t.title.strip().lower() in text}


def _coerce_decision(raw: dict[str, Any]) -> TaskDecision:
    from ..contracts import parse_decision

    return TaskDecision(
        task_id=str(raw.get("task_id", "")),
        decision=parse_decision(raw.get("decision")),
        note=str(raw.get("note", "")),
        modifications=dict(raw.get("modifications") or {}),
    )


_MODIFIABLE = {"title", "action", "motivation", "effort"}


def _apply_modifications(
    task: ExecutionTask,
    modifications: dict[str, Any],
    policy: Policy,
    workspace: Path | None = None,
    envelope: ScopeEnvelope | None = None,
) -> list[str]:
    """Apply the user's edits to an approved task.

    Only descriptive fields can be edited freely. Criteria may be replaced
    wholesale but never silently dropped, because weakening a definition of done
    after the fact is exactly how verification stops meaning anything. A
    replacement therefore passes the same gate a proposed definition of done
    does -- the mandatory bars policy requires are re-applied and weak criteria
    are reported -- and every criterion the edit removed is named.

    A ``scope_paths`` edit is clamped to the run's envelope, and the clamp is
    reported. Approving a task is a decision about that task; if it could move
    a run-level bound, the bound would only ever be as strong as the most
    permissive task anyone approved. The full argument, and the alternative it
    was chosen over, is in :mod:`supervisor_harness.core.envelope`.

    Returns the notes the user should see, for the caller to record on the run.
    """
    notes: list[str] = []
    for key, value in modifications.items():
        if key in _MODIFIABLE and isinstance(value, str):
            setattr(task, key, value)
        elif key == "dod" and isinstance(value, list):
            from ..contracts import parse_dod

            replacement = parse_dod(value)
            if replacement:
                dropped = _dropped_criteria(task.dod, replacement)
                task.dod = replacement
                notes.extend(
                    f"modification dropped {'mandatory ' if c.mandatory else ''}"
                    f"criterion: {c.statement}"
                    for c in dropped
                )
                _, bars = phases.prepare_tasks([task], policy, workspace)
                notes.extend(bars.get(task.id, []))
        elif key == "scope_paths" and isinstance(value, list):
            task.scope, clamped = attenuate(
                replace(task.scope, paths=[str(v) for v in value]),
                [Ceiling.of("run envelope", envelope)],
            )
            notes.extend(clamped)
    return notes


def _dropped_criteria(
    before: list[DoDCriterion], after: list[DoDCriterion]
) -> list[DoDCriterion]:
    """Criteria the replacement does not restate. Ids are regenerated by
    ``parse_dod``, so the statement is what identifies a criterion here."""
    kept = {_criterion_key(c) for c in after}
    return [c for c in before if _criterion_key(c) not in kept]


def _criterion_key(crit: DoDCriterion) -> str:
    return " ".join(crit.statement.lower().split())
