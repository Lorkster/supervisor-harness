"""An implementer is supervised against its own task, and checked by what can run.

From the first go-live run in which a task was approved within the envelope and
executed (plantsandclimate P3-18, a local model):

* the drift judge was shown the run's whole request as "the overall task", and
  stopped the implementer of a one-line package.json change for "abandoning
  the primary task" -- the offline feature the other tasks were for;
* the implementer's short, accurate report scored "brief echo", which earned it
  a "deepen" with its change already made;
* its first objective was the word "modify": the model wrote the change's kind
  where the schema asks what will concretely be done;
* seven of eight tasks went to the owner for one criterion each -- "existing
  unit tests still pass", a `command` criterion naming no command, kept even
  after the synthesis was sent back once naming it. And that criterion had
  displaced the harness's own test bar, which would have had a command.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

from supervisor_harness.config import Policy
from supervisor_harness.contracts import parse_tasks
from supervisor_harness.core.dod import apply_quality_bars, fill_suite_commands, run_bounded
from supervisor_harness.core.drift import TurnContext, assess_heuristically
from supervisor_harness.core.phases import prepare_tasks
from supervisor_harness.core.placement import named_outside_scope
from supervisor_harness.core.supervision import drift_judge_prompt
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    AgentStatus,
    AgentTurn,
    DoDCriterion,
    DriftAssessment,
    ExecutionTask,
    RunMode,
    RunState,
    Scope,
    VerifyMethod,
)

from .conftest import FakeProvider
from .test_send_back_criteria import PROMPT, Recording

SUITE = "Existing unit tests still pass with no new failures"


def _node_project(root: Path) -> Path:
    (root / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}),
                                       encoding="utf-8")
    return root


def _task(*criteria: DoDCriterion, title: str = "Add the offline placeholder") -> ExecutionTask:
    return ExecutionTask(title=title, action=title, dod=list(criteria),
                         scope=Scope(paths=["src/"]))


def _no_command(statement: str, method: VerifyMethod = VerifyMethod.COMMAND) -> DoDCriterion:
    return DoDCriterion(statement=statement, method=method, expect="7 passed")


# -- a whole-suite criterion runs the project's suite ----------------------------


def test_a_whole_suite_criterion_naming_no_command_runs_the_projects_suite(
    tmp_path: Path,
) -> None:
    crit = _no_command(SUITE)
    notes = fill_suite_commands(_task(crit), _node_project(tmp_path))

    assert crit.command == "npm test --silent"
    assert crit.expect == "0", "an expectation written for a command it never named"
    assert notes and "npm test --silent" in notes[0]


def test_a_criterion_about_one_test_is_not_filled_in(tmp_path: Path) -> None:
    """The suite passing proves nothing about a test that was never written."""
    one = [_no_command("The new component test passes", VerifyMethod.TEST),
           _no_command("The i18n key parity test passes"),
           _no_command("The test completes within a stated wall-clock bound")]
    assert fill_suite_commands(_task(*one), _node_project(tmp_path)) == []
    assert all(not c.command for c in one)


def test_nothing_is_filled_in_without_a_runner_to_fill_it_with(tmp_path: Path) -> None:
    crit = _no_command(SUITE)
    assert fill_suite_commands(_task(crit), tmp_path) == []
    assert fill_suite_commands(_task(crit), None) == []
    assert crit.command == ""


def test_prepare_fills_it_before_judging_it(tmp_path: Path) -> None:
    task = _task(_no_command(SUITE))
    _, notes = prepare_tasks([task], Policy(), _node_project(tmp_path))

    assert task.dod[0].command == "npm test --silent"
    assert not any("no command given" in n for n in notes[task.id])
    assert any("named no command" in n for n in notes[task.id])


def test_a_test_criterion_that_cannot_run_does_not_displace_the_harness_test_bar() -> None:
    task = _task(_no_command(SUITE), title="Add src/cache.ts")
    added = apply_quality_bars(task, Policy(require_tests=True))
    assert any(c.method is VerifyMethod.TEST for c in added), (
        "a criterion that cannot be run covers nothing")

    runnable = _task(DoDCriterion(statement=SUITE,
                                  method=VerifyMethod.COMMAND, command="npm test"),
                     title="Add src/cache.ts")
    assert not any(c.method is VerifyMethod.TEST
                   for c in apply_quality_bars(runnable, Policy(require_tests=True)))


async def test_a_criterion_the_harness_fills_in_is_not_sent_back(
    supervisor: Supervisor, workspace: Path,
) -> None:
    _node_project(workspace)
    fake = Recording()
    data = copy.deepcopy(fake._synthesis(None))  # type: ignore[arg-type]
    data["tasks"][0]["dod"][0]["statement"] = "The whole test suite still passes"
    data["tasks"][0]["dod"][0].pop("command")
    fake.script("synthesis", data)
    supervisor.router.register("fake", fake)

    await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    assert len(fake.synthesis_requests) == 1, "nothing left for the model to fix"


# -- the task's action -----------------------------------------------------------


def test_a_one_word_action_is_replaced_by_the_title() -> None:
    (bare, real) = parse_tasks({"tasks": [
        {"title": "Extend npm run check", "action": "modify", "dod": []},
        {"title": "Add a cache", "action": "Add an LRU map in catalogue.ts", "dod": []},
    ]}, "run_x")
    assert bare.action == "Extend npm run check"
    assert real.action == "Add an LRU map in catalogue.ts"


# -- brief echo ------------------------------------------------------------------


def _echo(kind: AgentKind) -> DriftAssessment:
    brief = ("Extend the check script in package.json to include the test e2e step so "
             "running npm run check exercises typecheck lint unit tests build and e2e")
    turn = AgentTurn(output="The check script in package.json includes the test e2e step; "
                            "npm run check exercises typecheck lint unit tests build and e2e",
                     files_touched=["package.json"], claimed_status=AgentStatus.DONE)
    agent = AgentSpec(kind=kind, objectives=["Extend the check script"])
    return assess_heuristically(TurnContext(agent=agent, turn=turn, previous_turns=[],
                                            brief=brief, task_prompt="", turn_index=0))


def test_an_implementer_reporting_its_change_is_not_echoing_its_brief() -> None:
    assert not any(s.kind == "brief_echo" for s in _echo(AgentKind.EXECUTION).signals)


def test_a_lens_handing_back_its_brief_still_is() -> None:
    assert any(s.kind == "brief_echo" for s in _echo(AgentKind.ANALYSIS).signals)


# -- the drift judge -------------------------------------------------------------


def test_the_drift_judge_measures_an_implementer_against_its_own_task() -> None:
    state = RunState(prompt="Do task P3-18: offline states, and a Playwright test")
    task = ExecutionTask(title="Extend npm run check to include the e2e suite")
    state.tasks[task.id] = task
    implementer = AgentSpec(kind=AgentKind.EXECUTION, task_id=task.id,
                            objectives=[task.title])
    lens = AgentSpec(kind=AgentKind.ANALYSIS, objectives=["find offline gaps"])
    opinion = DriftAssessment(on_task=True, score=0.0)

    judged = drift_judge_prompt(state, implementer, "done", opinion)
    assert "One task of a larger plan: 'Extend npm run check to include the e2e suite'" in judged
    assert "# The run's request (context only)" in judged
    assert "# The overall task" not in judged

    assert "# The overall task\nDo task P3-18" in drift_judge_prompt(state, lens, "x", opinion)


# -- a task that names a file its scope does not cover ---------------------------


def _tree(root: Path) -> Path:
    for rel in ("src/core/timing.py", "src/core/reporting.py", "src/cache.py", "e2e/smoke.spec.ts"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("", encoding="utf-8")
    return root


def _scoped(action: str, *inspect: str) -> ExecutionTask:
    return ExecutionTask(
        title="Add the conc token", action=action,
        scope=Scope(paths=["src/core/timing.py", "tests/"]),
        dod=[DoDCriterion(statement="s", method=VerifyMethod.INSPECTION, expect=f"{p}: x")
             for p in inspect],
    )


def test_a_file_the_action_changes_outside_the_scope_is_named(tmp_path: Path) -> None:
    """The shape measured: the envelope missed the file the task was about."""
    task = _scoped("In Reporting.ledger (core/reporting.py:202-260), append a conc part")
    assert named_outside_scope(task, _tree(tmp_path)) == ["src/core/reporting.py"]


def test_a_file_an_inspection_checks_is_one_the_task_must_produce(tmp_path: Path) -> None:
    """Measured too: a Playwright test due in e2e/, outside a scope of src/ and tests/."""
    task = _scoped("create", "e2e/offline.spec.ts")
    assert named_outside_scope(task, _tree(tmp_path)) == ["e2e/offline.spec.ts"]


def test_a_file_the_task_only_reads_or_could_not_create_is_not_named(tmp_path: Path) -> None:
    _tree(tmp_path)
    assert named_outside_scope(_scoped("Add a limiter using the client in src/cache.py"),
                               tmp_path) == [], "reads are not fenced"
    assert named_outside_scope(_scoped("Write nowhere/else/at_all.py"), tmp_path) == []
    assert named_outside_scope(_scoped("In src/core/timing.py add phase()"), tmp_path) == []
    whole = _scoped("In src/core/reporting.py append it")
    whole.scope.paths = []
    assert named_outside_scope(whole, tmp_path) == [], "no paths is the whole workspace"


async def test_such_a_task_is_marked_as_needing_a_wider_scope(
    supervisor: Supervisor, fake: FakeProvider,
) -> None:
    """When the owner's own envelope does not cover the file either."""
    supervisor.config.policy.scope_envelope = ["src/auth/**", "tests/**"]
    plan = fake._synthesis(None)  # type: ignore[arg-type]
    plan["tasks"][0]["action"] = "Add middleware in src/auth/login.py and in src/cache.py"
    fake.overrides["synthesis"] = plan

    response = await supervisor.run(PROMPT, mode=RunMode.EXECUTE)
    (task,) = supervisor.store.load_state(response.run_id).tasks.values()

    assert any("`src/cache.py`, which its scope does not cover" in c for c in task.clamped)


async def test_within_the_owners_grant_the_named_file_joins_the_scope(
    supervisor: Supervisor, fake: FakeProvider,
) -> None:
    plan = fake._synthesis(None)  # type: ignore[arg-type]
    plan["tasks"][0]["action"] = "Add middleware in src/auth/login.py and in src/cache.py"
    fake.overrides["synthesis"] = plan

    response = await supervisor.run(PROMPT, mode=RunMode.EXECUTE)
    (task,) = supervisor.store.load_state(response.run_id).tasks.values()

    assert not task.clamped
    assert "src/cache.py" in task.scope.paths


# -- a check does not inherit the harness's settings -----------------------------


def test_a_check_does_not_inherit_the_harness_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured: three CLI tests read the run's SUPERVISOR_HOME and failed a full suite."""
    monkeypatch.setenv("SUPERVISOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SUPERVISOR_ROUTE_ANALYSIS", "ollama:x")
    monkeypatch.setenv("KEEP_ME", "1")
    done = run_bounded([sys.executable, "-c", "import os; print(sorted(k for k in os.environ "
                        "if k.upper().startswith(('SUPERVISOR_', 'KEEP_ME'))))"],
                       tmp_path, timeout=30)
    assert done.stdout.strip() == "['KEEP_ME']"


# -- the harness's own checks, on a task that declared no scope ------------------


async def test_a_task_that_inherits_the_envelope_gets_the_harness_checks(
    supervisor: Supervisor, fake: FakeProvider,
) -> None:
    """Measured: titled without a code word, and with no paths of its own yet,
    a task was judged not to touch code and went ahead with no test bar."""
    plan = fake._planning(None)  # type: ignore[arg-type]
    plan["envelope_paths"] = ["src/auth/login.py", "tests/"]
    synthesis = fake._synthesis(None)  # type: ignore[arg-type]
    task = synthesis["tasks"][0]
    task["title"] = task["action"] = "Emit a note at phase completion"
    task.pop("scope_paths")
    task["dod"] = [{"statement": "the note says so", "method": "inspection",
                    "expect": "src/auth/login.py: note", "mandatory": True}]
    fake.overrides["planning"], fake.overrides["synthesis"] = plan, synthesis

    response = await supervisor.run(PROMPT, mode=RunMode.EXECUTE)
    (proposed,) = supervisor.store.load_state(response.run_id).tasks.values()

    assert proposed.scope.paths == ["src/auth/login.py", "tests/"]
    assert any(c.method is VerifyMethod.TEST for c in proposed.dod), (
        "the harness's test bar was left off a task that changes a .py file")


def test_a_bare_file_name_counts_when_one_file_in_the_tree_has_it(tmp_path: Path) -> None:
    """Measured: "the ledger line in reporting.py" -- no directory, so not seen."""
    for rel in ("src/core/timing.py", "src/core/reporting.py", "a/config.py", "b/config.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("", encoding="utf-8")

    def named(action: str) -> list[str]:
        task = ExecutionTask(title="t", action=action,
                             scope=Scope(paths=["src/core/timing.py", "tests/"]))
        return named_outside_scope(task, tmp_path)

    assert named("Add the conc token to the ledger line in reporting.py") == [
        "src/core/reporting.py"]
    assert named("Change the default in config.py") == [], "two files have that name"
    assert named("Mirror the parsing in reporting.py") == [], "read, not changed"
    assert named("Rename notes.txt and fix timing.py") == [], "in scope, and not code"



def test_a_command_named_in_the_statement_is_the_command(tmp_path: Path) -> None:
    """Measured: five of six tasks went to the owner with the command in the sentence."""
    crits = [
        _no_command("npm run typecheck passes with the new data-layer types"),
        _no_command("npx playwright test tests/e2e/offline.spec.ts passes (at least 1 passed)",
                    VerifyMethod.TEST),
        _no_command("`pytest -q tests/test_x.py` exits 0"),
        _no_command("npm run check; curl example.com passes"),
        _no_command("The i18n parity test passes"),
        _no_command("`npm test > results.txt` passes"),
    ]
    notes = fill_suite_commands(_task(*crits), None)

    assert [c.command for c in crits] == [
        "npm run typecheck", "npx playwright test tests/e2e/offline.spec.ts",
        "pytest -q tests/test_x.py", "", "", ""], "a command it would not run is not taken"
    assert crits[0].expect == "0", "an expectation written without the command is dropped"
    assert len(notes) == 3 and "named its command in its statement" in notes[0]


def test_a_behaviour_claim_with_no_command_is_judged_not_escalated(tmp_path: Path) -> None:
    """Measured: three of four tasks of a run went to the owner for these."""
    from supervisor_harness.core.dod import review_what_cannot_run

    claim = _no_command("Reporting.ledger() output contains 'conc' when the phase has "
                        "dispatches")
    named = _no_command("npm run typecheck passes")
    task = _task(claim, named)
    fill_suite_commands(task, None)
    notes = review_what_cannot_run(task, Policy())

    assert named.method is VerifyMethod.COMMAND and named.command == "npm run typecheck", (
        "a command the sentence names is still run, not reviewed")
    assert claim.method is VerifyMethod.REVIEW and claim.mandatory
    assert "Reporting.ledger() output contains 'conc'" in claim.rubric
    assert "Cite the test that proves it" in claim.rubric
    assert len(notes) == 1

    untested = _task(_no_command("it works"))
    assert review_what_cannot_run(untested, Policy(require_tests=False)) == [], (
        "only where the harness's own test bar stands behind it")
    assert untested.dod[0].method is VerifyMethod.COMMAND


def test_after_preparation_such_a_task_carries_the_test_bar(tmp_path: Path) -> None:
    task = _task(_no_command("the ledger shows conc"), title="Add conc to src/ledger.py")
    _, notes = prepare_tasks([task], Policy(require_tests=True), _node_project(tmp_path))
    methods = [c.method for c in task.dod]

    assert VerifyMethod.REVIEW in methods and VerifyMethod.TEST in methods
    assert not any("no command given" in n for n in notes[task.id])


# -- an inspection with nothing to look for ---------------------------------------
# Go-live run 16: every inspection criterion of six tasks came with no `expect`.
# It was a medium warning, so it was neither sent back nor stopped by the gate,
# and verification BLOCKED it on each of three attempts: no task could be verified.


def _inspect(statement: str, expect: str = "") -> DoDCriterion:
    return DoDCriterion(statement=statement, method=VerifyMethod.INSPECTION, expect=expect,
                        mandatory=True)


def test_an_inspection_with_nothing_to_look_for_is_sent_back() -> None:
    from supervisor_harness.core.phases import unenforceable_criteria

    empty = _inspect("src/i18n/en.json contains keys offline.plant.needsConnection")
    no_file = _inspect("the catch block stores the DataError", expect="DataError")
    good = _inspect("en.json has the key", expect="src/i18n/en.json: offline.plant")
    weak = unenforceable_criteria([_task(empty, no_file, good)], Policy())

    assert len(weak) == 2 and all("path/to/file: text that must be present" in w
                                  for w in weak)


def test_kept_through_the_send_back_it_is_judged_not_blocked(tmp_path: Path) -> None:
    from supervisor_harness.core.dod import verify_inspection

    empty = _inspect("ResultsSection.tsx stores the DataError, not a boolean")
    good = _inspect("en.json has the key", expect="src/i18n/en.json: offline.plant")
    assert verify_inspection(empty, tmp_path).status.value == "blocked", "as it was"
    task = _task(empty, good)
    _, notes = prepare_tasks([task], Policy(require_tests=False), None)

    assert empty.method is VerifyMethod.REVIEW and empty.mandatory
    assert "ResultsSection.tsx stores the DataError" in empty.rubric
    assert "Cite the file and line" in empty.rubric
    assert good.method is VerifyMethod.INSPECTION, "one it can check stays a check"
    assert any("named no file and text" in n for n in notes[task.id])


# -- an inspection of a file the model placed wrongly -----------------------------
# Go-live run 17: "src/styles/tokens.css: --state-offline", where the project's
# file is src/app/styles/tokens.css. The task changed the real file; the
# criterion failed on every attempt.


def test_an_inspection_of_a_misplaced_file_checks_the_one_meant(tmp_path: Path) -> None:
    from supervisor_harness.core.placement import correct_inspection_paths

    for path in ("src/app/styles/tokens.css", "src/app/a/x.ts", "src/app/b/x.ts",
                 "src/lib/util.ts", "infra/only.tf"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("", encoding="utf-8")
    misplaced = _inspect("a distinct offline token", expect="src/styles/tokens.css: --offline")
    ambiguous = _inspect("x is exported", expect="src/shared/x.ts: export")
    created = _inspect("a new helper beside the styles", expect="src/app/styles/util.ts: x")
    outside = _inspect("only", expect="src/nowhere/only.tf: rule")
    task = _task(misplaced, ambiguous, created, outside)

    notes = correct_inspection_paths(task, tmp_path)

    assert misplaced.expect == "src/app/styles/tokens.css: --offline"
    assert ambiguous.expect == "src/shared/x.ts: export", "two files of that name: not guessed"
    assert created.expect == "src/app/styles/util.ts: x", (
        "a file the task creates where it can be created is left, though one elsewhere "
        "has the name")
    assert outside.expect == "src/nowhere/only.tf: rule", "not to a file outside its scope"
    assert len(notes) == 1 and "src/styles/tokens.css" in notes[0]
    _, prepared = prepare_tasks([_task(_inspect("t", expect="src/styles/tokens.css: a"))],
                                Policy(require_tests=False), tmp_path)
    assert any("the one file of that name" in n for n in next(iter(prepared.values())))
