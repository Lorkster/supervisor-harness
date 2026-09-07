"""The exported trajectory, and the two invariants that make it honest.

Batch 4 of `docs/development-plan.md`. The format is adapted from NVIDIA's ATIF
and carries two of its conventions as rules rather than as documentation:

* a deterministic step says so and carries no model metrics;
* a step that repeats earlier work names the step it repeats.

Both are enforced by `validate`, and both are tested here from two directions --
that the builder produces documents satisfying them, and that `validate` refuses
documents that do not. A rule only checked on output the builder happens to
produce is a rule that holds until the next step kind forgets it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from supervisor_harness.config import HarnessConfig, Policy, default_config
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.core.trajectory import (
    MIXED,
    MODEL,
    POLICY,
    SCHEMA,
    AgentTrajectory,
    Step,
    Trajectory,
    build_trajectory,
    validate,
)
from supervisor_harness.host.detect import HostInfo
from supervisor_harness.models import Backend, RunMode
from supervisor_harness.serde import to_jsonable
from supervisor_harness.store.runstore import RunStore

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"


@pytest.fixture
def supervisor(workspace: Path) -> Supervisor:
    cfg: HarnessConfig = default_config()
    cfg.backend = Backend.HOST
    cfg.routing = {k: "host" for k in cfg.routing}
    cfg.policy = Policy(default_max_turns=3, execution_max_turns=3, max_analysis_lenses=2)
    return Supervisor(
        workspace=workspace, config=cfg, store=RunStore(workspace / ".supervisor"),
        host=HostInfo(name="claude-code", workspace=str(workspace), confidence=1.0),
    )


async def _analysed(supervisor: Supervisor) -> str:
    """A run driven far enough to have a planner, a lens and a reported turn."""
    started = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    plan = started.packets[0]
    await supervisor.report(plan.run_id, plan.agent_id, {
        "restated_goal": "rate limit login", "mode": "execute",
        "lenses": [{"role": "security", "why": "exposure",
                    "objectives": ["Find the reachable attack path"]}],
    })
    analysis = await supervisor.advance(started.run_id)
    lens = analysis.packets[0]
    await supervisor.report(lens.run_id, lens.agent_id, {
        "output": "The login handler at src/auth/login.py:40 has no per-IP counter.",
        "findings": [{"title": "No rate limit", "detail": "none on the login path",
                      "severity": "high", "evidence": "src/auth/login.py:40"}],
        "files_examined": ["src/auth/login.py"],
        "status": "done",
    })
    return started.run_id


def _built(supervisor: Supervisor, run_id: str) -> Trajectory:
    return build_trajectory(
        supervisor.store.load_state(run_id), supervisor.store.open(run_id).events()
    )


# -- the document -----------------------------------------------------------


async def test_a_run_projects_into_a_valid_trajectory(supervisor: Supervisor) -> None:
    run_id = await _analysed(supervisor)

    trajectory = _built(supervisor, run_id)

    assert validate(trajectory) == []
    assert trajectory.schema == SCHEMA
    assert trajectory.run_id == run_id
    assert trajectory.prompt == PROMPT
    assert trajectory.agents, "a supervised run has agents, and they are the nesting"
    assert trajectory.totals["steps"] > len(trajectory.steps), "agents carry steps too"


async def test_each_agent_is_a_child_trajectory_with_its_own_identity(
    supervisor: Supervisor,
) -> None:
    """The nesting ATIF's shape is borrowed for: one document, resolvable ids."""
    run_id = await _analysed(supervisor)

    trajectory = _built(supervisor, run_id)

    by_role = {a.role: a for a in trajectory.agents}
    assert "security" in by_role
    lens = by_role["security"]
    assert lens.agent_id and lens.kind == "analysis"
    assert lens.title == "Security"
    assert [s.kind for s in lens.steps][:3] == ["spawned", "brief", "dispatched"]
    assert len({a.agent_id for a in trajectory.agents}) == len(trajectory.agents)


async def test_the_document_survives_json(supervisor: Supervisor) -> None:
    """Portable is the whole point; anything unserialisable is lost here."""
    run_id = await _analysed(supervisor)

    payload = json.loads(json.dumps(to_jsonable(_built(supervisor, run_id))))

    assert payload["schema"] == SCHEMA
    assert payload["harness"]["name"] == "supervisor-harness"
    assert payload["agents"][0]["steps"][0]["index"] == 1


# -- invariant one: a deterministic step carries no model metrics -----------


async def test_a_heuristic_assessment_is_marked_as_costing_nothing(
    supervisor: Supervisor,
) -> None:
    """The claim this convention exists to make checkable.

    The harness argues that continuous drift-watching is affordable because the
    watching is mostly heuristic. A reader of the log could not separate what
    was decided for free from what a model was asked, so the argument could not
    be checked against a real run.
    """
    run_id = await _analysed(supervisor)

    trajectory = _built(supervisor, run_id)

    drift = [s for a in trajectory.agents for s in a.steps if s.kind == "drift"]
    assert drift, "a reported turn is assessed"
    assert all(s.decided_by == POLICY for s in drift)
    assert all(s.metrics is None for s in drift)
    assert trajectory.totals["policy_steps"] > trajectory.totals["model_steps"]


async def test_a_reported_turn_is_the_model_half(supervisor: Supervisor) -> None:
    run_id = await _analysed(supervisor)

    turns = [s for a in _built(supervisor, run_id).agents for s in a.steps if s.kind == "turn"]

    assert turns
    assert all(s.decided_by == MODEL and s.source == "agent" for s in turns)


async def test_host_delegated_turns_carry_no_zero_filled_metrics(
    supervisor: Supervisor,
) -> None:
    """"Nobody was counting" must not read as "the model used nothing".

    In host-delegated mode the harness never sees a token. A metrics block of
    zeroes would be the same overstatement this project declined to make by
    adding cost fields only one backend can populate.
    """
    run_id = await _analysed(supervisor)

    turns = [s for a in _built(supervisor, run_id).agents for s in a.steps if s.kind == "turn"]

    assert all(s.metrics is None for s in turns)


def test_the_builder_refuses_to_put_metrics_on_a_deterministic_step() -> None:
    """Enforced where steps are made, not only where they are checked."""
    from supervisor_harness.core.trajectory import _Builder
    from supervisor_harness.models import RunState
    from supervisor_harness.store.events import Event, EventType

    builder = _Builder(RunState(id="run_x"))
    event = Event(run_id="run_x", type=EventType.NOTE, ts="2026-09-07T10:00:00Z")

    step = builder.add(event, "drift", decided_by=POLICY, metrics={"input_tokens": 100})

    assert step.metrics is None


def test_validate_refuses_metrics_on_a_deterministic_step() -> None:
    trajectory = Trajectory(steps=[
        Step(index=1, decided_by=POLICY, metrics={"input_tokens": 100}),
    ])

    problems = validate(trajectory)

    assert any("cannot have model metrics" in p for p in problems)


def test_validate_allows_metrics_on_a_mixed_step() -> None:
    """A checkpoint is deterministic scoring a model was allowed to adjust."""
    trajectory = Trajectory(steps=[
        Step(index=1, decided_by=MIXED, metrics={"input_tokens": 100}),
    ])

    assert validate(trajectory) == []


# -- invariant two: a repeated step names what it repeats -------------------


async def test_a_re_issued_packet_names_the_dispatch_it_repeats(
    supervisor: Supervisor,
) -> None:
    """The harness's answer to ATIF's compaction flag.

    It does not compact context, but it does re-issue: an agent that has not
    answered is handed the same brief again, and a consumer counting work would
    count it twice.
    """
    started = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    plan = started.packets[0]
    await supervisor.report(plan.run_id, plan.agent_id, {
        "restated_goal": "rate limit login", "mode": "execute",
        "lenses": [{"role": "security", "why": "exposure", "objectives": ["Find it"]}],
    })
    # Reporting the plan already advanced into analysis and dispatched the lens.
    # Advancing again without answering hands the same packet out once more.
    await supervisor.advance(started.run_id)

    trajectory = _built(supervisor, started.run_id)

    lens = next(a for a in trajectory.agents if a.role == "security")
    dispatches = [s for s in lens.steps if s.kind == "dispatched"]
    assert len(dispatches) >= 2, "the packet was handed out more than once"
    # A chain, not a flag: each re-issue names the one before it, so a consumer
    # can fold any number of them back onto the work they all repeat.
    assert dispatches[0].repeats is None
    assert [s.repeats for s in dispatches[1:]] == [s.index for s in dispatches[:-1]]


async def test_an_answered_dispatch_does_not_mark_the_next_one_as_a_repeat(
    supervisor: Supervisor,
) -> None:
    """A second turn is new work, not the same work asked for twice."""
    run_id = await _analysed(supervisor)

    trajectory = _built(supervisor, run_id)

    planner = next(a for a in trajectory.agents if a.role == "planner")
    assert [s.repeats for s in planner.steps if s.kind == "dispatched"] == [None]


def test_validate_refuses_a_repeat_that_does_not_point_backwards() -> None:
    forward = Trajectory(steps=[Step(index=1, repeats=2), Step(index=2)])
    itself = Trajectory(steps=[Step(index=1), Step(index=2, repeats=2)])

    assert any("must name an earlier step" in p for p in validate(forward))
    assert any("must name an earlier step" in p for p in validate(itself))


# -- the rest of what validate is for ---------------------------------------


def test_validate_refuses_steps_that_are_not_in_order() -> None:
    trajectory = Trajectory(steps=[Step(index=1), Step(index=3)])

    assert any("indices must run 1..n" in p for p in validate(trajectory))


def test_validate_refuses_two_agents_with_one_id() -> None:
    """ATIF requires this of embedded subagents, and it is why: a reference

    resolving to two documents resolves to neither.
    """
    trajectory = Trajectory(agents=[
        AgentTrajectory(agent_id="agt_1"), AgentTrajectory(agent_id="agt_1"),
    ])

    assert any("duplicate agent trajectory" in p for p in validate(trajectory))


def test_validate_refuses_an_agent_with_no_id() -> None:
    assert any("no agent_id" in p for p in validate(Trajectory(agents=[AgentTrajectory()])))


def test_validate_refuses_an_unknown_schema() -> None:
    assert any("unknown schema" in p for p in validate(Trajectory(schema="atif/1.7")))


def test_validate_refuses_an_unknown_decided_by() -> None:
    trajectory = Trajectory(steps=[Step(index=1, decided_by="vibes")])

    assert any("unknown decided_by" in p for p in validate(trajectory))


# -- the command ------------------------------------------------------------


async def test_the_cli_writes_a_trajectory(
    supervisor: Supervisor, workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from supervisor_harness.cli import cmd_trajectory

    run_id = await _analysed(supervisor)
    out = workspace / "trajectory.json"

    code = cmd_trajectory(argparse.Namespace(
        workspace=str(workspace), run_id=run_id, out=str(out), backend=None, json=False,
    ))

    assert code == 0
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["run_id"] == run_id
    assert "step(s) across" in capsys.readouterr().out
