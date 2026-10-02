"""An analysis agent's "done" is not accepted before it has looked at its scope.

From the 18-file local run through security-eval: the security lens read 4 of
the 18 files, named the ones it had skipped in its own self-assessment, said
"done", and was accepted after one turn of six. Nothing compared what it read
with what it was asked to cover.

The rule pinned here: a "done" with turns left and less than
``policy.min_scope_coverage`` of the scope read is sent back once, naming what
was not read. The next "done" is accepted whatever was read -- the agent has
seen the list and chosen -- and every analysis agent's coverage goes on the
record, so a miss can be told apart as "never opened" or "read and missed".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness.config import Backend, HarnessConfig, Policy, default_config, load_config
from supervisor_harness.core.drift import (
    COVERAGE_LIST_LIMIT,
    COVERAGE_RATIONALE,
    TurnContext,
    assess_heuristically,
    decide_directive,
    scope_coverage,
)
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.core.tools import Toolbox
from supervisor_harness.host.detect import HostInfo
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    AgentStatus,
    AgentTurn,
    DirectiveKind,
    DriftAssessment,
    RunMode,
    Scope,
)
from supervisor_harness.providers.base import CompletionRequest, CompletionResponse
from supervisor_harness.store.events import EventType
from supervisor_harness.store.runstore import RunStore

from .conftest import FakeProvider
from .test_host_delegation import HostSimulator

PROMPT = "Review src/auth for security problems"
EXTRA = ("session.py", "tokens.py", "reset.py")


@pytest.fixture
def wide(workspace: Path) -> Path:
    """The shared workspace, with three more files beside ``login.py``."""
    for name in EXTRA:
        (workspace / "src" / "auth" / name).write_text(f"# {name}\n", encoding="utf-8")
    return workspace


@pytest.fixture
def covering(supervisor: Supervisor) -> Supervisor:
    supervisor.config.policy.min_scope_coverage = 0.5
    # Room for the check to act before the objective-coverage heuristic, which
    # waits for half the budget, has anything to say about a second turn.
    supervisor.config.policy.default_max_turns = 6
    return supervisor


class Reads(FakeProvider):
    """Reads ``paths`` on the first round of each analysis turn, then answers."""

    def __init__(self, paths: list[str], examined: list[str] | None = None) -> None:
        super().__init__()
        self.paths = paths
        self.examined = examined
        self.seen: set[str] = set()

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if self._classify(request) == "analysis":
            key = request.messages[-1].content[:300]
            if key not in self.seen and self.paths:
                self.seen.add(key)
                payload = {"tool_calls": [{"tool": "read_file", "args": {"path": p}}
                                          for p in self.paths]}
                return CompletionResponse(text=json.dumps(payload), model="fake-1",
                                          provider="fake")
            if self.examined is not None:
                answer = dict(self.answer_for("analysis", request))
                answer["files_examined"] = self.examined
                return CompletionResponse(text=json.dumps(answer), model="fake-1",
                                          provider="fake")
        return await super().complete(request)


def _analysis_directives(supervisor: Supervisor, run_id: str) -> dict[str, list[Any]]:
    state = supervisor.store.open(run_id).state
    out: dict[str, list[Any]] = {}
    for d in state.directives:
        agent = state.agents.get(d.agent_id)
        if agent is not None and agent.kind is AgentKind.ANALYSIS:
            out.setdefault(d.agent_id, []).append(d)
    return out


def _coverage_notes(supervisor: Supervisor, run_id: str) -> list[dict[str, Any]]:
    return [
        e.payload for e in supervisor.store.log(run_id).read()
        if e.type is EventType.NOTE and e.payload.get("text", "").startswith("scope coverage")
    ]


# -- the run ---------------------------------------------------------------------


async def test_a_lens_that_read_nothing_is_sent_back_once_naming_its_files(
    wide: Path, covering: Supervisor
) -> None:
    covering.router.register("fake", Reads([]))

    response = await covering.run(PROMPT, mode=RunMode.REPORT)

    directives = _analysis_directives(covering, response.run_id)
    assert directives
    for kinds in directives.values():
        first, second = kinds[0], kinds[1]
        assert first.kind is DirectiveKind.DEEPEN
        assert first.rationale.startswith(COVERAGE_RATIONALE)
        named = " ".join(first.corrections)
        assert "src/auth/login.py" in named and "src/auth/tokens.py" in named
        # Sent back once: the agent stood by its answer, and that is accepted.
        assert second.kind is DirectiveKind.ACCEPT

    notes = _coverage_notes(covering, response.run_id)
    assert len(notes) == len(directives)
    assert all(n["read"] == 0 and "src/auth/session.py" in n["unread"] for n in notes)


async def test_a_lens_that_read_its_scope_is_accepted_at_once(
    wide: Path, covering: Supervisor
) -> None:
    files = ["src/auth/login.py", *(f"src/auth/{n}" for n in EXTRA)]
    covering.router.register("fake", Reads(files))

    response = await covering.run(PROMPT, mode=RunMode.REPORT)

    for kinds in _analysis_directives(covering, response.run_id).values():
        assert kinds[0].kind is DirectiveKind.ACCEPT, kinds[0].rationale
    notes = _coverage_notes(covering, response.run_id)
    assert notes and all(n["read"] == n["in_scope"] and not n["unread"] for n in notes)
    assert all(set(n["files_read"]) == set(files) for n in notes)


async def test_an_autonomous_lens_is_counted_by_what_it_opened_not_what_it_says(
    wide: Path, covering: Supervisor
) -> None:
    """Listing every file in files_examined, having opened none, is still 0 of 4."""
    everything = ["src/auth/login.py", *(f"src/auth/{n}" for n in EXTRA)]
    covering.router.register("fake", Reads([], examined=everything))

    response = await covering.run(PROMPT, mode=RunMode.REPORT)

    for kinds in _analysis_directives(covering, response.run_id).values():
        assert kinds[0].kind is DirectiveKind.DEEPEN
        assert kinds[0].rationale.startswith(COVERAGE_RATIONALE)


async def test_read_files_are_measured_onto_the_turn(wide: Path, covering: Supervisor) -> None:
    covering.router.register("fake", Reads(["src/auth/login.py", "src/auth/nope.py"]))

    response = await covering.run(PROMPT, mode=RunMode.REPORT)

    state = covering.store.open(response.run_id).state
    analysis = [t for t in state.turns if state.agents[t.agent_id].kind is AgentKind.ANALYSIS]
    # The failed read of a file that does not exist is not a read.
    assert any(t.files_read == ["src/auth/login.py"] for t in analysis)
    assert not any("src/auth/nope.py" in t.files_read for t in analysis)


async def test_the_check_off_accepts_as_before(wide: Path, covering: Supervisor) -> None:
    covering.config.policy.min_scope_coverage = 0.0
    covering.router.register("fake", Reads([]))

    response = await covering.run(PROMPT, mode=RunMode.REPORT)

    for kinds in _analysis_directives(covering, response.run_id).values():
        assert kinds[0].kind is DirectiveKind.ACCEPT


# -- a host-run agent: its own report is what there is ---------------------------


@pytest.fixture
def host_supervisor(wide: Path) -> Supervisor:
    cfg: HarnessConfig = default_config()
    cfg.backend = Backend.HOST
    cfg.routing = {k: "host" for k in cfg.routing}
    cfg.policy = Policy(default_max_turns=6, max_analysis_lenses=2)
    store = RunStore(wide / ".supervisor")
    host = HostInfo(name="claude-code", workspace=str(wide), confidence=1.0)
    return Supervisor(workspace=wide, config=cfg, store=store, host=host)


async def _host_analysis_directives(
    supervisor: Supervisor, examined: list[str]
) -> dict[str, list[Any]]:
    fake = FakeProvider()
    analysis = fake._analysis

    def answer(request: CompletionRequest) -> dict[str, Any]:
        return {**analysis(request), "files_examined": examined}

    fake._analysis = answer  # type: ignore[method-assign]
    simulator = HostSimulator(supervisor, fake)
    response = await simulator.drive(await supervisor.start(PROMPT, mode=RunMode.REPORT))
    return _analysis_directives(supervisor, response.run_id)


async def test_a_host_run_lens_is_counted_by_what_it_reports(host_supervisor: Supervisor) -> None:
    everything = ["src/auth/login.py", *(f"src/auth/{n}" for n in EXTRA)]
    directives = await _host_analysis_directives(host_supervisor, everything)
    assert directives
    for kinds in directives.values():
        assert kinds[0].kind is DirectiveKind.ACCEPT, kinds[0].rationale


async def test_a_host_run_lens_reporting_one_file_of_four_is_sent_back(
    host_supervisor: Supervisor,
) -> None:
    directives = await _host_analysis_directives(host_supervisor, ["src/auth/login.py"])
    assert directives
    for kinds in directives.values():
        assert kinds[0].kind is DirectiveKind.DEEPEN
        assert "1 of 4 files" in kinds[0].rationale
        assert kinds[1].kind is DirectiveKind.ACCEPT


# -- the decision itself ---------------------------------------------------------


def _done() -> AgentTurn:
    return AgentTurn(output="found the problems", claimed_status=AgentStatus.DONE)


def _agent() -> AgentSpec:
    return AgentSpec(id="a", kind=AgentKind.ANALYSIS, objectives=[])


def test_a_done_on_the_last_turn_is_not_sent_back() -> None:
    """There is no turn left to read in; with one left, there is."""
    agent = _agent()
    coverage = scope_coverage(["a.py", "b.py", "c.py"], set())

    def kind(turns_used: int) -> DirectiveKind:
        return decide_directive(DriftAssessment(), agent, _done(), Policy(),
                                turns_used=turns_used, coverage=coverage).kind

    assert kind(agent.budget.max_turns) is not DirectiveKind.DEEPEN
    assert kind(agent.budget.max_turns - 1) is DirectiveKind.DEEPEN


@pytest.mark.parametrize(("read", "kind"), [
    (set(), DirectiveKind.DEEPEN),
    ({"a.py"}, DirectiveKind.DEEPEN),             # 1 of 4 is under a half
    ({"a.py", "b.py"}, DirectiveKind.ACCEPT),     # 2 of 4 is a half
    ({"a.py", "b.py", "elsewhere.py"}, DirectiveKind.ACCEPT),
])
def test_the_threshold_is_a_share_of_the_scope(read: set[str], kind: DirectiveKind) -> None:
    coverage = scope_coverage(["a.py", "b.py", "c.py", "d.py"], read)
    directive = decide_directive(DriftAssessment(), _agent(), _done(),
                                 Policy(min_scope_coverage=0.5), turns_used=1,
                                 coverage=coverage)
    assert directive.kind is kind


def test_a_long_list_of_unread_files_is_summarised() -> None:
    files = [f"f{i:02}.py" for i in range(COVERAGE_LIST_LIMIT + 5)]
    directive = decide_directive(DriftAssessment(), _agent(), _done(), Policy(), turns_used=1,
                                 coverage=scope_coverage(files, set()))
    text = directive.corrections[0]
    assert f"`f{COVERAGE_LIST_LIMIT - 1:02}.py`" in text
    assert f"`f{COVERAGE_LIST_LIMIT:02}.py`" not in text
    assert "and 5 more" in text


def test_standing_by_a_done_answer_is_not_circling() -> None:
    """A repeated answer called "done" is what the repetition correction asks for."""
    words = ("the session token is compared with a timing unsafe equality in the reset "
             "flow, and the password hash uses a fixed salt stored beside the cookie "
             "secret in settings")
    agent = AgentSpec(objectives=["find token handling problems"])
    previous = [AgentTurn(output=words, claimed_status=AgentStatus.DONE)]

    def kinds(status: AgentStatus) -> set[str]:
        return {s.kind for s in assess_heuristically(TurnContext(
            agent=agent, turn=AgentTurn(output=words, claimed_status=status),
            previous_turns=previous, brief="", task_prompt=PROMPT, turn_index=1,
        )).signals}

    assert "repetition" not in kinds(AgentStatus.DONE)
    assert "repetition" in kinds(AgentStatus.RUNNING)


# -- the parts ---------------------------------------------------------------------


def test_scope_files_is_the_scope_without_its_forbidden_paths(wide: Path) -> None:
    toolbox = Toolbox(wide, Policy())
    scope = Scope(paths=["src/auth/**"], forbidden_paths=["src/auth/reset.py"])
    assert toolbox.scope_files(scope) == [
        "src/auth/login.py", "src/auth/session.py", "src/auth/tokens.py",
    ]
    assert "src/auth/reset.py" in toolbox.scope_files(Scope())


def test_a_read_names_its_file_and_a_failed_read_names_none(wide: Path) -> None:
    toolbox = Toolbox(wide, Policy())
    assert toolbox.read_file("src/auth/login.py").path == "src/auth/login.py"
    assert toolbox.read_file("src/auth/nope.py").path == ""


def test_a_workspace_config_cannot_lower_the_bar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUPERVISOR_HOME", str(tmp_path / "home"))
    (tmp_path / "supervisor.config.json").write_text(
        '{"policy": {"min_scope_coverage": 0.0}}', encoding="utf-8",
    )
    cfg = load_config(workspace=tmp_path)
    assert cfg.policy.min_scope_coverage == Policy().min_scope_coverage
    assert any("min_scope_coverage" in r for r in cfg.rejected_settings), cfg.rejected_settings
