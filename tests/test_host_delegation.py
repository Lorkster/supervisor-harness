"""The host-delegated path: the harness plans and supervises, the host executes.

This mirrors what Claude Code or Cursor actually does -- receive packets, run
them with its own tools, report each turn back -- so it exercises the default
backend without a model provider being involved at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from supervisor_harness.config import HarnessConfig, Policy, default_config
from supervisor_harness.core.supervisor import Supervisor, SupervisorResponse
from supervisor_harness.host.detect import HostInfo
from supervisor_harness.models import (
    AgentKind,
    AgentStatus,
    Backend,
    Phase,
    RunMode,
    TaskStatus,
)
from supervisor_harness.store.events import EventType
from supervisor_harness.store.runstore import RunStore

from .conftest import FakeProvider

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"


@pytest.fixture
def host_config() -> HarnessConfig:
    cfg = default_config()
    cfg.backend = Backend.HOST
    cfg.routing = {k: "host" for k in cfg.routing}   # every stage delegated
    cfg.policy = Policy(default_max_turns=3, execution_max_turns=3, max_analysis_lenses=3)
    return cfg


@pytest.fixture
def host_supervisor(workspace: Path, host_config: HarnessConfig) -> Supervisor:
    store = RunStore(workspace / ".supervisor")
    host = HostInfo(name="claude-code", workspace=str(workspace), confidence=1.0)
    return Supervisor(workspace=workspace, config=host_config, store=store, host=host)


class HostSimulator:
    """Stands in for Claude Code: runs packets and reports them back."""

    def __init__(self, supervisor: Supervisor, answers: FakeProvider) -> None:
        self.supervisor = supervisor
        self.answers = answers
        self.dispatched: list[str] = []

    def _answer_for(self, packet) -> dict:
        """Produce the payload a host agent would return for this packet."""
        stage = {
            "planning": "planning",
            "synthesis": "synthesis",
            "checkpoint": "checkpoint",
            "improvement": "improvement",
            "analysis": "analysis",
            "execution": "execution",
            "verification": "verification",
        }[packet.kind]

        # Reuse the fake provider's canned answers, which key off the brief
        # text, and its script/override precedence, so a scripted stage behaves
        # the same on this path as it does under the autonomous backend.
        from supervisor_harness.providers.base import ChatMessage, CompletionRequest

        request = CompletionRequest(
            messages=[ChatMessage("user", packet.brief)],
            system=packet.brief[:200],
            json_schema=packet.schema,
        )
        return self.answers.answer_for(stage, request)

    async def drive(self, response: SupervisorResponse, *, approve: bool = True) -> SupervisorResponse:
        """Run the loop the host-side skill is documented to run."""
        for _ in range(60):
            if response.action == "complete" or response.action == "failed":
                return response

            if response.action == "await_approval":
                if not approve:
                    return response
                decisions = [{"task_id": t["id"], "decision": "approve"} for t in response.tasks]
                response = await self.supervisor.approve(response.run_id, decisions)
                continue

            if response.action == "dispatch":
                packets = list(response.packets)
                last = response
                for packet in packets:
                    self.dispatched.append(packet.kind)
                    last = await self.supervisor.report(
                        packet.run_id, packet.agent_id, self._answer_for(packet)
                    )
                # After reporting every packet, ask what happens next.
                response = (
                    last if last.action in ("complete", "failed", "await_approval", "dispatch")
                    else await self.supervisor.advance(response.run_id)
                )
                if response.action == "await_reports":
                    response = await self.supervisor.advance(response.run_id)
                continue

            response = await self.supervisor.advance(response.run_id)
        raise AssertionError("host loop did not terminate")


async def test_host_delegated_run_completes(host_supervisor: Supervisor, fake: FakeProvider) -> None:
    """The full lifecycle works with the host executing every stage."""
    simulator = HostSimulator(host_supervisor, fake)
    start = await host_supervisor.start(PROMPT, mode=RunMode.EXECUTE)

    assert start.action == "dispatch"
    assert start.packets[0].kind == "planning"

    final = await simulator.drive(start)

    assert final.action == "complete", final.message
    state = host_supervisor.store.load_state(final.run_id)
    assert state.phase is Phase.COMPLETE

    task = next(iter(state.tasks.values()))
    assert task.status is TaskStatus.VERIFIED
    assert task.dod_satisfied()

    # The host was asked to run every kind of stage.
    assert {"planning", "analysis", "synthesis", "execution", "verification"} <= set(
        simulator.dispatched
    )


async def test_packets_carry_everything_the_host_needs(host_supervisor: Supervisor) -> None:
    """A packet is self-contained: brief, schema, budget and agent identity."""
    start = await host_supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    packet = start.packets[0]

    assert packet.run_id and packet.agent_id
    assert packet.brief.strip()
    assert packet.schema.get("type") == "object"
    assert packet.turns_remaining >= 1
    # Serialisable, because it crosses the MCP boundary as JSON.
    assert json.loads(json.dumps(packet.to_dict()))


async def test_analysis_packets_are_parallel_and_name_their_peers(
    host_supervisor: Supervisor, fake: FakeProvider
) -> None:
    """Analysis dispatches several agents at once, each aware of the others."""
    simulator = HostSimulator(host_supervisor, fake)
    response = await host_supervisor.start(PROMPT, mode=RunMode.EXECUTE)

    # Run the planning packet only.
    packet = response.packets[0]
    simulator.dispatched.append(packet.kind)
    await host_supervisor.report(packet.run_id, packet.agent_id, simulator._answer_for(packet))
    response = await host_supervisor.advance(response.run_id)

    assert response.action == "dispatch"
    assert len(response.packets) >= 2, "analysis should fan out"
    assert "in parallel" in response.message

    briefs = [p.brief for p in response.packets]
    assert any("Other agents" in b for b in briefs), "agents must know their peers"
    assert all("Output contract" in b for b in briefs)


async def test_host_agents_are_matched_to_roles(workspace: Path, host_config: HarnessConfig) -> None:
    """Roles bind to the host's own subagent types when it declares them."""
    store = RunStore(workspace / ".supervisor")
    host = HostInfo(name="claude-code", workspace=str(workspace), confidence=1.0)
    supervisor = Supervisor(workspace=workspace, config=host_config, store=store, host=host)

    declared = [
        {"name": "Explore", "description": "Read-only search agent"},
        {"name": "Plan", "description": "Software architect agent for implementation plans"},
        {"name": "general-purpose", "description": "General agent"},
    ]
    response = await supervisor.start(PROMPT, mode=RunMode.EXECUTE, host_agents=declared)
    packet = response.packets[0]
    await supervisor.report(packet.run_id, packet.agent_id, {
        "restated_goal": "rate limit login", "mode": "execute",
        "lenses": [
            {"role": "architecture", "why": "structure", "objectives": ["Map the request path"]},
            {"role": "security", "why": "exposure", "objectives": ["Find the attack path"]},
        ],
    })
    response = await supervisor.advance(response.run_id)

    by_role = {p.title: p.host_agent_type for p in response.packets}
    assert by_role.get("Architecture") == "Plan", by_role
    assert by_role.get("Security") == "general-purpose", by_role


BLOCKED_EXECUTION = {
    "output": (
        "Added the per-IP counter to the login handler in src/auth/login.py, but the "
        "Redis client in src/cache.py is synchronous and the handler is async, so the "
        "account-keyed half of the limiter cannot be finished without a decision on "
        "which client to use."
    ),
    "files_touched": ["src/auth/login.py"],
    "commands_run": [],
    "criteria_progress": [],
    "status": "blocked",
    "blocked_on": "Whether to add an async Redis client or make the login handler sync.",
}


async def test_escalate_settles_the_agent_the_same_way_on_both_backends(
    workspace: Path, host_config: HarnessConfig, config: HarnessConfig, fake: FakeProvider
) -> None:
    """An escalating execution agent ends identically whoever ran it.

    ``status_after`` maps ESCALATE to BLOCKED on both paths, so the agent has to
    leave the active set and its task has to reach verification either way --
    otherwise the same directive finishes one backend and loops the other.
    """
    from supervisor_harness.models import ACTIVE_AGENT_STATUSES, AgentKind, AgentStatus
    from supervisor_harness.providers.router import ModelRouter

    fake.overrides["execution"] = dict(BLOCKED_EXECUTION)
    store = RunStore(workspace / ".supervisor")
    host = HostInfo(name="claude-code", workspace=str(workspace), confidence=1.0)

    host_supervisor = Supervisor(workspace=workspace, config=host_config, store=store, host=host)
    host_final = await HostSimulator(host_supervisor, fake).drive(
        await host_supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    )

    router = ModelRouter(config, host_name=host.name)
    router.register("fake", fake)
    autonomous = Supervisor(workspace=workspace, config=config, store=store,
                            host=host, router=router)
    auto_final = await autonomous.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)

    def settled(supervisor: Supervisor, run_id: str) -> tuple[AgentStatus, TaskStatus]:
        state = supervisor.store.load_state(run_id)
        agent = next(a for a in state.agents.values() if a.kind is AgentKind.EXECUTION)
        assert agent.status not in ACTIVE_AGENT_STATUSES, "an escalated agent is not still running"
        return agent.status, state.tasks[agent.task_id].status

    assert host_final.action != "failed", host_final.message
    assert auto_final.action != "failed", auto_final.message
    assert settled(host_supervisor, host_final.run_id) == settled(autonomous, auto_final.run_id)
    assert settled(host_supervisor, host_final.run_id)[0] is AgentStatus.BLOCKED


# -- the phase machine must not issue the same work twice --------------------


async def test_advancing_before_the_planner_reports_does_not_double_the_fleet(
    host_supervisor: Supervisor, fake: FakeProvider
) -> None:
    """An ordinary call sequence used to buy two full analysis fleets.

    The phase moved to ANALYZING before the planner had answered, so an
    ``advance`` while its packet was still out found the run in ANALYZING with no
    analysts and spawned the derived fallback fleet. The planner's report then
    called ``_apply_plan``, which spawned the model's fleet on top -- and the
    guard there tests the phase, which was already ANALYZING, so nothing noticed.
    """
    simulator = HostSimulator(host_supervisor, fake)
    started = await host_supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    planner = started.packets[0]
    assert planner.kind == "planning"

    # The host asks what to do next before it has run the planner.
    impatient = await host_supervisor.advance(started.run_id)
    assert impatient.action == "dispatch"
    assert [p.kind for p in impatient.packets] == ["planning"], (
        "an advance during planning must re-offer planning, not open analysis"
    )

    await host_supervisor.report(
        planner.run_id, planner.agent_id, simulator._answer_for(planner)
    )
    analysis = await host_supervisor.advance(started.run_id)

    state = host_supervisor.store.load_state(started.run_id)
    analysts = [a for a in state.agents.values() if a.kind is AgentKind.ANALYSIS]
    assert len(analysts) == len(analysis.packets)
    assert len(analysts) <= host_supervisor.config.policy.max_analysis_lenses, (
        f"{len(analysts)} analysis agents spawned for one run"
    )


async def test_a_packet_reported_twice_is_rejected_by_name(
    host_supervisor: Supervisor, fake: FakeProvider
) -> None:
    """A host retrying a call it is unsure landed must not buy a second turn.

    ``report`` asked only whether the agent existed -- not its status, not
    whether this turn had already been reported -- so one piece of work was
    recorded twice, counted twice in ``turn_counts``, supervised twice and
    answered with a second directive.
    """
    simulator = HostSimulator(host_supervisor, fake)
    started = await host_supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    planner = started.packets[0]
    answer = simulator._answer_for(planner)

    first = await host_supervisor.report(planner.run_id, planner.agent_id, answer)
    assert first.action != "await_reports" or first.detail.get("error") != "duplicate_report"

    before = host_supervisor.store.load_state(started.run_id)
    second = await host_supervisor.report(planner.run_id, planner.agent_id, answer)
    after = host_supervisor.store.load_state(started.run_id)

    assert second.detail.get("error") == "duplicate_report"
    assert planner.agent_id in second.message
    assert after.turn_counts[planner.agent_id] == before.turn_counts[planner.agent_id]
    assert len(after.agents) == len(before.agents), "the retry spawned something"


async def test_advancing_past_a_finished_synthesis_spawns_nothing(
    host_supervisor: Supervisor, fake: FakeProvider
) -> None:
    """The DONE guard was unreachable, so each advance spawned a synthesizer.

    ``_stage_agent`` only ever returns an agent in ACTIVE_AGENT_STATUSES, so the
    check for a finished one could not hold on anything it handed back -- and a
    synthesizer that had answered fell past it to a fresh spawn, with its own
    AGENT_SPAWNED event and its own dispatched packet, every time. Nothing about
    the run changed to stop the next one.

    Reaching that state means a run sitting in SYNTHESIZING whose synthesizer is
    already DONE, which ``_report_stage`` produces whenever ``_apply_synthesis``
    raises after the status has been set -- it is not wrapped, and the phase
    transition is the last thing it does. The log is the source of truth, so the
    state is reconstructed here by writing the phase back, which is what such a
    crash leaves behind.
    """
    simulator = HostSimulator(host_supervisor, fake)
    response = await host_supervisor.start(PROMPT, mode=RunMode.EXECUTE)

    for _ in range(30):
        if response.action == "dispatch" and response.packets[0].kind == "synthesis":
            break
        if response.action == "dispatch":
            last = response
            for packet in response.packets:
                last = await host_supervisor.report(
                    packet.run_id, packet.agent_id, simulator._answer_for(packet)
                )
            response = (
                last if last.action == "dispatch"
                else await host_supervisor.advance(response.run_id)
            )
            continue
        response = await host_supervisor.advance(response.run_id)
    else:
        raise AssertionError("never reached synthesis")

    synth = response.packets[0]
    await host_supervisor.report(synth.run_id, synth.agent_id, simulator._answer_for(synth))

    session = host_supervisor.store.open(synth.run_id)
    assert session.state.agents[synth.agent_id].status is AgentStatus.DONE
    session.emit(EventType.PHASE_CHANGED, {"phase": str(Phase.SYNTHESIZING)})

    baseline = len(session.state.agents)
    for _ in range(5):
        await host_supervisor.advance(synth.run_id)
    after = host_supervisor.store.load_state(synth.run_id)

    assert len(after.agents) == baseline, (
        f"{len(after.agents) - baseline} pseudo-agent(s) spawned by five advances"
    )
    assert len([a for a in after.agents.values() if a.role == "synthesizer"]) == 1
