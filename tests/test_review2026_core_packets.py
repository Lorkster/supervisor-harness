"""A continuation names its own result file (review finding 4).

The first turn's brief was stored and replaced the fresh one on every later
turn, so the text a sub-agent read said to write `results/<agent>.t0.json`
while the packet's `result_path` said `.t1.json`: the sub-agent overwrote turn
0's result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from supervisor_harness.config import HarnessConfig, Policy, default_config
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.host.detect import HostInfo
from supervisor_harness.models import Backend, RunMode
from supervisor_harness.store.runstore import RunStore

from .conftest import FakeProvider
from .test_host_delegation import PROMPT, HostSimulator


@pytest.fixture
def host_config() -> HarnessConfig:
    cfg = default_config()
    cfg.backend = Backend.HOST
    cfg.routing = {k: "host" for k in cfg.routing}
    cfg.policy = Policy(default_max_turns=3, execution_max_turns=3, max_analysis_lenses=3)
    return cfg


async def test_the_second_turns_brief_names_the_second_turns_result(
    workspace: Path, host_config: HarnessConfig, fake: FakeProvider
) -> None:
    supervisor = Supervisor(
        workspace=workspace, config=host_config, store=RunStore(workspace / ".supervisor"),
        host=HostInfo(name="claude-code", workspace=str(workspace), confidence=1.0),
    )
    simulator = HostSimulator(supervisor, fake)
    response = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    planner = response.packets[0]
    await supervisor.report(planner.run_id, planner.agent_id, simulator._answer_for(planner))
    response = await supervisor.advance(response.run_id)
    first = response.packets[0]
    assert ".t0.json" in first.read_brief()

    answer = simulator._answer_for(first)
    answer["status"] = "running"
    again = await supervisor.report(first.run_id, first.agent_id, answer)
    second = next((p for p in again.packets if p.agent_id == first.agent_id), None)
    if second is None:
        again = await supervisor.advance(first.run_id)
        second = next(p for p in again.packets if p.agent_id == first.agent_id)

    assert second.result_path.endswith(".t1.json")
    brief = second.read_brief()
    assert ".t1.json" in brief
    assert ".t0.json" not in brief


async def test_entry_points_that_only_declare_agents_do_not_rebuild_the_registry(
    workspace: Path, host_config: HarnessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = Supervisor(
        workspace=workspace, config=host_config, store=RunStore(workspace / ".supervisor"),
        host=HostInfo(name="claude-code", workspace=str(workspace), confidence=1.0),
    )
    started = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    built: list[object] = []
    real = supervisor.packets._registry_for

    def spy(session, host_agents):
        built.append(host_agents)
        return real(session, host_agents)

    monkeypatch.setattr(supervisor.packets, "_registry_for", spy)

    await supervisor.abandon(started.run_id, "agt_nobody")          # returns before advancing
    await supervisor.approve(started.run_id, [])                    # not waiting: refused
    await supervisor.resolve(started.run_id, [])                    # nothing to apply

    assert built == [], "a scan of the agent directories was made for nothing"
