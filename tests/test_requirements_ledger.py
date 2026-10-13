"""Batch L: every requirement of the brief, accounted for and tested.

Minesweeper run 4 verified all six tasks, and the hidden suite failed five things
-- the mine count, a flood beside a flag, the timer, the best time, the state
changing before an animation -- each a sentence of the brief, none a criterion
of the plan. The implementers' own 73 tests, written for the plan they were
given, tested none of them. See docs/autonomy-plan.md, batch L.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness.agents.brief import build_implementer_brief
from supervisor_harness.contracts import (
    REQUIREMENTS_SCHEMA,
    SYNTHESIS_SCHEMA,
    parse_tasks,
    requirement_id,
)
from supervisor_harness.core import phases, requirements
from supervisor_harness.core.requirements import (
    MAX_REQUIREMENTS,
    attach_requirements,
    build_ledger,
    coverage_problems,
    grounded,
)
from supervisor_harness.core.supervisor import LEDGER_READ, Supervisor
from supervisor_harness.core.tools import Toolbox
from supervisor_harness.models import (
    AcceptanceTests,
    AgentKind,
    AgentSpec,
    ExecutionTask,
    Requirement,
    RunMode,
    RunState,
    Scope,
    VerifyMethod,
)
from supervisor_harness.providers.base import CompletionRequest, CompletionResponse, ToolCall

from .conftest import FakeProvider
from .test_implementer_conversation import _calls, _writer
from .test_planner_conversation import Planner, _planning
from .test_send_back_criteria import PROMPT

SPEC = """\
# Login limits

1. **Ten tries.** The eleventh failed login from one address within a minute is
   refused with HTTP `429`.
2. The limit is kept per account as well as per address — a new address does not
   reset it.
"""
ELEVENTH = "The eleventh failed login from one address within a minute is refused with HTTP 429"
PER_ACCOUNT = ("The limit is kept per account as well as per address - a new address does not "
               "reset it")
BUILDER = "Add rate limiting to the login endpoint"
TESTER = "Write the acceptance tests for the login limits"


def _spec(workspace: Path) -> None:
    (workspace / "docs").mkdir(exist_ok=True)
    (workspace / "docs" / "SPEC.md").write_text(SPEC, encoding="utf-8")


def _quoted(*pairs: tuple[str, str]) -> dict[str, Any]:
    return {"requirements": [{"quote": q, "source": s} for q, s in pairs]}


def _ledger(*quotes: str) -> list[Requirement]:
    return [Requirement(id=f"R{i}", quote=q, source="docs/SPEC.md")
            for i, q in enumerate(quotes, 1)]


def _plan(fake: FakeProvider, *, deliver: list[str], test: list[str],
          **extra: Any) -> dict[str, Any]:
    """The fake's plan, its task delivering ``deliver``, and a second task testing ``test``."""
    data = copy.deepcopy(fake._synthesis(None))  # type: ignore[arg-type]
    builder = data["tasks"][0]
    builder.update(scope_paths=["src/auth/**"], depends_on=[], requirements=deliver)
    data["tasks"].append({
        "title": TESTER, "action": "Write tests/acceptance/test_limits.py from the brief",
        "motivation": "The limits are checked by someone other than their builder.",
        "rationale_refs": builder["rationale_refs"],
        "dod": [{"statement": "The tests are collected", "method": "command",
                 "command": "pytest --collect-only -q tests/acceptance", "expect": "0"}],
        "scope_paths": ["tests/acceptance/**"], "depends_on": [], "requirements": [],
        "acceptance_tests": [{"requirements": test,
                              "command": "pytest tests/acceptance/test_limits.py -q"}],
    })
    data.update(extra)
    return data


# -- grounding: no paraphrase becomes a requirement ------------------------------------------


def test_a_quote_is_found_as_written_whatever_the_marks_around_it() -> None:
    assert grounded("Ten tries. The eleventh failed login", SPEC), "emphasis left out"
    assert grounded(ELEVENTH, SPEC), "code marks and a line break left out"
    assert grounded(PER_ACCOUNT, SPEC) and grounded(PER_ACCOUNT.replace(" - ", " -- "), SPEC)
    assert grounded("the eleventh FAILED login from one address", SPEC), "any case"
    assert grounded("The eleventh failed login ... refused with HTTP 429", SPEC)


def test_a_paraphrase_a_reordering_or_a_scrap_is_not_found() -> None:
    assert not grounded("The eleventh failed attempt from one address is refused", SPEC)
    assert not grounded("refused with HTTP 429 ... The eleventh failed login", SPEC)
    assert not grounded("HTTP 429", SPEC), "too short to mean anything"
    assert not grounded("...", SPEC)


def test_the_ledger_keeps_what_it_finds_once_and_says_what_it_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _spec(workspace)
    (tmp_path / "outside.md").write_text(SPEC, encoding="utf-8")
    raw = _quoted((ELEVENTH, "docs/SPEC.md"),
                  ("a new address resets the count", "docs/SPEC.md"),
                  ("Add rate limiting to the public login endpoint", "docs/SPEC.md"),
                  ("The limit is kept per account", "../outside.md"))
    ledger, notes = build_ledger([], raw, request=PROMPT, workspace=workspace, found_by="reader")

    assert [(r.id, r.source) for r in ledger] == [("R1", "docs/SPEC.md"), ("R2", "request")]
    assert ledger[1].quote.startswith("Add rate limiting"), "found in the request instead"
    assert len(notes) == 2 and "'a new address resets the count' is not in docs/SPEC.md" in notes[0]
    assert "../outside.md" in notes[1], "a source outside the workspace is not read"

    more = _quoted(("eleventh failed login from one address", "docs/SPEC.md"),
                   (PER_ACCOUNT, "docs/SPEC.md"))
    ledger, notes = build_ledger(ledger, more, request=PROMPT, workspace=workspace,
                                 found_by="audit")
    assert [(r.id, r.found_by) for r in ledger][2:] == [("R3", "audit")], (
        "a quote inside one held is the same requirement")
    assert notes == []


def test_the_ledger_stops_at_its_size(tmp_path: Path) -> None:
    lines = [f"Rule {i}: the widget number {i} must glow." for i in range(MAX_REQUIREMENTS + 3)]
    raw = _quoted(*((line, "request") for line in lines))
    ledger, notes = build_ledger([], raw, request="\n".join(lines), workspace=tmp_path,
                                 found_by="reader")
    assert len(ledger) == MAX_REQUIREMENTS and ledger[-1].id == f"R{MAX_REQUIREMENTS}"
    assert len(notes) == 1 and f"past {MAX_REQUIREMENTS}" in notes[0]


def test_ids_are_read_as_a_model_writes_them() -> None:
    assert [requirement_id(v) for v in ("R7", "r07", "[R7]", "7", " R 12 ")] == ["R7"] * 4 + ["R12"]
    assert [requirement_id(v) for v in ("X1", "R", "", "R1a")] == [""] * 4
    assert REQUIREMENTS_SCHEMA["required"] == ["requirements"]
    task = SYNTHESIS_SCHEMA["properties"]["tasks"]["items"]["properties"]
    assert {"requirements", "acceptance_tests"} <= set(task)
    assert {"set_aside", "not_tested"} <= set(SYNTHESIS_SCHEMA["properties"])


def test_a_tasks_acceptance_tests_are_its_criteria(fake: FakeProvider) -> None:
    data = _plan(fake, deliver=["r1"], test=["R1", "2"])
    builder, tester = parse_tasks(data, "run_X")

    assert builder.requirements == ["R1"] and builder.acceptance == []
    assert tester.acceptance == [AcceptanceTests(["R1", "R2"],
                                                 "pytest tests/acceptance/test_limits.py -q")]
    (crit,) = [c for c in tester.dod if c.statement.startswith("The acceptance tests")]
    assert (crit.method, crit.command, crit.expect, crit.mandatory) == (
        VerifyMethod.TEST, "pytest tests/acceptance/test_limits.py -q", "0", True)


# -- the plan accounts for every requirement -----------------------------------------------


def test_what_the_plan_leaves_unaccounted_goes_back_by_id(fake: FakeProvider) -> None:
    ledger = _ledger(ELEVENTH, PER_ACCOUNT, "a third thing someone could check")

    data = _plan(fake, deliver=["R1"], test=["R1"])
    lines = coverage_problems(ledger, data, parse_tasks(data, "run_X"))
    assert any("no task delivers R2, R3" in line for line in lines)

    data = _plan(fake, deliver=["R1", "R2"], test=["R1"],
                 set_aside=[{"requirement": "R3", "reason": "the owner's call"}])
    (line,) = coverage_problems(ledger, data, parse_tasks(data, "run_X"))
    assert "no task tests R2" in line and "a task other than the one that delivers it" in line

    data = _plan(fake, deliver=["R1", "R2"], test=["R1"],
                 set_aside=[{"requirement": "R3", "reason": "the owner's call"}],
                 not_tested=[{"requirement": "R2", "reason": "the brief says it is visual"}])
    assert coverage_problems(ledger, data, parse_tasks(data, "run_X")) == []
    assert coverage_problems([], data, parse_tasks(data, "run_X")) == [], "no ledger, no rule"
    assert coverage_problems(ledger, data, []) == [], "a report proposes no work"


def test_a_builder_cannot_test_itself_nor_be_tested_before_it_is_built(
    fake: FakeProvider,
) -> None:
    ledger = _ledger(ELEVENTH)
    data = _plan(fake, deliver=["R1"], test=["R1"])
    data["tasks"][1]["requirements"] = ["R1"]
    data["tasks"][0]["depends_on"] = [TESTER]
    data["tasks"][1]["acceptance_tests"].append({"requirements": ["R9"], "command": ""})
    lines = coverage_problems(ledger, data, parse_tasks(data, "run_X"))

    assert any(f"task {TESTER!r} tests R1, which it also delivers" in line for line in lines)
    assert any(f"but {BUILDER!r}, which delivers it, depends on it" in line for line in lines)
    assert any("for R9 name no command" in line for line in lines)
    assert any("R9 is not on the list of requirements" in line for line in lines)


def test_a_reason_is_required_to_leave_one_out(fake: FakeProvider) -> None:
    data = _plan(fake, deliver=[], test=[],
                 set_aside=[{"requirement": "R1", "reason": ""}])
    (line,) = coverage_problems(_ledger(ELEVENTH), data, parse_tasks(data, "run_X"))
    assert "R1 is set aside or not tested without a reason" in line


# -- the checks the plan's account becomes --------------------------------------------------


def _attached(fake: FakeProvider, **plan: Any) -> tuple[list[ExecutionTask], list[Requirement],
                                                         dict[str, list[str]]]:
    data = _plan(fake, **plan)
    tasks = parse_tasks(data, "run_X")
    phases.resolve_dependencies(tasks)
    ledger, notes = attach_requirements(tasks, _ledger(ELEVENTH, PER_ACCOUNT), data)
    return tasks, ledger, notes


def test_the_builder_is_reviewed_against_its_quotes(fake: FakeProvider) -> None:
    (builder, _), ledger, _ = _attached(
        fake, deliver=["R1", "R2"], test=["R1"],
        not_tested=[{"requirement": "R2", "reason": "visual"}])

    (review,) = [c for c in builder.dod if c.statement.startswith("Delivers")]
    assert review.statement == "Delivers R1, R2 as the request states them"
    assert review.method is VerifyMethod.REVIEW and review.mandatory
    assert f"- R1: {ELEVENTH}" in review.rubric and f"- R2: {PER_ACCOUNT}" in review.rubric
    assert [(r.id, r.not_tested) for r in ledger] == [("R1", ""), ("R2", "visual")]


def test_the_tester_marks_runs_and_is_judged_on_each_test(fake: FakeProvider) -> None:
    (builder, tester), _, notes = _attached(fake, deliver=["R1", "R2"], test=["R1", "R2"])

    marks = [c for c in tester.dod if c.method is VerifyMethod.INSPECTION]
    assert [c.expect for c in marks] == ["tests/acceptance: [R1]", "tests/acceptance: [R2]"]
    (faithful,) = [c for c in tester.dod if c.statement.endswith("what each requirement says")]
    assert faithful.method is VerifyMethod.REVIEW and f"- R2: {PER_ACCOUNT}" in faithful.rubric
    assert any(c.command == "pytest tests/acceptance/test_limits.py -q" for c in tester.dod)

    assert tester.depends_on == [builder.id], "the tests come after the work they test"
    assert builder.scope.forbidden_paths == ["tests/acceptance/**"]
    assert any("may not change 'tests/acceptance/**'" in n for n in notes[builder.id])
    assert any("the tests come after" in n for n in notes[tester.id])


def test_a_builder_inside_the_testers_ground_is_said_not_fenced(fake: FakeProvider) -> None:
    data = _plan(fake, deliver=["R1"], test=["R1"])
    data["tasks"][0]["scope_paths"] = ["tests/acceptance/helpers.py"]
    data["tasks"][0]["depends_on"] = [TESTER]
    tasks = parse_tasks(data, "run_X")
    phases.resolve_dependencies(tasks)
    _, notes = attach_requirements(tasks, _ledger(ELEVENTH), data)
    builder, tester = tasks

    assert builder.scope.forbidden_paths == [], "fencing it off would leave it nothing"
    assert any("not fenced off" in n for n in notes[builder.id])
    assert tester.depends_on == [], "a cycle is not made"
    assert any("may be written before the work" in n for n in notes[tester.id])


@pytest.mark.parametrize(("paths", "root"), [
    (["tests/acceptance/**"], "tests/acceptance"),
    (["e2e/*.spec.ts"], "e2e"),
    (["e2e/play.spec.ts"], "e2e/play.spec.ts"),
    (["e2e/a.spec.ts", "e2e/b.spec.ts"], "e2e"),
    (["tests/unit/**", "e2e/**"], "."),
    ([], "."),
])
def test_where_a_testers_marks_are_looked_for(paths: list[str], root: str) -> None:
    assert requirements._tests_root(paths) == root


def test_the_builder_and_the_tester_are_told_their_parts(fake: FakeProvider) -> None:
    (builder, tester), ledger, _ = _attached(fake, deliver=["R1", "R2"], test=["R1", "R2"])
    run = RunState(id="run_X", prompt=PROMPT, requirements=ledger,
                   tasks={builder.id: builder, tester.id: tester})

    def brief(task: ExecutionTask) -> str:
        agent = AgentSpec(id="agt_x", kind=AgentKind.EXECUTION, task_id=task.id,
                          scope=task.scope)
        return build_implementer_brief(run, agent, task)

    mine = brief(builder)
    assert "## Requirements" in mine and f"- R1: {ELEVENTH}" in mine
    assert f"{TESTER!r} then tests them" in mine and "Never change: `tests/acceptance/**`" in mine
    theirs = brief(tester)
    assert "Write each test from the words quoted, not from the code" in theirs
    assert "`[R3] ...`" in theirs and f"- R2: {PER_ACCOUNT}" in theirs
    assert "Never weaken a test" in theirs


# -- in a run --------------------------------------------------------------------------------


def _readings(fake: FakeProvider) -> None:
    fake.script("requirements",
                _quoted((ELEVENTH, "docs/SPEC.md"), ("a new address resets it", "docs/SPEC.md")),
                _quoted((PER_ACCOUNT, "docs/SPEC.md")))


async def test_a_run_reads_holds_and_accounts_for_its_requirements(
    supervisor: Supervisor, fake: FakeProvider, workspace: Path,
) -> None:
    _spec(workspace)
    _readings(fake)
    fake.script("synthesis",
                _plan(fake, deliver=["R1"], test=["R1"]),           # leaves R2 out
                _plan(fake, deliver=["R1", "R2"], test=["R1"],
                      not_tested=[{"requirement": "R2", "reason": "needs two addresses"}]))
    response = await supervisor.run(PROMPT, mode=RunMode.EXECUTE)
    store = supervisor.store
    (store.runs_dir / response.run_id / "state.json").unlink()
    state = store.load_state(response.run_id)

    assert [(r.id, r.found_by, r.not_tested) for r in state.requirements] == [
        ("R1", "reader", ""), ("R2", "audit", "needs two addresses")], "as the plan left it"
    (read,) = [n for n in state.notes if n.text == LEDGER_READ]
    assert (read.context["requirements"], read.context["audit"]) == ("2", "1")
    assert "is not in docs/SPEC.md" in read.context["refused"]

    sent = next(n for n in state.notes if "synthesis sent back once" in n.text)
    assert "no task delivers R2" in sent.context["weak"]
    builder, tester = sorted(state.tasks.values(), key=lambda t: t.title)
    assert builder.requirements == ["R1", "R2"]
    assert "tests/acceptance/**" in builder.scope.forbidden_paths, "replayed from the log"
    assert builder.id in tester.depends_on

    synthesis = [c for c in fake.calls if c["stage"] == "synthesis"]
    assert len(synthesis) == 2
    report = phases.final_report_markdown(state)
    assert "## Requirements" in report and f"**R1** (docs/SPEC.md): {ELEVENTH}" in report
    assert f"delivered by: {BUILDER}" in report and f"tested by: {TESTER}" in report


def test_the_report_marks_what_nobody_accounted_for() -> None:
    task = ExecutionTask(title=BUILDER, requirements=["R1"])
    ledger = _ledger(ELEVENTH, PER_ACCOUNT, "a third thing someone could check")
    ledger[2].set_aside = "the owner's call"
    lines = requirements.report_lines(ledger, {task.id: task})

    at = {line.split("**")[1]: i for i, line in enumerate(lines) if line.startswith("- **")}
    assert lines[at["R1"] + 1] == f"    - delivered by: {BUILDER} (proposed)"
    assert lines[at["R2"] + 1] == "    - **not accounted for**"
    assert lines[at["R3"] + 1] == "    - set aside: the owner's call"
    assert requirements.report_lines([], {}) == []


async def test_the_planner_is_shown_the_ledger(
    supervisor: Supervisor, fake: FakeProvider, workspace: Path,
) -> None:
    seen: list[str] = []

    def plan(request: CompletionRequest) -> dict[str, Any]:
        seen.append(request.messages[0].content)
        return _plan(fake, deliver=["R1"], test=["R1"])

    _spec(workspace)
    fake.script("requirements", _quoted((ELEVENTH, "docs/SPEC.md")), _quoted())
    fake.script("synthesis", plan)
    await supervisor.run(PROMPT, mode=RunMode.EXECUTE)

    assert "# Requirements" in seen[0] and f"- R1: {ELEVENTH}" in seen[0]
    assert "`acceptance_tests`" in seen[0] and "`set_aside`" in seen[0]


async def test_no_reading_when_turned_off_or_only_reporting(
    supervisor: Supervisor, fake: FakeProvider,
) -> None:
    supervisor.config.policy.requirements_ledger = False
    await supervisor.run(PROMPT, mode=RunMode.EXECUTE)
    supervisor.config.policy.requirements_ledger = True
    await supervisor.run(PROMPT, mode=RunMode.REPORT)
    assert not [c for c in fake.calls if c["stage"] == "requirements"]


async def test_the_reading_happens_once_per_run(
    supervisor: Supervisor, fake: FakeProvider,
) -> None:
    session = supervisor.store.create(RunState(id="run_O", prompt=PROMPT, mode=RunMode.EXECUTE))
    assert await supervisor._read_requirements(session) is False, "nothing found by the fake"
    assert await supervisor._read_requirements(session) is False
    assert len([c for c in fake.calls if c["stage"] == "requirements"]) == 2, (
        "two readings the first time, none the second")


async def test_a_failed_reading_leaves_the_plan_to_go_on(
    supervisor: Supervisor, fake: FakeProvider,
) -> None:
    def boom(_request: CompletionRequest) -> dict[str, Any]:
        raise RuntimeError("model unavailable")

    fake.script("requirements", boom, boom)
    response = await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = supervisor.store.load_state(response.run_id)
    assert state.requirements == [] and state.tasks
    assert any("a requirements reading failed" in n.text for n in state.notes)


class Reader(Planner):
    """Reads the brief, then lists one requirement; the second reading finds nothing."""

    def __init__(self) -> None:
        super().__init__()
        self.readings: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        names = {t["name"] for t in request.tools or []}
        if "list_requirements" not in names:
            return await super().complete(request)
        self.readings.append(request)
        if "# The list so far" in request.messages[0].content:
            return _calls(ToolCall("list_requirements", _quoted()))(request)
        if not any(m.role == "tool" for m in request.messages):
            return _calls(ToolCall("read_file", {"path": "docs/SPEC.md"}))(request)
        return _calls(ToolCall("list_requirements",
                               _quoted((ELEVENTH, "docs/SPEC.md"))))(request)


async def test_the_reader_opens_the_brief_in_a_conversation(
    supervisor: Supervisor, workspace: Path,
) -> None:
    _spec(workspace)
    fake = Reader()
    response = await _planning(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE)
    state = supervisor.store.load_state(response.run_id)

    first = fake.readings[0]
    names = {t["name"] for t in first.tools or []}
    assert "list_requirements" in names and not names & {"edit_file", "write_file",
                                                         "run_command", "report", "message"}
    assert first.system == requirements.READER_SYSTEM
    assert fake.readings[-1].system == requirements.AUDIT_SYSTEM
    assert f"R1 (docs/SPEC.md): {ELEVENTH}" in fake.readings[-1].messages[0].content
    assert [r.quote for r in state.requirements] == [ELEVENTH]
    assert any(n.text == "requirements read in a conversation" for n in state.notes)
    # The fake planner's plan accounts for nothing: sent back in its conversation.
    assert any("no task delivers R1" in m.content
               for m in fake.planner[-1].messages if m.role == "user")


# -- the builder cannot rewrite its test ------------------------------------------------------


def test_the_tests_of_its_work_are_beyond_a_builders_reach(supervisor: Supervisor) -> None:
    (supervisor.workspace / "tests" / "acceptance").mkdir(parents=True)
    test = supervisor.workspace / "tests" / "acceptance" / "test_limits.py"
    test.write_text("def test_r1(): ...\n", encoding="utf-8")
    session, me = _writer(supervisor, "run_F")
    me.scope = Scope(paths=["src/auth/**"], forbidden_paths=["tests/acceptance/**"])
    box = Toolbox(supervisor.workspace, supervisor.config.policy)

    blocked = {"status": "blocked", "summary": "the test is wrong",
               "blocked_on": "tests/acceptance/test_limits.py:1 expects the wrong code"}
    assert supervisor._blocked_within_reach(session.state, me, blocked, box) == []

    import asyncio
    asyncio.run(supervisor._widen_for_write(session, me, box, "tests/acceptance/test_limits.py"))
    assert me.scope.paths == ["src/auth/**"] and not any(
        "scope widened" in n.text for n in session.state.notes)
    result = box.call("write_file", {"path": "tests/acceptance/test_limits.py", "content": "x"},
                      me, whole_files=True)
    assert not result.ok and "forbidden" in result.output
    assert test.read_text(encoding="utf-8") == "def test_r1(): ...\n"
