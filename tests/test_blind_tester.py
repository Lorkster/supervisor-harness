"""A task that tests other tasks' work is not shown their code (batch M).

Minesweeper run 11 verified all six tasks and failed four of the owner's hidden
tests, every one on chord. The engine listed the chorded cell in `lastRevealed`
and said why in a comment; the tester's test said the same in nearly the same
words. And its boards never put a mine where the engine's win check flagged one
on the way. Its brief already said to write each test "from the words quoted,
not from the code". Now its read tools will not show it the code.
"""

from __future__ import annotations

from pathlib import Path

from supervisor_harness.agents.brief import _requirements_block
from supervisor_harness.config import Policy
from supervisor_harness.core.phases import build_execution_agent
from supervisor_harness.core.requirements import attach_requirements
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.core.tools import Toolbox
from supervisor_harness.models import (
    AcceptanceTests,
    AgentKind,
    AgentSpec,
    ExecutionTask,
    Requirement,
    RunState,
    Scope,
)
from supervisor_harness.providers.base import ToolCall

CHORD = ("`chord` on a revealed cell whose number equals the count of flagged neighbours "
         "reveals every hidden, unflagged neighbour")


def _plan() -> tuple[list[ExecutionTask], list[Requirement]]:
    engine = ExecutionTask(title="Build the engine", requirements=["R1"],
                           scope=Scope(paths=["src/game/engine.ts"]))
    tests = ExecutionTask(title="Write the engine tests", scope=Scope(paths=["tests/**"]),
                          acceptance=[AcceptanceTests(["R1"], "npm test")])
    return [engine, tests], [Requirement(id="R1", quote=CHORD, source="docs/SPEC.md")]


def test_the_tester_is_not_shown_the_work_it_tests() -> None:
    tasks, ledger = _plan()
    engine, tests = tasks

    _, notes = attach_requirements(tasks, ledger, {})

    assert tests.scope.unread_paths == ["src/game/engine.ts"]
    assert engine.scope.unread_paths == [], "the builder reads its own code"
    assert any("is not shown src/game/engine.ts: it tests 'Build the engine'" in n
               for n in notes[tests.id])


def test_not_where_its_own_files_may_be() -> None:
    tasks, ledger = _plan()
    engine, tests = tasks
    engine.scope.paths = ["src/game/engine.ts", "tests/helpers.ts"]

    attach_requirements(tasks, ledger, {})

    assert tests.scope.unread_paths == ["src/game/engine.ts"], "it must read what it writes"


def test_its_agent_carries_the_fence(supervisor: Supervisor) -> None:
    tasks, ledger = _plan()
    attach_requirements(tasks, ledger, {})
    state = RunState(id="run_B", prompt="p", tasks={t.id: t for t in tasks})
    agent = build_execution_agent(state, tasks[1], supervisor.config,
                                  supervisor.packets._registry_for(  # type: ignore[arg-type]
                                      supervisor.store.create(state), None))
    assert agent.scope.unread_paths == ["src/game/engine.ts"]


def _workspace(tmp_path: Path) -> Toolbox:
    for rel, text in {"src/game/engine.ts": "export function chord() { /* listed at 0 */ }\n",
                      "src/game/types.ts": "export type Game = {}\n",
                      "docs/SPEC.md": "chord: listed at 0? no\n"}.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    return Toolbox(tmp_path, Policy())


def _tester() -> AgentSpec:
    return AgentSpec(id="agt_t", kind=AgentKind.EXECUTION,
                     scope=Scope(paths=["tests/**"], unread_paths=["src/game/engine.ts"]))


def test_its_read_tools_do_not_show_that_code(tmp_path: Path) -> None:
    toolbox, tester = _workspace(tmp_path), _tester()

    for path in ("src/game/engine.ts", "./src/game/engine.ts", "/src/game/engine.ts",
                 str(tmp_path / "src" / "game" / "engine.ts")):
        refused = toolbox.call("read_file", {"path": path}, tester)
        assert not refused.ok, path
        assert "is the work this task tests" in refused.output
    assert toolbox.call("read_file", {"path": "src/game/types.ts"}, tester).ok
    assert toolbox.call("read_file", {"path": "docs/SPEC.md"}, tester).ok

    found = toolbox.call("search", {"pattern": "listed at 0"}, tester).output
    assert "docs/SPEC.md" in found and "engine.ts" not in found
    builder = AgentSpec(id="agt_b", kind=AgentKind.EXECUTION, scope=Scope())
    assert "engine.ts" in toolbox.call("search", {"pattern": "listed at 0"}, builder).output
    assert toolbox.call("read_file", {"path": "src/game/engine.ts"}, builder).ok


async def test_nor_do_the_owners_analysis_tools(supervisor: Supervisor) -> None:
    (supervisor.workspace / "src").mkdir(exist_ok=True)
    (supervisor.workspace / "src" / "engine.ts").write_text("x\n", encoding="utf-8")
    session = supervisor.store.create(RunState(id="run_X", prompt="p"))
    tester = AgentSpec(id="agt_t", kind=AgentKind.EXECUTION,
                       scope=Scope(paths=["tests/**"], unread_paths=["src/engine.ts"]))

    ok, text = await supervisor._external_call(session, tester, ToolCall(
        "use_tool", {"name": "rea.read", "arguments": {"files": ["src/engine.ts"]}}))

    assert not ok and "is the work this task tests" in text


def test_its_brief_says_how_to_test_without_it() -> None:
    tasks, ledger = _plan()
    attach_requirements(tasks, ledger, {})
    state = RunState(id="run_Br", prompt="p", tasks={t.id: t for t in tasks},
                     requirements=ledger)

    brief = _requirements_block(state, tasks[1])
    assert ("The code of the work you test is not shown to you (src/game/engine.ts, built "
            "by 'Build the engine')") in brief
    assert "the corners and edges they allow" in brief
    assert "report blocked with needs `work`" in brief
    assert "is not shown to you" not in _requirements_block(state, tasks[0])


def test_nor_is_it_handed_that_code_to_fix(supervisor: Supervisor) -> None:
    """A blocked report, or a write a failing check named, widens a scope within the
    plan; never into the code a tester is not shown. That goes back as a finding."""
    from .test_blocked_inside_the_plan import BLOCKED, _box, _cache
    from .test_implementer_conversation import _writer

    _cache(supervisor.workspace)
    session, me = _writer(supervisor, "run_Fix")
    assert supervisor._blocked_within_reach(session.state, me, BLOCKED.arguments,
                                            _box(supervisor)) == ["src/cache.py"]
    me.scope.unread_paths = ["src/cache.py"]
    assert supervisor._blocked_within_reach(session.state, me, BLOCKED.arguments,
                                            _box(supervisor)) == []


async def test_nor_widened_to_write_it(supervisor: Supervisor) -> None:
    from supervisor_harness.models import CriterionStatus

    from .test_baseline_false_negatives import _ripple

    session, me, task = _ripple(supervisor, "run_W")
    task.dod[0].status = CriterionStatus.FAIL
    task.dod[0].evidence = f"{supervisor.workspace / 'docs' / 'design.md'}: stale reference"
    me.scope.unread_paths = ["docs/design.md"]

    await supervisor._widen_for_write(session, me, Toolbox(supervisor.workspace,
                                                           supervisor.config.policy),
                                      "docs/design.md")
    assert "docs/design.md" not in me.scope.paths
