"""What an agent reads is what it is shown, or it is told what it was not shown.

Seen in a security-eval trial run: a security lens reviewing four files asked
for four reads per round, and each round's results were sliced to 8,000
characters. It saw part of the first file and nothing else -- not the other
three, not the "more lines" notice, not the instruction to continue -- and
after six rounds reported, honestly, that it had not reached two of the files.

A second route to the same scope bug turned up in the same run: the planner's
*run envelope* was the workspace's absolute path, and a lens that declared no
scope inherited it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from supervisor_harness.core.supervisor import TOOL_RESULT_CHARS, Supervisor
from supervisor_harness.core.tools import (
    MAX_READ_CHARS,
    Toolbox,
    ToolResult,
    render_results,
)
from supervisor_harness.models import AgentKind, AgentSpec, Budget, RunState, Scope

from .conftest import FakeProvider


def _box(tree: Path) -> Toolbox:
    from supervisor_harness.config import Policy

    return Toolbox(tree, Policy())


def test_a_long_read_stops_at_its_size_and_says_where_to_go_on(tmp_path: Path) -> None:
    (tmp_path / "dense.py").write_text(
        "\n".join(f"value_{i} = '{'x' * 70}'" for i in range(1, 301)), encoding="utf-8")
    box = _box(tmp_path)

    first = box.read_file("dense.py")
    assert len(first.output) <= MAX_READ_CHARS + 200
    marker = first.output.rsplit("continue with read_file start=", 1)
    assert len(marker) == 2, "the agent is told where to continue"
    resume = int(marker[1].rstrip(")"))
    last_shown = int(first.output.splitlines()[-2].split()[0])
    assert resume == last_shown + 1, "no gap and no overlap"

    second = box.read_file("dense.py", start=resume)
    assert second.output.splitlines()[1].split()[0] == str(resume)


def big(tool: str, marker: str, size: int) -> ToolResult:
    return ToolResult(tool, True, marker + "\n" + "y" * size)


def test_a_round_keeps_whole_results_and_names_what_it_left_out() -> None:
    results = [big("read_file", "FIRST", 9_000), big("read_file", "SECOND", 9_000),
               big("search", "THIRD", 9_000)]
    text = render_results(results, limit=20_000)
    assert len(text) <= 20_000
    assert "FIRST" in text and "SECOND" in text and "THIRD" not in text
    assert "### search\n(left out: this round's results are limited to 20000" in text
    assert text.endswith("give your answer now."), "the instruction is never cut off"


def test_a_single_result_over_the_limit_is_clipped_visibly() -> None:
    text = render_results([big("read_file", "ONLY", 50_000)], limit=10_000)
    assert len(text) <= 10_000 and "ONLY" in text
    assert "[cut here: a round's results are limited to 10000 characters" in text


def test_without_a_limit_nothing_is_touched() -> None:
    text = render_results([big("read_file", "A", 30_000)])
    assert "cut here" not in text and "left out" not in text


async def test_in_a_turn_the_agent_sees_what_it_read_or_is_told(
    supervisor: Supervisor, fake: FakeProvider, workspace: Path
) -> None:
    names = ["one.py", "two.py", "three.py", "four.py"]
    for name in names:
        (workspace / name).write_text(
            f"# MARK-{name}\n" + "\n".join(f"v{i} = {'1' * 70}" for i in range(110)),
            encoding="utf-8")
    seen: list[str] = []

    def respond(request: Any) -> dict[str, Any]:
        seen.append("\n".join(m.content for m in request.messages))
        if len(seen) > 1:
            return {"output": "done", "findings": [], "status": "done"}
        return {"output": "", "findings": [], "status": "running",
                "tool_calls": [{"tool": "read_file", "args": {"path": n}} for n in names]}

    fake.script("analysis", *[respond] * 4)
    session = supervisor.store.create(
        RunState(id="run_A", prompt="review", workspace=str(workspace)))
    agent = AgentSpec(
        run_id="run_A", kind=AgentKind.ANALYSIS, role="security", title="Security",
        objectives=["Review"], scope=Scope(), budget=Budget(max_turns=1),
        binding=supervisor.config.binding_for("analysis"))
    supervisor.lifecycle._spawn(session, [agent])
    await supervisor._drive_agent(session, session.state.agents[agent.id])

    second = seen[1]
    shown = [n for n in names if f"MARK-{n}" in second]
    left_out = second.count("(left out: this round's results are limited")
    assert len(shown) >= 2, "two full reads fit in a round"
    assert len(shown) + left_out == len(names), "every read is either shown or named"
    assert TOOL_RESULT_CHARS >= 2 * MAX_READ_CHARS


async def test_a_run_envelope_written_as_the_workspace_bounds_nothing_out(
    supervisor: Supervisor, fake: FakeProvider, workspace: Path
) -> None:
    from supervisor_harness.agents.registry import AgentRegistry
    from supervisor_harness.host.detect import HostInfo

    session = supervisor.store.create(
        RunState(id="run_A", prompt="review", workspace=str(workspace)))
    plan = {"lenses": [{"role": "security", "objectives": ["Review"]}],
            "envelope_paths": [str(workspace).replace("\\", "/")],
            "envelope_forbidden_paths": [str(workspace / "secrets") + "/**"]}
    supervisor._apply_plan(session, plan, fallback=[],
                           registry=AgentRegistry(workspace, HostInfo(name="unknown")))
    envelope = session.state.envelope
    assert envelope is not None
    assert envelope.paths == ["**"] and envelope.forbidden_paths == ["secrets/**"]


def test_a_configured_envelope_written_absolute_is_placed_too(
    supervisor: Supervisor, workspace: Path
) -> None:
    supervisor.config.policy.scope_envelope = [str(workspace / "src") + "/**"]
    assert supervisor._configured_envelope().paths == ["src/**"]
