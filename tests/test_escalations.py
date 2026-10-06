"""Escalations: what only the owner can decide, and a run that waits for the answer.

An execution agent that says it cannot go on used to end `blocked`, and its task
went to verification and failed there, with nothing telling anyone why. Now the
harness turns the agent's account into a question for the owner, parks the task,
does everything else it can, and waits at `awaiting_owner`. A grant gives the
task another attempt with the owner's answer; a decline defers it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness import cli
from supervisor_harness.config import HarnessConfig
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.core.trajectory import build_trajectory
from supervisor_harness.host.detect import HostInfo
from supervisor_harness.models import (
    AgentKind,
    EscalationReason,
    Phase,
    Resolution,
    RunMode,
    TaskStatus,
)
from supervisor_harness.providers.base import CompletionRequest
from supervisor_harness.providers.router import ModelRouter
from supervisor_harness.store.runstore import RunStore

from .conftest import FakeProvider
from .test_host_delegation import BLOCKED_EXECUTION

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"


def _two_tasks(fake: FakeProvider) -> None:
    """A synthesis proposing the limiter and an unrelated audit-log task."""
    synthesis = fake._synthesis(None)  # type: ignore[arg-type]
    limiter = synthesis["tasks"][0]
    audit = json.loads(json.dumps(limiter))
    audit["title"] = "Log refused logins to the audit trail"
    audit["action"] = "Write each refused login to the audit log in src/auth/login.py."
    synthesis["tasks"] = [limiter, audit]
    fake.overrides["synthesis"] = synthesis


class BlockingLimiter(FakeProvider):
    """The limiter's agent blocks until the owner has answered; everything else works."""

    def __init__(self) -> None:
        super().__init__()
        self.limiter_attempts = 0

    def _execution(self, request: CompletionRequest) -> dict[str, Any]:
        brief = request.messages[0].content
        if "Execution brief: Add rate limiting" in brief:
            self.limiter_attempts += 1
            if "The owner's answer" not in brief:
                return dict(BLOCKED_EXECUTION)
        return super()._execution(request)


@pytest.fixture
def blocking() -> BlockingLimiter:
    fake = BlockingLimiter()
    _two_tasks(fake)
    return fake


@pytest.fixture
def sup(workspace: Path, config: HarnessConfig, blocking: BlockingLimiter) -> Supervisor:
    store = RunStore(workspace / ".supervisor")
    host = HostInfo(name="test-host", workspace=str(workspace), confidence=1.0)
    router = ModelRouter(config, host_name=host.name)
    router.register("fake", blocking)
    return Supervisor(workspace=workspace, config=config, store=store, host=host, router=router)


def _tasks(sup: Supervisor, run_id: str) -> dict[str, Any]:
    state = sup.store.load_state(run_id)
    return {t.title.split()[0]: t for t in state.tasks.values()}


# -- raising -------------------------------------------------------------------


async def test_a_blocked_agent_parks_its_task_and_the_run_waits_after_the_rest(
    sup: Supervisor,
) -> None:
    response = await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = sup.store.load_state(response.run_id)

    assert response.action == "await_owner", response.message
    assert state.phase is Phase.AWAITING_OWNER
    tasks = _tasks(sup, response.run_id)
    assert tasks["Add"].status is TaskStatus.BLOCKED, "the blocked task is parked"
    assert tasks["Log"].status is TaskStatus.VERIFIED, (
        "the other task was finished before the run stopped to ask"
    )

    (escalation,) = state.escalations.values()
    assert escalation.reason is EscalationReason.AGENT_BLOCKED
    assert escalation.task_id == tasks["Add"].id
    assert escalation.detail == BLOCKED_EXECUTION["blocked_on"], "the agent's own words"
    assert escalation.open
    assert [e["id"] for e in response.detail["escalations"]] == [escalation.id]
    assert [t["id"] for t in response.tasks] == [tasks["Add"].id]
    assert [e["id"] for e in sup.status(response.run_id)["escalations"]] == [escalation.id]


async def test_an_analysis_agent_that_blocks_raises_nothing(
    sup: Supervisor, blocking: BlockingLimiter,
) -> None:
    """Report mode is unchanged: security-eval measures it, and there is nothing to park."""
    blocking.overrides["analysis"] = {
        "output": "Could not read the deployment config.", "findings": [],
        "status": "blocked", "blocked_on": "access to deploy/",
    }
    response = await sup.run(PROMPT, mode=RunMode.REPORT)
    state = sup.store.load_state(response.run_id)

    assert response.action == "complete", response.message
    assert not state.escalations
    assert any(a.status.value == "blocked" for a in state.agents.values()
               if a.kind is AgentKind.ANALYSIS)


# -- answering -----------------------------------------------------------------


async def test_a_grant_gives_the_task_another_attempt_with_the_owners_answer(
    sup: Supervisor, blocking: BlockingLimiter,
) -> None:
    paused = await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    (escalation_id,) = [e["id"] for e in paused.detail["escalations"]]

    response = await sup.resolve(paused.run_id, [{
        "escalation_id": escalation_id, "decision": "grant",
        "note": "Make the login handler sync.",
    }])
    state = sup.store.load_state(paused.run_id)
    limiter = _tasks(sup, paused.run_id)["Add"]

    assert response.action == "complete", response.message
    assert response.detail["resolutions_applied"] == [escalation_id]
    assert state.escalations[escalation_id].resolution is Resolution.GRANT
    assert "Make the login handler sync." in limiter.action
    assert blocking.limiter_attempts == 2
    assert limiter.status is TaskStatus.VERIFIED


async def test_a_decline_defers_the_task_and_the_report_carries_it_forward(
    sup: Supervisor,
) -> None:
    paused = await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    (escalation_id,) = [e["id"] for e in paused.detail["escalations"]]

    response = await sup.resolve(paused.run_id, [
        {"escalation_id": escalation_id, "decision": "decline", "note": "not this sprint"},
    ])
    limiter = _tasks(sup, paused.run_id)["Add"]

    assert response.action == "complete", response.message
    assert limiter.status is TaskStatus.DEFERRED
    assert "not this sprint" in limiter.decision_note
    report = response.report_markdown
    assert "## Escalations to the owner" in report
    assert "Deferred by the owner, to carry forward" in report
    assert limiter.title in report
    assert "done when:" in report, "with its definition of done, so a later run can start there"


async def test_answers_that_do_not_apply_are_returned_not_guessed(sup: Supervisor) -> None:
    paused = await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    (escalation_id,) = [e["id"] for e in paused.detail["escalations"]]

    response = await sup.resolve(paused.run_id, [
        {"escalation_id": "esc_nope", "decision": "grant"},
        {"escalation_id": escalation_id, "decision": "maybe"},
    ])
    assert response.action == "await_owner", "nothing applied, so the run still waits"
    assert set(response.detail["not_applied"]) == {"esc_nope", escalation_id}
    assert response.detail["resolutions_applied"] == []

    await sup.resolve(paused.run_id, [{"escalation_id": escalation_id, "decision": "decline"}])
    again = await sup.resolve(paused.run_id, [{"escalation_id": escalation_id,
                                               "decision": "grant"}])
    assert "already answered" in again.detail["not_applied"][escalation_id]
    assert _tasks(sup, paused.run_id)["Add"].status is TaskStatus.DEFERRED


async def test_the_record_survives_a_replay_and_is_exported(sup: Supervisor) -> None:
    paused = await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    (escalation_id,) = [e["id"] for e in paused.detail["escalations"]]
    await sup.resolve(paused.run_id, [{"escalation_id": escalation_id,
                                       "decision": "decline"}])

    events = sup.store.log(paused.run_id).read_all()
    trajectory = build_trajectory(sup.store.load_state(paused.run_id), events)
    kinds = {(s.kind, s.decided_by) for s in trajectory.steps}
    assert ("escalation_raised", "policy") in kinds
    assert ("escalation_resolved", "person") in kinds


# -- the command line ----------------------------------------------------------


def test_the_cli_lists_and_answers_escalations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    workspace: Path, config: HarnessConfig, blocking: BlockingLimiter,
) -> None:
    def make(args: Any) -> Supervisor:
        host = HostInfo(name="test-host", workspace=str(workspace), confidence=1.0)
        router = ModelRouter(config, host_name=host.name)
        router.register("fake", blocking)
        return Supervisor(workspace=workspace, config=config,
                          store=RunStore(workspace / ".supervisor"), host=host, router=router)

    monkeypatch.setattr(cli, "_supervisor", make)
    monkeypatch.delenv("SUPERVISOR_HOME", raising=False)
    where = ["-w", str(workspace)]

    assert cli.main(["run", PROMPT, "--mode", "execute", "--yes", *where]) == 0
    out = capsys.readouterr().out
    assert "escalation(s) waiting for you" in out
    assert "supervisor resolve" in out, "the run says how to answer"

    assert cli.main(["escalations", "--open", "--json", *where]) == 0
    listed = json.loads(capsys.readouterr().out)
    (escalation,) = listed["escalations"]
    assert listed["phase"] == "awaiting_owner"

    with pytest.raises(SystemExit):  # argparse refuses anything but grant or decline
        cli.main(["resolve", escalation["id"], "maybe", *where])
    capsys.readouterr()
    assert cli.main(["resolve", "esc_nope", "decline", *where]) == 1
    assert "not applied" in capsys.readouterr().err

    assert cli.main(["resolve", escalation["id"], "grant", "--note", "go sync",
                     "--json", *where]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "complete"
    assert cli.main(["escalations", "--open", *where]) == 0
    assert "No escalations." in capsys.readouterr().out
