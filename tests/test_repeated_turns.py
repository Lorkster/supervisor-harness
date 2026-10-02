"""Two faults one trial run showed together: a repeated turn's findings stored
twice, and every agent scored as drifting because its scope was an absolute path.

Seen in a security-eval run on a local model. The planner handed back the
workspace's absolute path as each lens's scope; every file the agents read was
then "outside the declared scope" (score 0.85 on the first turn). One agent then
repeated its turn word for word, and its three findings were recorded again
under new ids: nine findings in four places.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from supervisor_harness.contracts import parse_tasks
from supervisor_harness.core import phases
from supervisor_harness.core.drift import TurnContext, assess_heuristically
from supervisor_harness.core.paths import NOTHING, matches_any, relative_patterns
from supervisor_harness.core.supervision import new_findings
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import AgentSpec, AgentTurn, Budget, Finding, RunState, Scope

from .conftest import FakeProvider

WS = "C:/Users/someone/AppData/Local/Temp/work/repo"


# -- scope paths, as a model writes them -----------------------------------------


def test_the_workspace_itself_is_the_whole_workspace() -> None:
    assert relative_patterns([WS], WS) == ["**"]
    assert relative_patterns([WS + "/"], WS) == ["**"]
    assert relative_patterns(["c:\\users\\someone\\appdata\\local\\temp\\work\\repo"], WS) \
        == ["**"], "Windows: separators and case differ"


def test_a_path_beneath_the_workspace_becomes_relative_globs_and_all() -> None:
    assert relative_patterns([WS + "/src/auth/**", WS + "/README.md"], WS) \
        == ["src/auth/**", "README.md"]


def test_relative_patterns_pass_through() -> None:
    assert relative_patterns(["src/**", "./docs/*.md", ""], WS) == ["src/**", "docs/*.md"]


def test_a_rooted_path_elsewhere_names_nothing_here_rather_than_widening() -> None:
    out = relative_patterns(["D:/elsewhere/**"], WS)
    assert out == [NOTHING], "an empty list would mean the whole workspace"
    assert not matches_any("src/a.py", out)
    assert relative_patterns(["/etc/**"], "") == [NOTHING], "no workspace to place it in"


def _context(spec: AgentSpec, files: list[str]) -> TurnContext:
    return TurnContext(
        agent=spec, turn=AgentTurn(agent_id=spec.id, output="Read the config loader.",
                                   files_touched=files),
        previous_turns=[], brief=spec.brief, task_prompt="Review the code", turn_index=1,
        workspace=WS)


def test_a_lens_scoped_to_the_workspace_by_the_planner_is_not_drifting(
    config: Any, tmp_path: Path
) -> None:
    from supervisor_harness.agents.registry import AgentRegistry
    from supervisor_harness.host.detect import HostInfo

    state = RunState(prompt="Review the code", workspace=WS)
    plan = {"lenses": [{"role": "security", "objectives": ["Trace inputs to sinks"],
                        "scope_paths": [WS]}]}
    specs, _, _ = phases.apply_plan(state, plan, config,
                                    AgentRegistry(tmp_path, HostInfo(name="unknown")),
                                    fallback=[])
    (spec,) = [s for s in specs if s.role == "security"]
    assert spec.scope.paths == ["**"]

    assessment = assess_heuristically(_context(spec, ["pkg/config.py", "pkg/core/paths.py"]))
    assert "scope_paths" not in {s.kind for s in assessment.signals}


def test_a_genuinely_outside_file_is_still_outside(config: Any, tmp_path: Path) -> None:
    from supervisor_harness.agents.registry import AgentRegistry
    from supervisor_harness.host.detect import HostInfo

    state = RunState(prompt="Review the code", workspace=WS)
    plan = {"lenses": [{"role": "security", "scope_paths": [WS + "/pkg/auth/**"]}]}
    specs, _, _ = phases.apply_plan(state, plan, config,
                                    AgentRegistry(tmp_path, HostInfo(name="unknown")),
                                    fallback=[])
    (spec,) = [s for s in specs if s.role == "security"]
    assessment = assess_heuristically(_context(spec, ["pkg/billing.py"]))
    assert "scope_paths" in {s.kind for s in assessment.signals}


def test_task_scopes_from_the_planner_are_made_relative_too() -> None:
    (task,) = parse_tasks({"tasks": [{"title": "Fix it", "scope_paths": [WS + "/pkg/a.py"]}]},
                          "run_A", WS)
    assert task.scope.paths == ["pkg/a.py"]


# -- findings from a repeated turn ------------------------------------------------


def _finding(**kw: Any) -> Finding:
    base: dict[str, Any] = dict(agent_id="agt_1", lens="security", title="Env vars redirect",
                                detail="d", evidence=["pkg/config.py:474"], path="pkg/config.py",
                                line_start=474, line_end=478, cwe="CWE-732")
    base.update(kw)
    return Finding(**base)


def test_only_word_for_word_repeats_by_the_same_agent_are_held_back() -> None:
    recorded = [_finding()]
    assert new_findings([_finding()], recorded) == [], "a fresh id does not make it new"
    revised = _finding(line_end=490)
    other_agent = _finding(agent_id="agt_2")
    assert new_findings([revised, other_agent], recorded) == [revised, other_agent]
    twice = _finding(title="New")
    assert len(new_findings([twice, _finding(title="New")], recorded)) == 1, "within a turn too"


async def test_a_repeated_turn_records_its_findings_once(
    workspace: Path, config: Any, fake: FakeProvider
) -> None:
    from supervisor_harness.host.detect import HostInfo
    from supervisor_harness.providers.router import ModelRouter
    from supervisor_harness.store.runstore import RunStore

    router = ModelRouter(config, host_name="test-host")
    router.register("fake", fake)
    supervisor = Supervisor(
        workspace=workspace, config=config, store=RunStore(workspace / ".supervisor"),
        host=HostInfo(name="test-host", workspace=str(workspace), confidence=1.0),
        router=router,
    )
    session = supervisor.store.create(
        RunState(id="run_A", prompt="Review", workspace=str(workspace)))
    supervisor.lifecycle._spawn(session, [AgentSpec(
        id="agt_1", role="security", title="Security", objectives=["x"],
        scope=Scope(), budget=Budget(max_turns=4))])
    live = session.state.agents["agt_1"]
    payload = {"output": "Same answer.", "status": "running", "findings": [
        {"severity": "medium", "title": "Env vars redirect", "detail": "d",
         "evidence": ["config.py:474"], "confidence": 0.5}]}

    first = await supervisor.supervision._record_turn(session, live, payload)
    second = await supervisor.supervision._record_turn(session, live, payload)

    assert len(session.state.findings) == 1
    assert len(first.findings) == len(second.findings) == 1, "the turn keeps what was said"
    reopened = supervisor.store.open("run_A").state
    assert len(reopened.findings) == 1, "and so does a replay of the log"
