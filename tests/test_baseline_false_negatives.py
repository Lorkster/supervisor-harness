"""Work that passed the project's own checks, failed by the harness.

A five-run baseline on main (two tasks, a local model) was accepted by the
projects' own acceptance commands in four runs of five, while the harness
verified 15 of 23 tasks. The gap had four causes, each in the harness:

1. the liveness rubric asks for a timed test run, and the verifier -- a reader
   -- was never shown the runs the harness had made;
2. an inspection failed whenever the planner's guess at the wording was not in
   the file, whether or not the statement held;
3. ``policy.command_timeout_seconds`` never reached the harness's own checks,
   which always stopped at 300 seconds -- under a full suite that took 330;
4. a file the run's own check named when it failed -- a document whose line
   citations the change had moved -- was outside the plan's envelope, and three
   agents escalated for it one after another.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from supervisor_harness.agents.brief import build_verifier_brief
from supervisor_harness.config import Policy
from supervisor_harness.core.dod import (
    apply_quality_bars,
    quotes_the_file,
    verify_criterion,
    verify_inspection,
)
from supervisor_harness.core.supervisor import Supervisor, _files_named, _is_a_check
from supervisor_harness.core.tools import Toolbox, ToolResult
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    AgentStatus,
    CriterionStatus,
    DoDCriterion,
    EnvelopeGrant,
    ExecutionTask,
    RunMode,
    RunState,
    Scope,
    ScopeEnvelope,
    VerifyMethod,
)
from supervisor_harness.providers.base import ToolCall

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"


# -- 1. the verifier sees what the harness ran ------------------------------------------


def test_the_verifier_is_shown_the_runs_the_harness_made() -> None:
    """Baseline runs 3 and 5: "every wait is bounded", failed for want of a timed run."""
    ran = DoDCriterion(statement="the offline e2e test passes", method=VerifyMethod.TEST,
                       command="npx playwright test e2e/offline.spec.ts",
                       status=CriterionStatus.PASS,
                       evidence="$ npx playwright test e2e/offline.spec.ts\nexit=0\n"
                                "  1 passed (9.1s)")
    open_review = DoDCriterion(statement="No wait can hang", method=VerifyMethod.REVIEW)
    task = ExecutionTask(title="Offline test", dod=[ran, open_review])

    brief = build_verifier_brief(RunState(prompt="p"), task)

    runs = brief.split("What the harness already ran", 1)[1]
    assert "1 passed (9.1s)" in runs and "[pass] **the offline e2e test passes**" in runs
    assert "Criteria to judge" in brief and "No wait can hang" in brief


def test_the_liveness_rubric_accepts_a_run_the_harness_recorded() -> None:
    task = ExecutionTask(title="Retry the upload with backoff",
                         action="Add a retry loop to src/upload.py",
                         scope=Scope(paths=["src/upload.py"]))
    apply_quality_bars(task, Policy())
    rubric = next(c.rubric for c in task.dod if c.statement.startswith("No wait"))

    assert "one the harness recorded" in rubric
    assert "Reading the code alone is not a demonstration" in rubric


# -- 2. a guessed wording is the verifier's to judge, with a quote ------------------------


def test_a_missing_wording_is_handed_to_the_verifier_and_a_missing_file_is_not(
    workspace: Path,
) -> None:
    guessed = DoDCriterion(statement="the key is derived from the form",
                           method=VerifyMethod.INSPECTION,
                           expect="src/auth/login.py: derive_account_key")
    absent = DoDCriterion(statement="the limiter module exists",
                          method=VerifyMethod.INSPECTION, expect="src/auth/limiter.py: limit")

    assert verify_criterion(guessed, workspace, Policy(), allow_commands=False) is None
    assert verify_inspection(guessed, workspace).status is CriterionStatus.FAIL, "as it was"
    failed = verify_criterion(absent, workspace, Policy(), allow_commands=False)
    assert failed is not None and failed.status is CriterionStatus.FAIL


def test_a_quote_counts_only_when_it_is_in_the_file(workspace: Path) -> None:
    crit = DoDCriterion(statement="s", method=VerifyMethod.INSPECTION,
                        expect="src/auth/login.py: derive_account_key")

    assert quotes_the_file(crit, "line 3: `account_key = request.form['email']`", workspace)
    assert quotes_the_file(crit, "it reads “account_key   =  request.form”", workspace)
    assert not quotes_the_file(crit, "`account_key = derive(request)`", workspace)
    assert not quotes_the_file(crit, "I read the file and the key is derived.", workspace)
    assert not quotes_the_file(crit, "`login`", workspace), "too short to stand for anything"


def _guessed_wording_plan(fake: Any) -> None:
    fake.overrides["synthesis"] = {
        "summary": "The key derivation is unclear.",
        "recommended_mode": "execute",
        "tasks": [{
            "title": "Derive the account key in src/auth/login.py",
            "action": "Derive the account key from the submitted form in src/auth/login.py",
            "motivation": "The limiter keys on it.",
            "dod": [{
                "statement": "src/auth/login.py derives the account key from the form",
                "method": "inspection",
                # The planner's guess at the wording; the fixture spells it otherwise.
                "expect": "src/auth/login.py: derive_account_key",
                "mandatory": True,
            }],
            "scope_paths": ["src/auth/**"],
            "suggested_role": "implementer",
        }],
    }


async def _verified(supervisor: Supervisor, fake: Any, evidence: str) -> Any:
    _guessed_wording_plan(fake)
    response = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    while response.action != "await_approval":
        response = await supervisor.advance(response.run_id)
    task_id = response.tasks[0]["id"]
    criterion_id = next(c["id"] for c in response.tasks[0]["definition_of_done"]
                        if c["method"] == "inspection")
    fake.overrides["verification"] = {
        "results": [{"criterion_id": criterion_id, "status": "pass", "evidence": evidence}],
        "summary": "checked",
    }
    await supervisor.approve(response.run_id, [{"task_id": task_id, "decision": "approve"}])
    state = supervisor.store.load_state(response.run_id)
    return next(c for c in state.tasks[task_id].dod if c.id == criterion_id)


async def test_a_verifier_that_quotes_the_file_settles_a_guessed_wording(
    supervisor: Supervisor, fake: Any,
) -> None:
    crit = await _verified(supervisor, fake,
                           "src/auth/login.py:3 `account_key = request.form['email']`")

    assert crit.status is CriterionStatus.PASS and crit.verified_by != "harness"


async def test_a_verifier_that_quotes_what_is_not_there_does_not(
    supervisor: Supervisor, fake: Any,
) -> None:
    crit = await _verified(supervisor, fake, "src/auth/login.py:3 `derive_account_key(form)`")

    assert crit.status is CriterionStatus.FAIL and crit.verified_by == "harness"
    assert "does not contain 'derive_account_key'" in crit.evidence


# -- 3. the harness's own checks have their own bound -------------------------------------


def _sleeper(tmp_path: Path, seconds: float) -> DoDCriterion:
    (tmp_path / "slow_check.py").write_text(f"import time\ntime.sleep({seconds})\n",
                                            encoding="utf-8")
    return DoDCriterion(statement="the suite passes", method=VerifyMethod.COMMAND,
                        command="python slow_check.py")


def test_a_check_is_bounded_by_the_check_timeout_not_the_agents(tmp_path: Path) -> None:
    """Baseline run 2: "command timed out after 300s: pytest -q" on an 8-of-9 task."""
    crit = _sleeper(tmp_path, 3)

    agents_short = Policy(command_timeout_seconds=1, check_timeout_seconds=60)
    passed = verify_criterion(crit, tmp_path, agents_short, allow_commands=True)
    assert passed is not None and passed.status is CriterionStatus.PASS, passed

    checks_short = Policy(command_timeout_seconds=60, check_timeout_seconds=1)
    timed_out = verify_criterion(crit, tmp_path, checks_short, allow_commands=True)
    assert timed_out is not None and "timed out after 1s" in timed_out.evidence


def test_the_check_timeout_outlasts_a_full_suite_by_default() -> None:
    assert Policy().check_timeout_seconds >= 1200 > Policy().command_timeout_seconds


# -- 4. a file the run's own check named may be changed within the grant ------------------


def _ripple(supervisor: Supervisor, run_id: str) -> tuple[Any, AgentSpec, ExecutionTask]:
    """A plan whose envelope is src/ and tests/, the owner's grant src/, tests/, docs/."""
    root = supervisor.workspace
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "design.md").write_text("see login.py:3\n", encoding="utf-8")
    (root / "infra").mkdir(exist_ok=True)
    (root / "infra" / "deploy.md").write_text("x\n", encoding="utf-8")
    session = supervisor.store.create(RunState(id=run_id, prompt="p"))
    session.state.envelope = ScopeEnvelope(paths=["src/", "tests/"], source="run plan")
    session.state.envelope_grant = EnvelopeGrant(by="the owner",
                                                 paths=["src/", "tests/", "docs/"])
    gate = DoDCriterion(statement="doc references resolve", method=VerifyMethod.COMMAND,
                        command="python tools/check_doc_refs.py")
    task = ExecutionTask(title="mine", scope=Scope(paths=["src/auth/**"]), dod=[gate])
    me = AgentSpec(id="agt_me", kind=AgentKind.EXECUTION, task_id=task.id,
                   scope=Scope(paths=["src/auth/**"]), status=AgentStatus.RUNNING)
    session.state.tasks[task.id] = task
    session.state.agents[me.id] = me
    return session, me, task


async def test_a_file_a_failing_check_named_is_writable_within_the_grant(
    supervisor: Supervisor,
) -> None:
    """Baseline run 2: three agents escalated for docs/reasoning-control-plane.md."""
    session, me, task = _ripple(supervisor, "run_R1")
    task.dod[0].status = CriterionStatus.FAIL
    task.dod[0].evidence = (f"{supervisor.workspace / 'docs' / 'design.md'}: login.py:3 no "
                            "longer shows check\n1 stale reference(s). See infra/deploy.md")
    box = Toolbox(supervisor.workspace, supervisor.config.policy)

    await supervisor._widen_for_write(session, me, box, "docs/design.md")
    await supervisor._widen_for_write(session, me, box, "infra/deploy.md")

    assert "docs/design.md" in me.scope.paths
    assert "infra/deploy.md" not in me.scope.paths, "named, but outside the owner's grant"
    assert session.state.envelope is not None
    assert "docs/design.md" in session.state.envelope.paths
    assert any("named by a failing check" in n.text for n in session.state.notes)


async def test_the_agents_own_failing_run_of_a_check_names_files_too(
    supervisor: Supervisor,
) -> None:
    """How the agents in baseline run 2 found the document: they ran the gate."""
    session, me, _ = _ripple(supervisor, "run_R3")
    box = Toolbox(supervisor.workspace, supervisor.config.policy)
    gate = ToolCall("run_command", {"command": "python tools/check_doc_refs.py"})
    other = ToolCall("run_command", {"command": "git log docs/design.md"})
    said = ToolResult("run_command", False,
                      "$ python tools/check_doc_refs.py\nexit=1\ndocs/design.md: stale reference")

    supervisor._remember_named(session.state, me, other, said, box)
    await supervisor._widen_for_write(session, me, box, "docs/design.md")
    assert "docs/design.md" not in me.scope.paths, "not one of the run's checks"

    supervisor._remember_named(session.state, me, gate,
                               ToolResult("run_command", True, said.output), box)
    await supervisor._widen_for_write(session, me, box, "docs/design.md")
    assert "docs/design.md" not in me.scope.paths, "the check passed"

    supervisor._remember_named(session.state, me, gate, said, box)
    await supervisor._widen_for_write(session, me, box, "docs/design.md")
    assert "docs/design.md" in me.scope.paths


async def test_a_file_no_check_named_stays_outside_the_plan(supervisor: Supervisor) -> None:
    session, me, _ = _ripple(supervisor, "run_R2")
    box = Toolbox(supervisor.workspace, supervisor.config.policy)

    await supervisor._widen_for_write(session, me, box, "docs/design.md")

    assert me.scope.paths == ["src/auth/**"]
    assert session.state.envelope is not None
    assert session.state.envelope.paths == ["src/", "tests/"]


def test_only_the_runs_own_checks_name_files(tmp_path: Path) -> None:
    """Any failing command would let a model widen its reach by printing a path."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x", encoding="utf-8")
    state = RunState(prompt="p")
    task = ExecutionTask(title="t", dod=[DoDCriterion(
        statement="s", method=VerifyMethod.COMMAND, command="python  tools/check_doc_refs.py")])
    state.tasks[task.id] = task
    box = Toolbox(tmp_path, Policy())

    assert _is_a_check(state, "python tools/check_doc_refs.py")
    assert not _is_a_check(state, "git log docs/a.md")
    assert not _is_a_check(state, "")
    assert _files_named(f"{tmp_path / 'docs' / 'a.md'}: stale; docs/missing.md; ../x.md",
                        box) == {"docs/a.md"}


def test_an_agent_may_run_the_runs_own_check_wherever_its_script_is(tmp_path: Path) -> None:
    """Baseline 2: `python tools/check_doc_refs.py` refused for the script's path."""
    from supervisor_harness.core.tools import normal_command

    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "check.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    box = Toolbox(tmp_path, Policy(allow_command_execution=True))
    scope = Scope(paths=["src/**"])

    refused = box.run_command("python tools/check.py", scope)
    assert not refused.ok and "outside this agent's scope" in refused.output
    ran = box.run_command("python tools/check.py", scope, checks={"python tools/check.py"})
    assert ran.output.startswith("$ python tools/check.py\nexit=3"), ran.output
    other = box.run_command("python tools/other.py", scope, checks={"python tools/check.py"})
    assert "outside this agent's scope" in other.output, "only the check itself"
    assert normal_command("python -m  pytest -q") == normal_command("pytest -q") == "pytest -q"


async def test_a_refused_check_names_nothing(supervisor: Supervisor) -> None:
    """A refusal names the path it refused -- the check's script -- not a broken file."""
    session, me, _ = _ripple(supervisor, "run_R4")
    box = Toolbox(supervisor.workspace, supervisor.config.policy)
    gate = ToolCall("run_command", {"command": "python tools/check_doc_refs.py"})

    supervisor._remember_named(session.state, me, gate, ToolResult(
        "run_command", False, "docs/design.md is outside this agent's scope (src/auth/**)"), box)
    assert not supervisor._named_by_checks.get(me.id)

    supervisor._remember_named(session.state, me, ToolCall(
        "run_command", {"command": "python  tools/check_doc_refs.py"}), ToolResult(
        "run_command", False, "$ python  tools/check_doc_refs.py\nexit=1\ndocs/design.md: stale"),
        box)
    assert supervisor._named_by_checks.get(me.id) == {"docs/design.md"}
