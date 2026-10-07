"""An agent whose tool rounds run out is told the turns it has left.

Measured on a local model (go-live run 7, item 9a): every implementer spent
its first turns reading until the tool budget ran out, was told only to
"answer now with what you have", and reported itself blocked -- one with
"need additional turns to write the implementation" and nine of ten turns
unused, another asking whether it might create a test file inside its own
scope. A blocked report parks the task for the owner, so the run ended with
four tasks parked and nothing written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from supervisor_harness.core.supervisor import (
    _LAST_TOOL_ROUND,
    MAX_TOOL_ROUNDS,
    Supervisor,
    _round_nudge,
)
from supervisor_harness.models import AgentKind, AgentSpec, Budget, RunState, Scope
from supervisor_harness.providers.base import CompletionRequest

from .conftest import FakeProvider

EXECUTION, ANALYSIS = AgentKind.EXECUTION, AgentKind.ANALYSIS


def test_an_implementer_out_of_tool_rounds_is_told_its_work_is_not_over() -> None:
    told = _round_nudge(MAX_TOOL_ROUNDS, 9, EXECUTION)
    assert "9 more turn(s)" in told
    assert "status `running`" in told
    assert "not for needing more turns" in told
    assert "not for creating a file inside your scope" in told


def test_on_its_last_turn_it_is_told_to_answer_with_what_it_has() -> None:
    told = _round_nudge(MAX_TOOL_ROUNDS, 0, EXECUTION)
    assert "Answer now with what you have" in told
    assert "more turn" not in told


def test_a_lens_is_told_its_turns_but_not_how_an_implementer_reports() -> None:
    told = _round_nudge(MAX_TOOL_ROUNDS, 3, ANALYSIS)
    assert "3 more turn(s)" in told
    assert "running" not in told


def test_an_implementer_is_warned_while_it_can_still_write() -> None:
    assert _round_nudge(MAX_TOOL_ROUNDS - 1, 5, EXECUTION) == _LAST_TOOL_ROUND
    assert "write the files in this round" in _LAST_TOOL_ROUND
    assert _round_nudge(MAX_TOOL_ROUNDS - 1, 5, ANALYSIS) == ""
    assert all(_round_nudge(r, 5, EXECUTION) == "" for r in range(MAX_TOOL_ROUNDS - 1))


async def test_the_count_is_the_turns_this_implementer_actually_has_left(
    supervisor: Supervisor, fake: FakeProvider, workspace: Path,
) -> None:
    """Driven for real: four turns, out of tool rounds on the first."""
    (workspace / "alpha.txt").write_text("ALPHA\n", encoding="utf-8")
    told: list[str] = []

    def respond(request: CompletionRequest) -> dict[str, Any]:
        last = request.messages[-1].content
        if "used all" in last:
            told.append(last)
            return {"output": "read alpha.txt; writing next", "status": "running",
                    "findings": []}
        return {"output": "", "status": "running", "findings": [],
                "tool_calls": [{"tool": "read_file", "args": {"path": "alpha.txt"}}]}

    # An agent with no task is briefed with the analysis contract, so the fake
    # files its requests under that stage; its kind is what the nudge reads.
    fake.script("analysis", *[respond] * (MAX_TOOL_ROUNDS + 1))
    session = supervisor.store.create(
        RunState(id="run_T", prompt="change it", workspace=str(workspace)))
    agent = AgentSpec(run_id="run_T", kind=EXECUTION, role="implementer", title="Implementer",
                      objectives=["change alpha"], scope=Scope(),
                      budget=Budget(max_turns=4),
                      binding=supervisor.config.binding_for("execution"))
    supervisor.lifecycle._spawn(session, [agent])
    await supervisor._drive_agent(session, session.state.agents[agent.id])

    assert told and "3 more turn(s)" in told[0]
