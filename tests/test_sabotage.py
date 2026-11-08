"""Batch P: a run's tests must catch a break of what they test.

Minesweeper runs 11, 23 and 24 each failed the owner's chord test with a
different engine bug, and each run's own chord test could not have seen it.
Measured with no model, by breaking each run's engine by hand: a break of the
chord distances got through every one of the three runs' chord tests, and a
chord that ignores flags, or a mine count off by one, was caught every time.
These tests hold the harness to the same bar: a break is applied in place, the
tests run, the file comes back byte for byte, and only the run decides.

The runner here is a stand-in for vitest that writes its JSON report from the
file it would test, so no Node install is needed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness.agents.brief import build_verifier_brief
from supervisor_harness.core import requirements, sabotage
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    Break,
    CriterionStatus,
    DoDCriterion,
    ExecutionTask,
    Requirement,
    RunState,
    Scope,
    TaskStatus,
    VerifyMethod,
)
from supervisor_harness.providers.base import CompletionRequest, CompletionResponse, ToolCall

from .conftest import FakeProvider

GAME = ('export function add(a: number, b: number) {\n  return a + b;\n}\n'
        'export function name() {\n  return "game";\n}\n// a note\n')
ADD = "add [R1] returns the sum"
NAME = "name [R2] names the game"


def _project(root: Path, game: str = GAME) -> Path:
    (root / "src").mkdir(exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}),
                                       encoding="utf-8")
    (root / "src" / "game.ts").write_bytes(game.encode("utf-8"))
    (root / "tests" / "game.test.ts").write_text("// [R1] [R2]\n", encoding="utf-8")
    return root


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """A stand-in vitest: each test passes while the code it tests is intact."""
    calls: list[list[str]] = []

    def run(argv: list[str], cwd: Path | str, timeout: float,
            extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        text = (Path(cwd) / "src" / "game.ts").read_text(encoding="utf-8")
        if "TIMEOUT" in text:
            raise subprocess.TimeoutExpired(argv, timeout)
        out = next(a.split("=", 1)[1] for a in argv if a.startswith("--outputFile="))
        broken = "SYNTAX(" in text
        tests = [(ADD, "return a + b;" in text and not broken),
                 (NAME, '"game"' in text and not broken)]
        Path(out).write_text(json.dumps({"testResults": [{"assertionResults": [
            {"fullName": n, "status": "passed" if ok else "failed"} for n, ok in tests]}]}),
            encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sabotage, "run_bounded", run)
    monkeypatch.setattr(sabotage, "resolve_runner", lambda name: (name, ""))
    return calls


def _crit(*breaks: Break) -> DoDCriterion:
    crit = sabotage.criterion("R1", "npm test")
    crit.breaks = list(breaks)
    return crit


def _break(find: str, replace: str, file: str = "src/game.ts", clause: int = 1) -> Break:
    return Break(requirement="R1", clause=clause, file=file, find=find, replace=replace)


def test_a_break_the_tests_catch_passes_and_the_file_comes_back(
        tmp_path: Path, runner: list[list[str]]) -> None:
    tree = _project(tmp_path)
    outcome, tried = sabotage.verify(_crit(_break("return a + b;", "return a - b;")),
                                     tree, 60, ["src/"], ["tests/"])
    assert outcome.status is CriterionStatus.PASS, outcome.evidence
    assert [t.verdict for t in tried] == ["caught"] and tried[0].red == [ADD]
    assert (tree / "src" / "game.ts").read_bytes() == GAME.encode("utf-8")
    assert runner[0][:2] == ["npm", "test"] and runner[0][2] == "--", "npm passes the flags on"


def test_a_break_that_gets_through_fails_and_tells_the_tester(
        tmp_path: Path, runner: list[list[str]]) -> None:
    """Run 24: every distance plus one, and "an integer of at least 1" stays green."""
    tree = _project(tmp_path)
    crit = _crit(_break("add(a: number", "add(a: any", clause=2))
    outcome, tried = sabotage.verify(crit, tree, 60, ["src/"], ["tests/"])

    assert outcome.status is CriterionStatus.FAIL
    assert "every test marked [R1] still passed" in outcome.evidence
    assert "`add(a: number` became `add(a: any`" in outcome.evidence
    req = Requirement(id="R1", quote="add returns the sum. It returns a number.")
    told = sabotage.told(tried, req, requirements.clauses(req.quote))
    assert 'clause 2 ("It returns a number.")' in told
    assert "report blocked" in told and "the owner decides" in told


def test_a_break_that_breaks_everything_is_not_counted(
        tmp_path: Path, runner: list[list[str]]) -> None:
    """"A bad sabotage looks exactly like a bad test": no other test passing, no verdict."""
    tree = _project(tmp_path)
    outcome, tried = sabotage.verify(_crit(_break("export function name", "SYNTAX(")),
                                     tree, 60, ["src/"], ["tests/"])
    assert tried[0].verdict == "invalid" and "broken everything" in tried[0].note
    assert outcome.status is CriterionStatus.WAIVED
    assert "faithfulness review alone stands" in outcome.evidence


def test_the_file_comes_back_when_the_run_does_not_finish(
        tmp_path: Path, runner: list[list[str]]) -> None:
    tree = _project(tmp_path)
    _, tried = sabotage.verify(_crit(_break("// a note", "// TIMEOUT")),
                                     tree, 60, ["src/"], ["tests/"])
    assert tried[0].verdict == "invalid" and "did not finish" in tried[0].note
    assert (tree / "src" / "game.ts").read_bytes() == GAME.encode("utf-8")


def test_a_test_already_failing_does_not_catch_anything(
        tmp_path: Path, runner: list[list[str]]) -> None:
    tree = _project(tmp_path, GAME.replace("a + b", "a - b"))
    outcome, tried = sabotage.verify(_crit(_break("// a note", "// changed")),
                                     tree, 60, ["src/"], ["tests/"])
    assert "no test marked [R1] passed with nothing broken" in tried[0].note
    assert outcome.status is CriterionStatus.WAIVED


def test_a_break_goes_only_into_the_code_the_tests_test(tmp_path: Path) -> None:
    tree = _project(tmp_path)
    (tree / "src" / "other.ts").write_text("x\nx\n", encoding="utf-8")
    (tree / "src" / "game.spec.ts").write_text("y\n", encoding="utf-8")

    def why(brk: Break, builders: list[str] | None = None) -> str:
        return sabotage.refusal(brk, tree, builders or ["src/"], ["tests/"]) or ""

    assert why(_break("return a + b;", "return 0;")) == ""
    assert "a test file" in why(_break("// [R1]", "//", file="tests/game.test.ts"))
    assert "a test file" in why(_break("y", "z", file="src/game.spec.ts"))
    assert "outside the scope" in why(_break("return a + b;", "return 0;"), ["lib/"])
    assert "found 2 times" in why(_break("x", "y", file="src/other.ts"))
    assert "changes nothing" in why(_break("return a + b;", "return a + b;"))
    assert "not a file" in why(_break("a", "b", file="../outside.ts"))


def test_which_runner_and_how_it_is_told_to_report(tmp_path: Path) -> None:
    tree = _project(tmp_path)
    assert sabotage.runner_of("npm test", tree) == sabotage.VITEST
    assert sabotage.runner_of("npx playwright test", tree) == sabotage.PLAYWRIGHT
    assert sabotage.runner_of("npm run test:e2e", tree) == "", "no such script"
    assert sabotage.runner_of("pytest -q", tree) == "", "its report carries no marks"

    argv, env = sabotage.reporting("npx playwright test", sabotage.PLAYWRIGHT,
                                   tmp_path / "r.json")
    assert argv == ["npx", "playwright", "test", "--reporter=json"]
    assert env == {"PLAYWRIGHT_JSON_OUTPUT_NAME": str(tmp_path / "r.json")}

    report = tmp_path / "pw.json"
    report.write_text(json.dumps({"suites": [{"title": "board.spec.ts", "specs": [],
                                              "suites": [{"title": "ripple", "specs": [
        {"title": "[R30] cells ripple", "tests": [{"status": "unexpected"}]},
        {"title": "[R31] a flag animates", "tests": [{"status": "expected"}]},
        {"title": "[R32] skipped", "tests": [{"status": "skipped"}]}]}]}]}),
        encoding="utf-8")
    assert sabotage.results(sabotage.PLAYWRIGHT, report) == [
        ("board.spec.ts > ripple > [R30] cells ripple", False),
        ("board.spec.ts > ripple > [R31] a flag animates", True)]
    assert sabotage.marked("x > [R30] cells ripple", "R30")
    assert not sabotage.marked("x > [R300] y", "R30")


def test_proposals_are_capped_and_browser_breaks_more_so() -> None:
    same = {"clause": 1, "file": "src/a.ts", "find": "a", "replace": "b"}
    raw = {"breaks": [same, same, {**same, "find": "c"}, {**same, "find": "d"},
                      {**same, "find": "e"}, "junk", {"clause": "x", "file": "", "find": "f"}]}
    got = sabotage.parse_breaks(raw, "R7")
    assert [b.find for b in got] == ["a", "c", "d"], "deduplicated, then at most three"
    assert all(b.requirement == "R7" for b in got)
    assert sabotage.parse_breaks(None, "R7") == []
    assert len(sabotage.capped(got, browser=False, taken=0)) == 3
    assert len(sabotage.capped(got, browser=True, taken=0)) == 1
    assert sabotage.capped(got, browser=True, taken=5) == []


def test_a_tester_is_held_to_it_for_what_it_tests_by_running() -> None:
    ledger = {"R1": Requirement(id="R1", quote="add returns the sum.", checked_by="running"),
              "R2": Requirement(id="R2", quote="package.json has no dependencies.",
                                checked_by="reading")}
    held = requirements.tester_criteria(ledger, ["R1", "R2"], "tests",
                                        {"R1": "npm test", "R2": "npm test"})
    breaks = [c for c in held if c.method is VerifyMethod.CATCHES_BREAKS]
    assert [(c.requirement, c.command) for c in breaks] == [("R1", "npm test")]
    assert breaks[0].statement == "The tests marked [R1] catch a break of each clause of it"
    assert not any(c.method is VerifyMethod.CATCHES_BREAKS
                   for c in requirements.tester_criteria(ledger, ["R1"], "tests")), \
        "not without the commands, which come only where the harness may run them"


class Saboteur(FakeProvider):
    """Native tools; proposes one break that gets through and one into a test file."""

    native_tools = True

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if not any(t["name"] == "breaks" for t in request.tools or []):
            return await super().complete(request)
        self.requests.append(request)
        return CompletionResponse(tool_calls=[ToolCall("breaks", {"breaks": [
            {"clause": 1, "file": "src/game.ts", "find": "add(a: number",
             "replace": "add(a: any"},
            {"clause": 2, "file": "tests/game.test.ts", "find": "// [R1]", "replace": "//"},
        ]})])


def _run(supervisor: Supervisor) -> tuple[Any, ExecutionTask]:
    _project(supervisor.workspace)
    session = supervisor.store.create(RunState(id="run_P", prompt="Build the game"))
    session.state.requirements = [Requirement(
        id="R1", quote="add returns the sum of its arguments. It returns a number.")]
    builder = ExecutionTask(title="Build the game", scope=Scope(paths=["src/"]),
                            requirements=["R1"], status=TaskStatus.VERIFIED)
    tester = ExecutionTask(title="Test the game", scope=Scope(paths=["tests/"]),
                           status=TaskStatus.AWAITING_VERIFICATION, attempts=1,
                           dod=[sabotage.criterion("R1", "npm test")])
    for task in (builder, tester):
        session.state.tasks[task.id] = task
    return session, tester


async def test_the_saboteur_proposes_and_the_harness_rules(
        supervisor: Supervisor, runner: list[list[str]]) -> None:
    supervisor.config.policy.allow_command_execution = True
    fake = Saboteur()
    supervisor.router.register("fake", fake)
    session, tester = _run(supervisor)

    await supervisor._verify_off_the_loop(session)

    (crit,) = tester.dod
    assert [b.file for b in crit.breaks] == ["src/game.ts"], "the test file was refused"
    assert crit.breaks[0].attempt == 1 and crit.breaks_asked == 1
    assert crit.status is CriterionStatus.FAIL and crit.verified_by == "harness"
    assert "every test marked [R1] still passed" in crit.evidence
    assert any("report blocked" in t for t in tester.told), "the tester is told how to dispute"
    assert any("saboteur on R1" in n.text and "a test file" in n.text
               for n in session.state.notes)
    brief = fake.requests[0].messages[0].content
    assert "1. add returns the sum of its arguments." in brief and "`src/`" in brief
    assert {t["name"] for t in fake.requests[0].tools or []}.isdisjoint(
        {"edit_file", "write_file", "run_command", "report"}), "read-only"

    await supervisor._verify_off_the_loop(session)
    assert len(fake.requests) == 1, "asked once per attempt"
    assert crit.statement not in build_verifier_brief(session.state, tester).split(
        "What the harness already ran")[0], "never a model's to judge"


async def test_a_saboteur_with_nothing_usable_leaves_the_review_to_stand(
        supervisor: Supervisor, runner: list[list[str]]) -> None:
    supervisor.config.policy.allow_command_execution = True
    supervisor.router.register("fake", FakeProvider())  # no native tools
    session, tester = _run(supervisor)

    await supervisor._verify_off_the_loop(session)

    (crit,) = tester.dod
    assert crit.status is CriterionStatus.WAIVED and not crit.breaks
    assert "takes no tools natively" in crit.evidence
    assert "faithfulness review alone stands" in crit.evidence


async def test_the_saboteur_is_measured_against_an_oracle_it_never_sees(
        tmp_path: Path, config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The eval role asks the saboteur as a run does; `breaks_turn_red` runs the oracle."""
    from supervisor_harness.evals import checks
    from supervisor_harness.evals.cases import parse_case
    from supervisor_harness.evals.checks import judge
    from supervisor_harness.evals.roles import Variant, run_role
    from supervisor_harness.providers.router import ModelRouter

    (tmp_path / "ws").mkdir()
    ws = _project(tmp_path / "ws")
    router = ModelRouter(config, host_name="eval")
    router.register("fake", Saboteur())
    oracle = tmp_path / "oracle.json"
    case = parse_case({
        "id": "c-saboteur", "role": "saboteur", "fixture": {"kind": "files", "files": {}},
        "request": "Build the game", "input": {
            "requirement": {"id": "R1", "quote": "add returns the sum. It returns a number."},
            "builder_paths": ["src/"], "tester_paths": ["tests/"], "command": "npm test"},
        "checks": ["answered", "breaks_kept", {"check": "breaks_turn_red",
                                               "command": "node oracle.mjs",
                                               "report": str(oracle)}]})
    out = await run_role(case, ws, tmp_path / "store", Variant(), base=config, router=router)
    assert [b["find"] for b in out.breaks] == ["add(a: number"], out.error
    assert "a test file" in out.why

    def run(argv: list[str], cwd: Path | str, timeout: float,
            extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        typed = "a: number" in (Path(cwd) / "src" / "game.ts").read_text(encoding="utf-8")
        oracle.write_text(json.dumps({"testResults": [{"assertionResults": [
            {"fullName": "the oracle checks the types", "status": "passed" if typed
             else "failed"}]}]}), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(checks, "run_bounded", run)
    results = judge(case, out, ws)
    assert results["breaks_kept"]["passed"]
    assert results["breaks_turn_red"] == {"passed": True,
                                          "detail": "clause 1: 1 red (the oracle checks the types)"}
    assert (ws / "src" / "game.ts").read_bytes() == GAME.encode("utf-8")


def test_a_testers_scope_bars_only_what_the_builders_does_not_hold(tmp_path: Path) -> None:
    """Run 11's tester held the engine in its scope, from before batch M."""
    tree = _project(tmp_path)
    brk = _break("return a + b;", "return 0;")
    assert sabotage.refusal(brk, tree, ["src/"], ["tests/", "src/game.ts"]) is None
    assert "testing task's own files" in (
        sabotage.refusal(brk, tree, ["lib/"], ["src/game.ts"]) or "")


async def test_a_verifiers_verdict_on_the_bar_is_not_applied(supervisor: Supervisor) -> None:
    """Whether the tests caught a break is read from their run, never from a model."""
    from supervisor_harness.models import AgentKind, AgentSpec

    session, tester = _run(supervisor)
    (crit,) = tester.dod
    verifier = AgentSpec(kind=AgentKind.VERIFICATION, role="verifier", task_id=tester.id)
    session.state.agents[verifier.id] = verifier

    await supervisor._report_verification(session, verifier, {"results": [
        {"criterion_id": crit.id, "status": "pass", "evidence": "tests/game.test.ts:1"}]})

    assert crit.status is CriterionStatus.UNVERIFIED
    assert any("verdict on a break criterion was not applied" in n.text
               for n in session.state.notes)


async def test_the_saboteur_is_asked_once_an_attempt_and_its_breaks_are_kept(
        supervisor: Supervisor, runner: list[list[str]]) -> None:
    """The same breaks run on every later attempt, and new ones are asked for."""
    supervisor.config.policy.allow_command_execution = True
    fake = Saboteur()
    supervisor.router.register("fake", fake)
    session, tester = _run(supervisor)
    (crit,) = tester.dod

    await supervisor._propose_breaks(session)
    await supervisor._propose_breaks(session)
    assert len(fake.requests) == 1 and len(crit.breaks) == 1

    tester.attempts = 2
    await supervisor._propose_breaks(session)
    assert len(fake.requests) == 2, "asked again on the next attempt"
    assert len(crit.breaks) == 1 and crit.breaks[0].attempt == 1, \
        "the same proposal is not kept twice; the first attempt's break still runs"
