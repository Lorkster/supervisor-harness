"""A project-wide check failed only on another task's files is that task's to fix.

Minesweeper run 8: the engine task's `npm run typecheck` and `npm test` failed on
nothing but `e2e/minesweeper.spec.ts`, which the task writing the browser tests
was writing at the same time. The engine was sent back for it, on its last
attempt, and could not touch the file. Now the check goes on the task whose files
the failure names, and the engine's check waits for that task and runs again.
Every test runs a real command in the workspace.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from supervisor_harness.core.dod import VerificationOutcome
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    AgentStatus,
    CriterionStatus,
    DoDCriterion,
    ExecutionTask,
    Phase,
    RunState,
    Scope,
    TaskStatus,
    VerifyMethod,
)
from supervisor_harness.store.runstore import RunSession

CHECK = (
    "import pathlib, sys\n"
    "broken = pathlib.Path('broken.txt')\n"
    "if broken.exists():\n"
    "    print(broken.read_text())\n"
    "    sys.exit(1)\n"
    "print('all good')\n"
)
#: What a whole-project check prints when only the browser tests are broken: the
#: broken file, and a passing one that is a verified task's.
THEIRS = "e2e/spec.ts(3,1): error TS2345: not assignable\n ✓ tests/engine.test.ts (46 tests)\n"


@pytest.fixture
def project(supervisor: Supervisor) -> Path:
    supervisor.config.policy.allow_command_execution = True
    root = supervisor.workspace
    for rel, text in {"check.py": CHECK, "src/engine.ts": "export {}\n",
                      "e2e/spec.ts": "test()\n", "tests/engine.test.ts": "it()\n"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def _gate(*, holds: int = 0, mandatory: bool = True) -> DoDCriterion:
    return DoDCriterion(statement="the project typechecks", method=VerifyMethod.COMMAND,
                        command="python check.py", holds=holds, mandatory=mandatory)


def _plan(supervisor: Supervisor, run_id: str,
          owner_first: bool = False) -> tuple[RunSession, dict[str, ExecutionTask]]:
    tasks = {
        "engine": ExecutionTask(title="Build the engine", scope=Scope(paths=["src/engine.ts"]),
                                status=TaskStatus.AWAITING_VERIFICATION, attempts=1,
                                dod=[_gate()]),
        "e2e": ExecutionTask(title="Write the browser tests", scope=Scope(paths=["e2e/**"]),
                             status=TaskStatus.AWAITING_VERIFICATION, attempts=1,
                             dod=[DoDCriterion(statement="the browser tests pass",
                                               method=VerifyMethod.COMMAND,
                                               command="python check.py e2e")]),
        "tests": ExecutionTask(title="Write the engine tests", scope=Scope(paths=["tests/**"]),
                               status=TaskStatus.VERIFIED, attempts=1),
    }
    order = ["e2e", "engine", "tests"] if owner_first else list(tasks)
    state = RunState(id=run_id, prompt="p", phase=Phase.VERIFYING,
                     tasks={tasks[k].id: tasks[k] for k in order})
    return supervisor.store.create(state), tasks


def _break(root: Path, output: str = THEIRS) -> None:
    (root / "broken.txt").write_text(output, encoding="utf-8")


def test_the_check_waits_for_the_task_whose_files_it_failed_on(
    supervisor: Supervisor, project: Path,
) -> None:
    # The owner checked first, so its copy of the check is recorded with the engine's.
    session, tasks = _plan(supervisor, "run_Hold", owner_first=True)
    engine, e2e, verified = tasks["engine"], tasks["e2e"], tasks["tests"]
    _break(project)

    supervisor._verify_mechanically(session)

    (gate,) = engine.dod
    assert gate.status is CriterionStatus.UNVERIFIED, "not charged to the engine"
    assert (gate.held_for, gate.holds) == ([e2e.id], 1)
    assert gate.evidence.startswith("held for 'Write the browser tests'")
    put = next(c for c in e2e.dod if c.command == "python check.py")
    assert put.status is CriterionStatus.FAIL and put.verified_by == "harness"
    assert "Found by the harness while checking 'Build the engine'" in e2e.action
    assert "`python check.py` fails on e2e/spec.ts, which are this task's work" in e2e.action
    assert verified.status is TaskStatus.VERIFIED and verified.dod == [], (
        "a verified task's file that only passed is left alone")

    supervisor._settle_tasks(session)
    assert engine.status is TaskStatus.AWAITING_VERIFICATION, "settled once the owner has"
    assert e2e.status is TaskStatus.FAILED


async def test_it_runs_again_once_the_owner_has_settled(
    supervisor: Supervisor, project: Path,
) -> None:
    session, tasks = _plan(supervisor, "run_Again")
    engine, e2e = tasks["engine"], tasks["e2e"]
    _break(project)
    supervisor._verify_mechanically(session)
    supervisor._settle_tasks(session)

    assert await supervisor._follow_up(session) == 1
    assert e2e.status is TaskStatus.APPROVED
    assert supervisor._held(session.state, engine), "the owner is coming back"
    assert not supervisor._release_holds(session)
    supervisor._verify_mechanically(session)
    assert (engine.dod[0].status, engine.dod[0].holds) == (CriterionStatus.UNVERIFIED, 1), (
        "not run again while it waits")

    (project / "broken.txt").unlink()
    e2e.status = TaskStatus.VERIFIED
    assert supervisor._release_holds(session)
    supervisor._verify_mechanically(session)
    supervisor._settle_tasks(session)
    assert engine.dod[0].status is CriterionStatus.PASS
    assert engine.dod[0].held_for == [], "a check run again waits for nothing"
    assert engine.status is TaskStatus.VERIFIED and engine.attempts == 1


@pytest.mark.parametrize(("case", "output"), [
    ("names its own file", "src/engine.ts(1,1): error\ne2e/spec.ts(3,1): error\n"),
    ("names no file", "something went wrong\n"),
    ("names a file nobody owns", "check.py(1,1): error\n"),
])
def test_a_failure_that_may_be_its_own_is_charged_to_it(
    supervisor: Supervisor, project: Path, case: str, output: str,
) -> None:
    session, tasks = _plan(supervisor, f"run_Own{len(case)}")
    _break(project, output)
    supervisor._verify_mechanically(session)
    gate = tasks["engine"].dod[0]
    assert gate.status is CriterionStatus.FAIL and gate.held_for == [], case


def test_an_owner_that_waits_on_it_takes_the_check(supervisor: Supervisor, project: Path) -> None:
    """Minesweeper run 17: the engine's lint failed on the unit tests' file, whose task
    waited for the engine. Held for it, the check would never run again; charged here,
    the engine could not fix it. The owner comes after: the check moves there."""
    session, tasks = _plan(supervisor, "run_Loop")
    engine, e2e = tasks["engine"], tasks["e2e"]
    e2e.depends_on, e2e.status = [engine.id], TaskStatus.APPROVED   # waiting for the engine
    e2e.action = "Write the browser tests."
    _break(project)

    supervisor._verify_mechanically(session)

    assert engine.dod == [], "not the engine's"
    moved = [c for c in e2e.dod if c.command == "python check.py"]
    assert len(moved) == 1 and moved[0].status is CriterionStatus.UNVERIFIED
    assert e2e.action.startswith("Write the browser tests.")
    assert "Found by the harness while checking 'Build the engine'" in e2e.action
    assert any("which waits on it and whose files it failed on" in n.text
               for n in session.state.notes)


def test_held_a_bounded_number_of_times(supervisor: Supervisor, project: Path) -> None:
    attempts = supervisor.config.policy.max_task_attempts
    session, tasks = _plan(supervisor, "run_Bound")
    tasks["engine"].dod = [_gate(holds=attempts)]
    _break(project)
    supervisor._verify_mechanically(session)
    assert tasks["engine"].dod[0].status is CriterionStatus.FAIL


def test_an_optional_check_is_not_held(supervisor: Supervisor, project: Path) -> None:
    session, tasks = _plan(supervisor, "run_Optional")
    tasks["engine"].dod = [_gate(mandatory=False)]
    _break(project)
    supervisor._verify_mechanically(session)
    assert tasks["engine"].dod[0].status is CriterionStatus.FAIL


def test_a_verified_owner_is_reopened_while_it_has_attempts(
    supervisor: Supervisor, project: Path,
) -> None:
    session, tasks = _plan(supervisor, "run_Reopen")
    e2e = tasks["e2e"]
    e2e.status = TaskStatus.VERIFIED
    e2e.dod[0].status = CriterionStatus.PASS
    _break(project, "e2e/spec.ts(3,1): error\n")
    supervisor._verify_mechanically(session)

    assert tasks["engine"].dod[0].held_for == [e2e.id]
    assert e2e.status is TaskStatus.APPROVED and e2e.assigned_agent_id is None
    assert all(c.status is CriterionStatus.UNVERIFIED for c in e2e.dod), "for the next attempt"
    assert any(c.command == "python check.py" for c in e2e.dod)

    session, tasks = _plan(supervisor, "run_Spent")
    tasks["e2e"].status = TaskStatus.VERIFIED
    tasks["e2e"].attempts = supervisor.config.policy.max_task_attempts
    supervisor._verify_mechanically(session)
    assert tasks["engine"].dod[0].status is CriterionStatus.FAIL, "nobody left to fix it"


async def test_a_verifier_cannot_settle_a_held_check(supervisor: Supervisor, project: Path) -> None:
    session, tasks = _plan(supervisor, "run_Verdict")
    engine = tasks["engine"]
    _break(project)
    supervisor._verify_mechanically(session)
    verifier = AgentSpec(id="agt_v", kind=AgentKind.VERIFICATION, task_id=engine.id,
                         status=AgentStatus.RUNNING, attempt=1)
    session.state.agents[verifier.id] = verifier

    await supervisor._report_verification(session, verifier, {"results": [
        {"criterion_id": engine.dod[0].id, "status": "fail", "evidence": "typecheck exit 2"}]})

    assert engine.dod[0].status is CriterionStatus.UNVERIFIED
    assert engine.status is TaskStatus.AWAITING_VERIFICATION


def test_a_check_does_not_wait_for_work_that_is_not_coming(
    supervisor: Supervisor, project: Path,
) -> None:
    session, tasks = _plan(supervisor, "run_Stuck")
    engine, e2e = tasks["engine"], tasks["e2e"]
    _break(project)
    supervisor._verify_mechanically(session)
    gone = ExecutionTask(title="gone", status=TaskStatus.FAILED,
                         attempts=supervisor.config.policy.max_task_attempts)
    session.state.tasks[gone.id] = gone
    e2e.status, e2e.depends_on = TaskStatus.APPROVED, [gone.id]

    assert supervisor._release_holds(session)
    assert engine.dod[0].held_for == [] and engine.dod[0].holds == 1


def test_the_files_are_read_from_the_whole_output(supervisor: Supervisor, project: Path) -> None:
    session, tasks = _plan(supervisor, "run_Whole")
    outcome = VerificationOutcome(CriterionStatus.FAIL, "$ python check.py\nexit=1\n...",
                                  output="e2e/spec.ts(3,1): error\n" + "x\n" * 2000)
    files, owners = supervisor._owners_of_failure(session.state, tasks["engine"], outcome)
    assert files == {"e2e/spec.ts"} and owners == [tasks["e2e"]]


def test_a_file_named_early_in_a_long_output_counts(supervisor: Supervisor, project: Path) -> None:
    session, tasks = _plan(supervisor, "run_Long")
    _break(project, "e2e/spec.ts(3,1): error TS2345\n" + "noise\n" * 600)
    supervisor._verify_mechanically(session)
    assert tasks["engine"].dod[0].held_for == [tasks["e2e"].id]


@pytest.mark.parametrize("more_to_run", [False, True])
async def test_verification_runs_a_released_check_before_moving_on(
    supervisor: Supervisor, project: Path, more_to_run: bool,
) -> None:
    session, tasks = _plan(supervisor, f"run_Loop{more_to_run}")
    engine, e2e = tasks["engine"], tasks["e2e"]
    if more_to_run:
        other = ExecutionTask(title="Something else", status=TaskStatus.APPROVED)
        session.state.tasks[other.id] = other
    for task in (engine, e2e):
        session.state.agents[f"agt_v_{task.id}"] = AgentSpec(
            id=f"agt_v_{task.id}", kind=AgentKind.VERIFICATION, task_id=task.id,
            status=AgentStatus.DONE, attempt=1)
    _break(project)
    supervisor._verify_mechanically(session)
    # The owner fixed it, and settles in the same round as the check that waits for it.
    (project / "broken.txt").unlink()
    for crit in e2e.dod:
        crit.status = CriterionStatus.PASS

    assert await supervisor._continue_verification(session) is None
    assert e2e.status is TaskStatus.VERIFIED
    assert session.state.phase is Phase.VERIFYING, "the released check runs first"
    assert engine.status is TaskStatus.AWAITING_VERIFICATION
    assert await supervisor._continue_verification(session) is None
    assert engine.status is TaskStatus.VERIFIED


def test_a_failed_owner_counts_while_it_is_about_to_be_followed_up(
    supervisor: Supervisor, project: Path,
) -> None:
    session, tasks = _plan(supervisor, "run_Failed")
    _break(project)
    supervisor._verify_mechanically(session)
    gate, e2e = tasks["engine"].dod[0], tasks["e2e"]
    e2e.status = TaskStatus.FAILED
    assert supervisor._still_held(session.state, gate)
    supervisor.config.policy.follow_up = False
    assert not supervisor._still_held(session.state, gate), "no follow-up is coming"


# -- a task held for the owner comes back when they answer ----------------------------------


def _owner_holds(session: RunSession, task: ExecutionTask) -> None:
    from supervisor_harness.models import Escalation, EscalationReason

    task.status = TaskStatus.BLOCKED
    escalation = Escalation(reason=EscalationReason.AGENT_BLOCKED, task_id=task.id)
    session.state.escalations[escalation.id] = escalation


def test_a_check_waits_for_a_task_held_for_the_owner(
    supervisor: Supervisor, project: Path,
) -> None:
    """Minesweeper run 15: the page's type check failed on the unit tests' file, whose
    task was waiting for the owner; the page was charged, sent back, and escalated in
    turn for wanting to edit the file itself."""
    session, tasks = _plan(supervisor, "run_OwnerHold")
    engine, e2e = tasks["engine"], tasks["e2e"]
    _owner_holds(session, e2e)
    _break(project)

    supervisor._verify_mechanically(session)
    assert engine.dod[0].held_for == [e2e.id], "not charged to the engine"

    supervisor._settle_tasks(session)
    assert engine.status is TaskStatus.AWAITING_VERIFICATION
    assert not supervisor._release_holds(session), "it comes back when the owner answers"
    assert engine.dod[0].held_for == [e2e.id]
    supervisor._verify_mechanically(session)
    assert engine.dod[0].holds == 1, "not run again while it waits"


def test_blocked_with_nothing_for_the_owner_is_not_coming(
    supervisor: Supervisor, project: Path,
) -> None:
    session, tasks = _plan(supervisor, "run_NoEsc")
    tasks["e2e"].status = TaskStatus.BLOCKED      # no escalation open
    _break(project)
    supervisor._verify_mechanically(session)
    assert tasks["engine"].dod[0].status is CriterionStatus.FAIL


async def test_when_the_owner_has_answered_it_runs_again(
    supervisor: Supervisor, project: Path,
) -> None:
    from supervisor_harness.models import Phase

    session, tasks = _plan(supervisor, "run_Answered")
    engine, e2e = tasks["engine"], tasks["e2e"]
    _owner_holds(session, e2e)
    _break(project)
    supervisor._verify_mechanically(session)

    from supervisor_harness.models import Resolution

    for escalation in session.state.escalations.values():   # declined: deferred
        escalation.resolution = Resolution.DECLINE
    e2e.status = TaskStatus.DEFERRED
    session.state.phase = Phase.AWAITING_OWNER
    supervisor._await_owner(session)
    assert session.state.phase is Phase.VERIFYING, "the held check runs before the run ends"
    assert supervisor._release_holds(session) and engine.dod[0].held_for == []
