"""A host agent that is dispatched and never reports.

Finding ``fnd_01M13MPPWM0Y82``. Every ``AgentStatus.FAILED`` transition lived in
``_drive_agent``, which only the autonomous backend walks. A host subagent that
crashed or was cancelled therefore stayed in ``ACTIVE_AGENT_STATUSES`` forever:
its packet was re-emitted on every ``advance`` with nothing distinguishing
"still working" from "gone", and a human had to stand in for the missing
mechanism by hand.

Two ways out, both exercised here: the host says so with ``abandon``, or the
supervisor notices on its own once the agent has been handed the same packet
more times than policy allows. Either way the transition is terminal, it is on
the log by name and cause, and the phase settles.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from supervisor_harness.config import HarnessConfig, Policy, default_config
from supervisor_harness.core.supervisor import Supervisor, SupervisorResponse
from supervisor_harness.host.detect import HostInfo
from supervisor_harness.models import (
    ACTIVE_AGENT_STATUSES,
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
from .test_host_delegation import PROMPT, HostSimulator


@pytest.fixture
def host_config() -> HarnessConfig:
    cfg = default_config()
    cfg.backend = Backend.HOST
    cfg.routing = {k: "host" for k in cfg.routing}
    cfg.policy = Policy(
        default_max_turns=3,
        execution_max_turns=3,
        max_analysis_lenses=3,
        max_unreported_dispatches=2,
    )
    return cfg


@pytest.fixture
def host_supervisor(workspace: Path, host_config: HarnessConfig) -> Supervisor:
    store = RunStore(workspace / ".supervisor")
    host = HostInfo(name="claude-code", workspace=str(workspace), confidence=1.0)
    return Supervisor(workspace=workspace, config=host_config, store=store, host=host)


async def _reach_analysis(
    supervisor: Supervisor, fake: FakeProvider
) -> SupervisorResponse:
    """Drive a host run through planning, up to the analysis fan-out."""
    simulator = HostSimulator(supervisor, fake)
    response = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    packet = response.packets[0]
    await supervisor.report(packet.run_id, packet.agent_id, simulator._answer_for(packet))
    response = await supervisor.advance(response.run_id)
    assert response.action == "dispatch"
    assert len(response.packets) >= 2, "analysis should fan out"
    return response


async def _drive_until(
    supervisor: Supervisor, fake: FakeProvider, response: SupervisorResponse, kind: str
) -> SupervisorResponse:
    """Run the host loop honestly until a packet of `kind` is dispatched."""
    simulator = HostSimulator(supervisor, fake)
    run_id = response.run_id
    for _ in range(30):
        if response.action == "await_approval":
            response = await supervisor.approve(
                run_id,
                [{"task_id": t["id"], "decision": "approve"} for t in response.tasks],
            )
            continue
        if response.action == "dispatch":
            if response.packets[0].kind == kind:
                return response
            last = response
            for packet in response.packets:
                last = await supervisor.report(
                    packet.run_id, packet.agent_id, simulator._answer_for(packet)
                )
            response = (
                last if last.action in ("await_approval", "dispatch")
                else await supervisor.advance(run_id)
            )
            if response.action == "await_reports":
                response = await supervisor.advance(run_id)
            continue
        if response.action in ("complete", "failed"):
            break
        response = await supervisor.advance(run_id)
    pytest.fail(f"the run never dispatched a {kind} packet")


def _notes(supervisor: Supervisor, run_id: str) -> list[str]:
    session = supervisor.store.open(run_id)
    return [
        e.payload.get("text", "")
        for e in session.events()
        if e.type is EventType.NOTE
    ]


async def test_an_analysis_agent_that_never_reports_does_not_hold_the_phase(
    host_supervisor: Supervisor, fake: FakeProvider
) -> None:
    """The bound settles the phase without anyone reporting the missing agent."""
    simulator = HostSimulator(host_supervisor, fake)
    response = await _reach_analysis(host_supervisor, fake)
    run_id = response.run_id

    silent = response.packets[0].agent_id
    for packet in response.packets[1:]:
        await host_supervisor.report(
            packet.run_id, packet.agent_id, simulator._answer_for(packet)
        )

    # The host keeps asking what to do next; nothing ever reports `silent`.
    for _ in range(10):
        response = await host_supervisor.advance(run_id)
        state = host_supervisor.store.load_state(run_id)
        if state.phase is not Phase.ANALYZING:
            break
    else:
        pytest.fail("analysis never settled: the silent agent held the phase open")

    state = host_supervisor.store.load_state(run_id)
    assert state.agents[silent].status is AgentStatus.FAILED
    assert not [p for p in response.packets if p.agent_id == silent], (
        "an abandoned agent must not be dispatched again"
    )

    abandoned = [n for n in _notes(host_supervisor, run_id) if "abandoned" in n]
    assert any(silent in n for n in abandoned), (
        "the terminal transition must name the agent and its cause on the log"
    )


async def test_the_wall_clock_bound_abandons_a_silent_agent(
    workspace: Path, host_config: HarnessConfig, fake: FakeProvider
) -> None:
    """The same bound expressed in elapsed time, with the dispatch count off."""
    host_config.policy.max_unreported_dispatches = 0      # disabled
    store = RunStore(workspace / ".supervisor")
    host = HostInfo(name="claude-code", workspace=str(workspace), confidence=1.0)
    supervisor = Supervisor(workspace=workspace, config=host_config, store=store, host=host)

    response = await _reach_analysis(supervisor, fake)
    run_id = response.run_id
    dispatched = {p.agent_id for p in response.packets}

    # Armed only now. The bound used to be set before the run started, which put
    # it against the test's own setup rather than against a silent agent: on a
    # loaded machine, getting as far as the analysis fan-out takes longer than
    # 50ms, so the agents were abandoned -- correctly -- before the test had
    # finished dispatching them, and `_reach_analysis` failed on a fan-out of
    # one. `_abandonment_reason` reads the policy on each call, so arming it here
    # measures what the test is named for. Reproduced at 3 failures in 8
    # concurrent runs; nil in 12 unloaded ones, which is why it survived until
    # there was CI.
    host_config.policy.agent_timeout_seconds = 0.05
    await asyncio.sleep(0.1)
    response = await supervisor.advance(run_id)

    state = supervisor.store.load_state(run_id)
    assert all(state.agents[a].status is AgentStatus.FAILED for a in dispatched)
    assert state.phase is not Phase.ANALYZING
    assert any("abandoned" in n and "after its packet went out" in n
               for n in _notes(supervisor, run_id))


async def test_the_host_can_abandon_a_named_agent_with_a_reason(
    host_supervisor: Supervisor, fake: FakeProvider
) -> None:
    """The explicit entry point: the host says the subagent is gone."""
    simulator = HostSimulator(host_supervisor, fake)
    response = await _reach_analysis(host_supervisor, fake)
    run_id = response.run_id

    silent = response.packets[0].agent_id
    for packet in response.packets[1:]:
        await host_supervisor.report(
            packet.run_id, packet.agent_id, simulator._answer_for(packet)
        )

    reason = "the subagent was killed by an infrastructure failure"
    response = await host_supervisor.abandon(run_id, silent, reason)

    state = host_supervisor.store.load_state(run_id)
    assert state.agents[silent].status is AgentStatus.FAILED
    assert state.phase is not Phase.ANALYZING, "the phase should settle immediately"
    assert not [p for p in response.packets if p.agent_id == silent]

    notes = _notes(host_supervisor, run_id)
    assert any(silent in n and reason in n for n in notes), notes


async def test_abandoning_an_unknown_agent_leaves_the_run_intact(
    host_supervisor: Supervisor, fake: FakeProvider
) -> None:
    """A mistyped id is the caller's error, not the run's."""
    response = await _reach_analysis(host_supervisor, fake)
    run_id = response.run_id

    result = await host_supervisor.abandon(run_id, "agt_nonexistent", "gone")

    assert result.action != "failed"
    assert result.detail.get("error") == "unknown_agent"
    state = host_supervisor.store.load_state(run_id)
    assert state.phase is Phase.ANALYZING
    assert any(a.status in ACTIVE_AGENT_STATUSES for a in state.agents.values())


async def test_abandoning_an_execution_agent_settles_its_task(
    host_supervisor: Supervisor, fake: FakeProvider
) -> None:
    """A lost implementer leaves a failed task, not a run stuck in EXECUTING."""
    response = await _reach_analysis(host_supervisor, fake)
    run_id = response.run_id
    response = await _drive_until(host_supervisor, fake, response, "execution")

    packet = response.packets[0]
    await host_supervisor.abandon(run_id, packet.agent_id, "subagent cancelled")

    state = host_supervisor.store.load_state(run_id)
    agent = state.agents[packet.agent_id]
    assert agent.kind is AgentKind.EXECUTION
    assert agent.status is AgentStatus.FAILED
    assert state.phase is not Phase.EXECUTING
    task = state.tasks[packet.task_id]
    assert task.status is not TaskStatus.IN_PROGRESS, (
        "the task of an abandoned implementer must not stay in progress"
    )


async def test_an_abandoned_stage_is_not_replaced_by_an_identical_one(
    host_supervisor: Supervisor, fake: FakeProvider
) -> None:
    """Spawning a fresh synthesizer would re-issue the same packet forever."""
    response = await _reach_analysis(host_supervisor, fake)
    run_id = response.run_id
    response = await _drive_until(host_supervisor, fake, response, "synthesis")

    packet = response.packets[0]
    response = await host_supervisor.abandon(run_id, packet.agent_id, "subagent cancelled")

    assert response.action == "failed"
    assert "synthesis" in response.message
    state = host_supervisor.store.load_state(run_id)
    assert state.agents[packet.agent_id].status is AgentStatus.FAILED
    assert state.findings, "the analysis the run did do is still on the log"

    # Asking again names the same cause rather than dispatching a replacement.
    again = await host_supervisor.advance(run_id)
    assert not again.packets


async def test_an_abandoned_checkpoint_is_judged_on_the_mechanical_scoring(
    host_supervisor: Supervisor, fake: FakeProvider
) -> None:
    """Losing the judge costs the run its opinion, not its verdict."""
    response = await _reach_analysis(host_supervisor, fake)
    run_id = response.run_id
    response = await _drive_until(host_supervisor, fake, response, "checkpoint")

    packet = response.packets[0]
    response = await host_supervisor.abandon(run_id, packet.agent_id, "subagent cancelled")

    state = host_supervisor.store.load_state(run_id)
    assert state.checkpoints, "the run must still be scored on what the harness proved"
    assert state.phase is Phase.IMPROVING, "the checkpoint settled without its judge"

    # And the rest of the run is unaffected.
    final = await HostSimulator(host_supervisor, fake).drive(response)
    assert final.action == "complete", final.message


async def test_status_reports_the_note_explaining_a_failed_agent(
    workspace: Path, host_config: HarnessConfig, fake: FakeProvider
) -> None:
    """The reason existed only in the raw log, where no reader was looking.

    ``supervisor status`` reported an agent as FAILED and said nothing about
    why; the sentence naming the agent and its cause was emitted as a NOTE, and
    the fold dropped every one of them. Only ``supervisor events --type note``
    could retrieve it, which is not where anyone looks first.
    """
    host_config.policy.max_unreported_dispatches = 0
    store = RunStore(workspace / ".supervisor")
    host = HostInfo(name="claude-code", workspace=str(workspace), confidence=1.0)
    supervisor = Supervisor(workspace=workspace, config=host_config, store=store, host=host)

    response = await _reach_analysis(supervisor, fake)
    run_id = response.run_id
    silent = response.packets[0].agent_id
    host_config.policy.agent_timeout_seconds = 0.05
    await asyncio.sleep(0.1)
    await supervisor.advance(run_id)

    status = supervisor.status(run_id)
    assert status["notes"], "status reported a failed run with no reason attached"
    abandoned = [n for n in status["notes"] if "abandoned" in n["text"]]
    assert any(silent in n["text"] for n in abandoned), (
        f"no note in status names the abandoned agent: {[n['text'] for n in status['notes']]}"
    )
    # The log stays the complete record; status carries the tail of it.
    assert status["note_count"] >= len(status["notes"])
    assert len(status["notes"]) == len(_notes(supervisor, run_id)[-len(status["notes"]):])
