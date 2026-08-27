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
from supervisor_harness.models import Backend, Phase, RunMode, TaskStatus
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

        if stage in self.answers.overrides:
            return dict(self.answers.overrides[stage])

        # Reuse the fake provider's canned answers, which key off the brief text.
        from supervisor_harness.providers.base import ChatMessage, CompletionRequest

        request = CompletionRequest(
            messages=[ChatMessage("user", packet.brief)],
            system=packet.brief[:200],
            json_schema=packet.schema,
        )
        return getattr(self.answers, f"_{stage}")(request)

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
