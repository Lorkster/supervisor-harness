"""Recording a turn, judging it, and answering it with a directive.

The fourth layer out of `core/supervisor.py`, and the one that took the most
untangling. Before the earlier extractions it made twelve calls outside itself;
afterwards only two remained -- `_call` and `_set_status` -- and both were small
enough to have a home rather than a dependency. `_call` is here, because this
layer owns the router and the phase machine borrows it. `_set_status` went to
`Lifecycle`, which is the one collaborator this layer keeps.

What is *not* here is `_report_stage` and `_report_verification`. Those settle a
task and drive the phase machine (`_advance`, `_apply_plan`, `_apply_synthesis`),
so they belong to it rather than to this. That boundary is the reason this layer
could come out at all: supervising a turn and deciding what the run does next
turned out to be two things, not one.

The bodies are the ones that were on ``Supervisor``, moved verbatim.
"""

from __future__ import annotations

from typing import Any

from ..config import HarnessConfig
from ..contracts import (
    DRIFT_SCHEMA,
    parse_drift,
    parse_established,
    parse_findings,
    parse_messages,
    parse_status,
)
from ..models import (
    ACTIVE_AGENT_STATUSES,
    SUPERVISOR,
    AgentKind,
    AgentSpec,
    AgentTurn,
    Directive,
    DirectiveKind,
    DriftAssessment,
)
from ..providers.base import ChatMessage, CompletionRequest
from ..providers.router import ModelRouter
from ..serde import to_jsonable
from ..store.events import EventType
from ..store.runstore import RunSession, RunStore
from .blackboard import Blackboard, answer_from_record
from .drift import (
    TurnContext,
    assess_heuristically,
    decide_directive,
    merge_assessments,
    status_after,
)
from .lifecycle import Lifecycle
from .packets import Packets
from .reporting import Reporting
from .responses import SupervisorResponse

#: How many of an agent's questions the supervisor answers in one directive.
#: A directive an agent cannot read is not an answer, and an agent asking more
#: than this per turn has a briefing problem rather than a question.
MAX_QUESTIONS_PER_TURN = 3


class Supervision:
    """Turns an agent's reported work into an assessment and a directive."""

    def __init__(
        self,
        config: HarnessConfig,
        store: RunStore,
        router: ModelRouter,
        packets: Packets,
        reporting: Reporting,
        lifecycle: Lifecycle,
    ) -> None:
        self.config = config
        self.store = store
        self.router = router
        self.packets = packets
        self.reporting = reporting
        self.lifecycle = lifecycle

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
                str(f)
                for f in (payload.get("files_touched") or payload.get("files_examined") or [])
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
        await self.lifecycle._set_status(session, agent, status)
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
