"""Task dependency resolution, per-attempt verification, and finding closure.

Three defects that this harness demonstrated on itself while reviewing itself.

The synthesis model names a dependency by title, because task ids do not exist
until the harness parses its answer -- but ``runnable_tasks`` matched those
titles against a set of ids, so a dependent task was approved by the user and
then never dispatched, silently, for the life of the run.

Separately, a retired verification agent stays in ``state.agents`` because the
fold replays it, and the guard that decides whether to build a verifier did not
look at which attempt the existing one belonged to. A remediated task was
therefore closed on its first attempt's evidence and could never be re-proven.

And tasks were derived from findings without the mapping being recorded, so
nothing at the end of a run could say which findings it had actually closed.
"""

from __future__ import annotations

from typing import Any

from supervisor_harness.core import phases
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    CriterionStatus,
    DoDCriterion,
    ExecutionTask,
    Finding,
    RunState,
    TaskStatus,
)


def _task(title: str, **kw: object) -> ExecutionTask:
    return ExecutionTask(title=title, action=f"do: {title}", **kw)  # type: ignore[arg-type]


# -- dependency resolution -------------------------------------------------


def test_a_dependency_named_by_title_resolves_to_that_tasks_id() -> None:
    """The model writes titles; runnable_tasks compares ids. Bridge the two."""
    first = _task("Settle every agent-terminating path")
    second = _task("Give each remediation attempt its own verifier",
                   depends_on=["Settle every agent-terminating path"])

    notes = phases.resolve_dependencies([first, second])

    assert second.depends_on == [first.id]
    assert notes == {}


def test_dependency_resolution_is_insensitive_to_case_and_padding() -> None:
    first = _task("Fix the index")
    second = _task("Use the index", depends_on=["  fix THE index  "])

    phases.resolve_dependencies([first, second])

    assert second.depends_on == [first.id]


def test_an_already_resolved_id_is_left_alone() -> None:
    """Resolution must be idempotent -- it runs once, but must not corrupt ids."""
    first = _task("First")
    second = _task("Second", depends_on=[first.id])

    phases.resolve_dependencies([first, second])
    phases.resolve_dependencies([first, second])

    assert second.depends_on == [first.id]


def test_a_dependency_naming_no_task_is_dropped_and_reported() -> None:
    """Left in place it would block the task forever, and say nothing."""
    task = _task("Lonely", depends_on=["A task nobody proposed"])

    notes = phases.resolve_dependencies([task])

    assert task.depends_on == []
    assert "A task nobody proposed" in " ".join(notes[task.id])


def test_a_self_dependency_is_dropped_and_reported() -> None:
    task = _task("Recursive", depends_on=["Recursive"])

    notes = phases.resolve_dependencies([task])

    assert task.depends_on == []
    assert notes[task.id]


def test_a_dependent_task_is_runnable_only_once_its_dependency_verifies() -> None:
    """The end-to-end property the resolution exists to make true."""
    first = _task("First", status=TaskStatus.APPROVED)
    second = _task("Second", depends_on=["First"], status=TaskStatus.APPROVED)
    phases.resolve_dependencies([first, second])

    state = RunState(prompt="p")
    state.tasks = {first.id: first, second.id: second}

    runnable = {t.id for t in phases.runnable_tasks(state)}
    assert runnable == {first.id}, "the dependent task must wait"

    first.status = TaskStatus.VERIFIED
    runnable = {t.id for t in phases.runnable_tasks(state)}
    assert second.id in runnable, "the dependent task must run once unblocked"


def test_an_unresolved_dependency_would_park_the_task_forever() -> None:
    """Guards the regression directly: unresolved, the task never runs."""
    first = _task("First", status=TaskStatus.VERIFIED)
    second = _task("Second", depends_on=["First"], status=TaskStatus.APPROVED)
    # deliberately NOT resolved
    state = RunState(prompt="p")
    state.tasks = {first.id: first, second.id: second}

    assert second.id not in {t.id for t in phases.runnable_tasks(state)}

    phases.resolve_dependencies([first, second])
    assert second.id in {t.id for t in phases.runnable_tasks(state)}


# -- per-attempt verification ---------------------------------------------


def test_a_verification_agent_records_the_attempt_it_was_built_for() -> None:
    task = _task("Anything")
    task.attempts = 2
    state = RunState(prompt="p")

    agent = phases.build_verification_agent(state, task, _config(), _registry())

    assert agent.attempt == 2
    assert agent.task_id == task.id


def test_a_retired_verifier_does_not_match_the_next_attempt() -> None:
    """The guard predicate itself: same task and kind, different attempt.

    A stopped or done verifier stays in state.agents because the fold replays
    AGENT_SPAWNED, so identity alone is not enough to decide whether this
    attempt has been verified.
    """
    task = _task("Anything")
    task.attempts = 2
    retired = AgentSpec(kind=AgentKind.VERIFICATION, task_id=task.id, attempt=1)

    covered = (retired.task_id == task.id
               and retired.kind is AgentKind.VERIFICATION
               and retired.attempt == task.attempts)

    assert not covered, "attempt 2 must not be considered already verified"


def _config():
    from supervisor_harness.config import HarnessConfig

    return HarnessConfig()


def _registry(tmp_path=None):
    from pathlib import Path

    from supervisor_harness.agents.registry import AgentRegistry
    from supervisor_harness.host.detect import HostInfo

    return AgentRegistry(Path(tmp_path or "."), HostInfo(), [])


# -- findings closed, and findings left open -------------------------------
#
# Tasks were derived from findings with no mapping recorded back, so the end of
# a run could not separate "fixed here" from "still open" and someone rebuilt
# the mapping by hand from the report.


def _run(findings: list[Finding], tasks: list[ExecutionTask]) -> RunState:
    state = RunState(prompt="review the harness")
    state.findings = findings
    state.tasks = {t.id: t for t in tasks}
    return state


def _proven(task: ExecutionTask) -> ExecutionTask:
    task.status = TaskStatus.VERIFIED
    task.dod = [DoDCriterion(statement="proven", status=CriterionStatus.PASS)]
    return task


def test_a_task_citing_a_finding_by_title_resolves_to_its_id() -> None:
    """The model is asked for ids and often answers with titles."""
    finding = Finding(title="The scope fence is advisory")
    task = _task("Enforce the scope fence", rationale_refs=["The scope fence is advisory"])

    notes = phases.resolve_rationale_refs([task], [finding])

    assert task.rationale_refs == [finding.id]
    assert task.id not in notes


def test_a_reference_naming_no_finding_is_dropped_and_reported() -> None:
    finding = Finding(title="The scope fence is advisory")
    task = _task("Enforce the scope fence", rationale_refs=[finding.id, "fnd_invented"])

    notes = phases.resolve_rationale_refs([task], [finding])

    assert task.rationale_refs == [finding.id]
    assert any("fnd_invented" in note for note in notes[task.id])


def test_a_task_that_closes_no_finding_is_reported_before_approval() -> None:
    task = _task("Rewrite the logging module")

    notes = phases.resolve_rationale_refs([task], [Finding(title="unrelated")])

    assert any("closes no finding" in note for note in notes[task.id])


def test_only_a_verified_task_closes_the_finding_it_claimed() -> None:
    fixed, attempted, ignored = (
        Finding(title="a"), Finding(title="b"), Finding(title="c")
    )
    done = _proven(_task("close a", rationale_refs=[fixed.id]))
    failed = _task("close b", rationale_refs=[attempted.id], status=TaskStatus.FAILED,
                   dod=[DoDCriterion(statement="not proven")])
    state = _run([fixed, attempted, ignored], [done, failed])

    outcome = {r.finding.id: r.state for r in phases.reconcile_findings(state)}

    assert outcome[fixed.id] == phases.FINDING_FIXED
    assert outcome[attempted.id] == phases.FINDING_ATTEMPTED
    assert outcome[ignored.id] == phases.FINDING_OPEN


def test_a_finding_whose_only_task_was_rejected_stays_open() -> None:
    finding = Finding(title="The lock can spin")
    rejected = _task("bound the retry", rationale_refs=[finding.id],
                     status=TaskStatus.REJECTED)
    state = _run([finding], [rejected])

    row = phases.reconcile_findings(state)[0]

    assert row.state == phases.FINDING_OPEN
    assert "rejected" in row.reason


def test_the_reconciliation_artifact_accounts_for_every_finding() -> None:
    fixed, open_ = Finding(title="the fence is advisory"), Finding(title="the lock can spin")
    state = _run([fixed, open_], [_proven(_task("close it", rationale_refs=[fixed.id]))])

    markdown = phases.reconciliation_markdown(state)

    assert "2 finding(s): 1 fixed" in markdown
    assert "1 still open" in markdown
    assert fixed.id in markdown and open_.id in markdown
    assert "Still open" in markdown and "Fixed and proven in this run" in markdown


def test_the_final_report_says_what_the_run_did_not_close() -> None:
    open_ = Finding(title="the lock can spin", recommendation="bound the retry")
    state = _run([open_], [])

    markdown = phases.final_report_markdown(state)

    assert "## Findings reconciliation" in markdown
    assert "1 still open" in markdown
    assert "no task claimed this finding" in markdown


# -- a dependency chain runs a wave at a time -------------------------------


async def test_a_task_waiting_on_another_runs_once_that_one_is_verified(
    supervisor: Any, fake: Any,
) -> None:
    """Baseline 2: four runs of five ran their first task and ended.

    The second task waited on the first, which was still awaiting its verifier
    when the first wave finished; verification then went straight to the
    checkpoint, which found nothing failed to send back, and the run ended with
    the rest of the plan approved and never started.
    """
    import copy

    from supervisor_harness.models import RunMode

    plan = copy.deepcopy(fake._synthesis(None))
    first = plan["tasks"][0]
    second = copy.deepcopy(first)
    second.update(title="Cover the limiter in the cache layer", depends_on=[first["title"]],
                  action="Add a per-account counter to src/cache.py.",
                  scope_paths=["src/cache.py"])
    plan["tasks"].append(second)
    fake.overrides["synthesis"] = plan

    response = await supervisor.run("Add rate limiting to the public login endpoint",
                                    mode=RunMode.EXECUTE, auto_approve=True)
    state = supervisor.store.load_state(response.run_id)

    by_title = {t.title: t for t in state.tasks.values()}
    waited = by_title["Cover the limiter in the cache layer"]
    assert waited.depends_on == [by_title[first["title"]].id]
    assert waited.attempts >= 1, f"never started: {waited.status}"
    assert by_title[first["title"]].status is TaskStatus.VERIFIED


async def test_a_task_whose_dependency_failed_for_good_still_runs(
    supervisor: Any, fake: Any,
) -> None:
    """Baseline 2: one failed task left the rest of three plans never started."""
    import copy

    from supervisor_harness.models import RunMode

    plan = copy.deepcopy(fake._synthesis(None))
    first = plan["tasks"][0]
    # A wording the file lacks, and a verifier that quotes nothing from it: fails.
    first["dod"][1]["expect"] = "src/auth/login.py: no_such_limiter"
    second = copy.deepcopy(plan["tasks"][0])
    second.update(title="Cover the limiter in the cache layer", depends_on=[first["title"]],
                  action="Add a per-account counter to src/cache.py.",
                  scope_paths=["src/cache.py"])
    second["dod"][1]["expect"] = "src/auth/login.py: account_key"
    plan["tasks"].append(second)
    fake.overrides["synthesis"] = plan

    response = await supervisor.run("Add rate limiting to the public login endpoint",
                                    mode=RunMode.EXECUTE, auto_approve=True)
    state = supervisor.store.load_state(response.run_id)

    by_title = {t.title: t for t in state.tasks.values()}
    assert by_title[first["title"]].status is TaskStatus.FAILED
    waited = by_title["Cover the limiter in the cache layer"]
    assert waited.attempts >= 1, f"never started: {waited.status}"
    assert any("runs although" in n.text for n in state.notes)


def test_a_dependency_held_for_the_owner_still_holds(supervisor: Any) -> None:
    held = _task("Held", status=TaskStatus.BLOCKED)
    failed = _task("Failed", status=TaskStatus.FAILED, attempts=1)
    both = _task("Both", status=TaskStatus.APPROVED, depends_on=[held.id, failed.id])
    one = _task("One", status=TaskStatus.APPROVED, depends_on=[failed.id])
    session = supervisor.store.create(RunState(prompt="p"))
    session.state.tasks = {t.id: t for t in (held, failed, both, one)}

    supervisor._leave_or_release(session)

    assert one.depends_on == [] and both.depends_on == [held.id, failed.id]
