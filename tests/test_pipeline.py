"""End-to-end pipeline tests against the fake provider."""

from __future__ import annotations

from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    AgentKind,
    CriterionStatus,
    Phase,
    RunMode,
    TaskStatus,
)

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"


async def test_full_run_reaches_verified_completion(supervisor: Supervisor) -> None:
    """A run goes analysis -> tasks -> approval -> execution -> verified done."""
    response = await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)

    assert response.action == "complete", response.message
    state = supervisor.store.load_state(response.run_id)
    assert state.phase is Phase.COMPLETE

    # Analysis produced findings from more than one lens.
    lenses = {f.lens for f in state.findings}
    assert len(lenses) >= 2, f"expected multiple lenses, got {lenses}"

    # A task was proposed, approved, executed and verified.
    tasks = list(state.tasks.values())
    assert len(tasks) == 1
    task = tasks[0]
    assert task.status is TaskStatus.VERIFIED
    assert task.dod_satisfied()
    assert not task.unmet_criteria()

    # The report states the verified definition of done.
    assert "Definition-of-done verification" in response.report_markdown
    assert "PASS" in response.report_markdown


async def test_quality_bars_are_added_to_proposed_tasks(supervisor: Supervisor) -> None:
    """Policy-mandated criteria are inserted even when synthesis omits them."""
    response = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    while response.action not in ("await_approval", "complete", "failed"):
        response = await supervisor.advance(response.run_id)

    assert response.action == "await_approval"
    criteria = response.tasks[0]["definition_of_done"]
    statements = " ".join(c["statement"].lower() for c in criteria)

    # Synthesis proposed two; the harness added the security and quality bars.
    assert len(criteria) > 2
    assert "weakness" in statements          # security bar
    assert "conventions" in statements       # code-quality bar
    assert any(c["method"] in ("test", "command") for c in criteria)


async def test_rejecting_every_task_still_produces_a_report(supervisor: Supervisor) -> None:
    """Declining the proposed work ends the run with the analysis, not an error."""
    response = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    while response.action != "await_approval":
        response = await supervisor.advance(response.run_id)

    decisions = [{"task_id": t["id"], "decision": "reject", "note": "not now"}
                 for t in response.tasks]
    final = await supervisor.approve(response.run_id, decisions)

    assert final.action == "complete"
    state = supervisor.store.load_state(final.run_id)
    assert all(t.status is TaskStatus.REJECTED for t in state.tasks.values())
    assert state.report is not None
    assert "Findings" in final.report_markdown


async def test_verification_rejects_a_pass_without_evidence(supervisor: Supervisor, fake) -> None:
    """A criterion marked passed with no evidence is recorded as failed."""
    fake.overrides["verification"] = {
        "results": [{"criterion_id": "will-be-replaced", "status": "pass", "evidence": ""}],
        "summary": "trust me",
    }
    response = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    while response.action != "await_approval":
        response = await supervisor.advance(response.run_id)

    task_id = response.tasks[0]["id"]
    criterion_id = response.tasks[0]["definition_of_done"][0]["id"]
    fake.overrides["verification"] = {
        "results": [{"criterion_id": criterion_id, "status": "pass", "evidence": ""}],
        "summary": "trust me",
    }

    await supervisor.approve(response.run_id, [{"task_id": task_id, "decision": "approve"}])
    state = supervisor.store.load_state(response.run_id)
    criterion = next(c for c in state.tasks[task_id].dod if c.id == criterion_id)

    assert criterion.status is CriterionStatus.FAIL
    assert "without evidence" in criterion.evidence


async def test_run_is_resumable_from_the_event_log(supervisor: Supervisor) -> None:
    """State rebuilt purely from events matches the live state."""
    response = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    while response.action != "await_approval":
        response = await supervisor.advance(response.run_id)

    run_id = response.run_id
    from_snapshot = supervisor.store.load_state(run_id)

    from supervisor_harness.store.events import fold

    from_log = fold(supervisor.store.log(run_id).read_all())

    assert from_log.phase is from_snapshot.phase
    assert set(from_log.agents) == set(from_snapshot.agents)
    assert len(from_log.findings) == len(from_snapshot.findings)
    assert set(from_log.tasks) == set(from_snapshot.tasks)

    # And the supervisor can pick the run back up.
    resumed = await supervisor.resume(run_id)
    assert resumed.action == "await_approval"
    assert resumed.run_id == run_id


async def test_messages_are_routed_and_delivered(supervisor: Supervisor) -> None:
    """A broadcast from one agent reaches its peers through the supervisor."""
    response = await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = supervisor.store.load_state(response.run_id)

    broadcasts = [m for m in state.messages if m.recipient == "*"]
    assert broadcasts, "the security lens should have broadcast its finding"
    assert broadcasts[0].sender.startswith("agt_")


async def test_lessons_are_recorded_and_reused(supervisor: Supervisor) -> None:
    """The improvement loop persists lessons into the cross-run library."""
    response = await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    assert response.action == "complete"

    lessons = supervisor.store.lessons()
    assert lessons, "expected at least one lesson"
    assert any(le.target == "implementer" for le in lessons)
    assert all(le.how_to_apply for le in lessons)

    # A later run for the same role picks them up.
    applicable = supervisor.store.lessons_for(["implementer"])
    assert applicable


async def test_report_mode_skips_execution(supervisor: Supervisor, fake) -> None:
    """When synthesis recommends a report, no tasks and no execution happen."""
    fake.overrides["synthesis"] = {
        "summary": "The endpoint is already throttled upstream by the WAF.",
        "conflicts": [],
        "open_questions": [],
        "recommended_mode": "report",
        "tasks": [],
    }
    response = await supervisor.run(PROMPT, mode=RunMode.AUTO, auto_approve=True)

    assert response.action == "complete"
    state = supervisor.store.load_state(response.run_id)
    assert not state.tasks
    assert not [a for a in state.agents.values() if a.kind is AgentKind.EXECUTION]
    assert "already throttled upstream" in response.report_markdown


async def test_index_projects_the_run(supervisor: Supervisor) -> None:
    """The SQLite projection answers cross-run questions after a run."""
    response = await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    index = supervisor.store.index()

    runs = index.list_runs()
    assert runs and runs[0]["id"] == response.run_id
    assert runs[0]["tasks_verified"] == 1

    methods = {row["method"]: row for row in index.criteria_failure_rate()}
    assert methods, "criteria should be projected"
    assert sum(row["total"] for row in methods.values()) >= 2
