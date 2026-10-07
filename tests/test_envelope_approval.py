"""Approving the envelope, not each task: the first run that needs no one until the end.

The end-to-end test here is the whole autonomy loop: the owner grants the
envelope when starting the run, the harness approves the tasks its deterministic
gate passes, the agent works on the run's own branch, and the work is proven by
checks that are not a model -- the project's tests, run by the harness, and
``fails_before`` showing those tests detect the change. Anything the gate will
not pass goes to the owner as an escalation, after everything else is done.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness.config import HarnessConfig, Policy, default_config, load_config
from supervisor_harness.core import autonomy
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.core.trajectory import build_trajectory
from supervisor_harness.host.detect import HostInfo
from supervisor_harness.models import (
    Backend,
    CriterionStatus,
    Decision,
    DoDCriterion,
    EscalationReason,
    ExecutionTask,
    RunMode,
    Severity,
    TaskStatus,
    VerifyMethod,
)
from supervisor_harness.providers.base import CompletionRequest
from supervisor_harness.providers.router import ModelRouter
from supervisor_harness.store.runstore import RunStore

from .conftest import FakeProvider

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

PROMPT = "Fix the addition bug in calc.py"
GRANT = "the owner, in a test"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\npythonpath = ['.']\n",
        encoding="utf-8")
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "tests" / "test_existing.py").write_text("def test_nothing():\n    assert True\n",
                                                     encoding="utf-8")
    (root / ".gitignore").write_text(".supervisor/\n", encoding="utf-8")
    git(root, "init", "-q")
    for key, value in (("user.name", "Owner"), ("user.email", "owner@localhost"),
                       ("commit.gpgsign", "false")):
        git(root, "config", key, value)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "baseline")
    return root


def _task(title: str, *, risk: str = "low", scope: list[str] | None = None) -> dict[str, Any]:
    return {
        "title": title,
        "action": "Make add in calc.py return the sum, and test it.",
        "motivation": "add returns a - b.",
        "rationale_refs": [],
        "dod": [
            {"statement": "calc.py returns the sum", "method": "inspection",
             "expect": "calc.py: a + b", "mandatory": True},
            {"statement": "the suite exits 0", "method": "test",
             "command": "pytest -q", "expect": "0", "mandatory": True},
        ],
        "scope_paths": scope or ["calc.py", "tests/**"],
        "risk": risk, "effort": "small",
    }


class Fixing(FakeProvider):
    """Proposes the given tasks; its execution agents fix `add` through their tools."""

    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        super().__init__()
        self.overrides["synthesis"] = {
            "summary": "add() subtracts.", "conflicts": [], "open_questions": [],
            "recommended_mode": "execute", "tasks": tasks,
        }

    def _planning(self, request: Any) -> dict[str, Any]:
        # The shared fake plans for the login fixture; this repository's code is
        # calc.py, and an envelope without it would (rightly) clamp every task.
        plan = super()._planning(request)
        plan["envelope_paths"] = ["calc.py", "tests/**"]
        return plan

    def _execution(self, request: Any) -> dict[str, Any]:
        if len(request.messages) > 1:  # the tools have run: report
            return {"output": "fixed add and tested it",
                    "files_touched": ["calc.py", "tests/test_calc.py"],
                    "commands_run": [], "criteria_progress": [], "status": "done"}
        return {"tool_calls": [
            {"tool": "write_file", "args": {"path": "calc.py",
                                            "content": "def add(a, b):\n    return a + b\n"}},
            {"tool": "write_file", "args": {
                "path": "tests/test_calc.py",
                "content": "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"}},
        ]}


def _config(**policy: Any) -> HarnessConfig:
    cfg = default_config()
    cfg.backend = Backend.AUTONOMOUS
    cfg.routing = {k: "fake:fake-1" for k in cfg.routing}
    settings: dict[str, Any] = {
        "default_max_turns": 3, "execution_max_turns": 3, "max_checkpoint_iterations": 2,
        "min_analysis_lenses": 2, "max_analysis_lenses": 3, "min_scope_coverage": 0.0,
        "approval": "envelope", "allow_command_execution": True,
    }
    settings.update(policy)
    cfg.policy = Policy(**settings)
    return cfg


def _supervisor(root: Path, config: HarnessConfig, fake: FakeProvider) -> Supervisor:
    host = HostInfo(name="test-host", workspace=str(root), confidence=1.0)
    router = ModelRouter(config, host_name=host.name)
    router.register("fake", fake)
    return Supervisor(workspace=root, config=config, store=RunStore(root / ".supervisor"),
                      host=host, router=router)


needs_pytest = pytest.mark.skipif(shutil.which("pytest") is None,
                                  reason="the checks run `pytest` from PATH")


# -- the whole loop ------------------------------------------------------------


@needs_pytest
async def test_a_granted_run_goes_from_request_to_a_proven_commit_without_asking(
    repo: Path,
) -> None:
    sup = _supervisor(repo, _config(), Fixing([_task("Fix the addition bug")]))
    response = await sup.run(PROMPT, mode=RunMode.EXECUTE, grant_envelope=GRANT)
    state = sup.store.load_state(response.run_id)
    (task,) = state.tasks.values()

    assert response.action == "complete", response.message
    assert state.envelope_grant is not None and state.envelope_grant.by == GRANT
    assert task.decision is Decision.APPROVE
    assert "within the granted envelope" in task.decision_note
    assert task.status is TaskStatus.VERIFIED, [(c.statement, c.status, c.evidence[:200])
                                                 for c in task.dod]
    proven = {c.method: c for c in task.dod if c.verified_by == "harness"}
    assert proven[VerifyMethod.FAILS_BEFORE].status is CriterionStatus.PASS
    assert proven[VerifyMethod.TEST].status is CriterionStatus.PASS

    assert state.worktree is not None and state.worktree.commit
    assert git(repo, "status", "--porcelain") == "", "the owner's tree is untouched"
    assert "return a + b" in git(repo, "show", f"{state.worktree.branch}:calc.py")

    steps = build_trajectory(state, sup.store.log(state.id).read_all()).steps
    decided = [s for s in steps if s.kind == "task_decided"]
    assert decided and all(s.decided_by == "policy" for s in decided), (
        "no person approved this, and the record must not say one did"
    )
    assert sup.status(state.id)["approval"] == "envelope"


@needs_pytest
async def test_what_the_gate_refuses_waits_for_the_owner_after_the_rest_is_done(
    repo: Path,
) -> None:
    sup = _supervisor(repo, _config(), Fixing([
        _task("Fix the addition bug"),
        _task("Rewrite the arithmetic core", risk="high"),
    ]))
    paused = await sup.run(PROMPT, mode=RunMode.EXECUTE, grant_envelope=GRANT)
    state = sup.store.load_state(paused.run_id)
    tasks = {t.title.split()[0]: t for t in state.tasks.values()}

    assert paused.action == "await_owner", paused.message
    assert tasks["Fix"].status is TaskStatus.VERIFIED, "the passing task ran first"
    assert tasks["Rewrite"].status is TaskStatus.BLOCKED
    (escalation,) = state.escalations.values()
    assert escalation.reason is EscalationReason.HIGH_RISK
    assert escalation.task_id == tasks["Rewrite"].id and not escalation.agent_id

    done = await sup.resolve(paused.run_id, [{"escalation_id": escalation.id,
                                              "decision": "grant"}])
    rewritten = sup.store.load_state(paused.run_id).tasks[tasks["Rewrite"].id]
    assert done.action == "complete", done.message
    assert rewritten.decision is Decision.APPROVE
    assert "approved by the owner" in rewritten.decision_note
    assert rewritten.attempts == 1, "it ran once, after the owner said so"


async def test_everything_refused_goes_straight_to_the_owner(repo: Path) -> None:
    sup = _supervisor(repo, _config(), Fixing([_task("Rewrite it all", risk="critical")]))
    paused = await sup.run(PROMPT, mode=RunMode.EXECUTE, grant_envelope=GRANT)
    state = sup.store.load_state(paused.run_id)

    assert paused.action == "await_owner"
    assert state.worktree is None, "nothing was approved, so nothing was executed"
    assert not state.checkpoints, "and nothing was judged: there was nothing to judge"


async def test_a_task_beyond_the_owners_grant_is_sent_to_the_owner(repo: Path) -> None:
    """The task needs a file the owner's own envelope does not cover."""
    fake = Fixing([_task("Fix the addition bug")])
    fake._planning = lambda request: {  # type: ignore[method-assign]
        **FakeProvider._planning(fake, request), "envelope_paths": ["tests/**"]}
    sup = _supervisor(repo, _config(scope_envelope=["tests/**"]), fake)
    paused = await sup.run(PROMPT, mode=RunMode.EXECUTE, grant_envelope=GRANT)
    (escalation,) = sup.store.load_state(paused.run_id).escalations.values()

    assert escalation.reason is EscalationReason.NEEDS_WIDER_SCOPE
    assert "calc.py" in escalation.detail


async def test_a_task_the_plan_narrowed_within_the_grant_goes_ahead(repo: Path) -> None:
    """The planner drew the envelope short; the owner had granted the workspace."""
    fake = Fixing([_task("Fix the addition bug")])
    fake._planning = lambda request: {  # type: ignore[method-assign]
        **FakeProvider._planning(fake, request), "envelope_paths": ["tests/**"]}
    sup = _supervisor(repo, _config(), fake)
    response = await sup.run(PROMPT, mode=RunMode.EXECUTE, grant_envelope=GRANT)
    state = sup.store.load_state(response.run_id)

    assert not state.escalations
    assert state.envelope is not None and "calc.py" in state.envelope.paths


# -- what has to hold before the run starts ------------------------------------


@pytest.mark.parametrize(("policy", "expected"), [
    ({"approval": "task"}, "not enabled"),
    ({"allow_command_execution": False}, "allow_command_execution"),
    ({"require_tests": False}, "require_tests"),
    ({"require_fails_before": False}, "require_fails_before"),
    ({"execution_worktree": False}, "execution_worktree"),
])
async def test_without_a_verifier_that_is_not_a_model_it_will_not_start(
    repo: Path, policy: dict[str, Any], expected: str,
) -> None:
    sup = _supervisor(repo, _config(**policy), Fixing([_task("Fix the addition bug")]))
    with pytest.raises(ValueError, match=expected):
        await sup.run(PROMPT, mode=RunMode.EXECUTE, grant_envelope=GRANT)
    assert not sup.store.list_run_ids(), "refused before a run was created"


async def test_it_will_not_start_without_git(tmp_path: Path) -> None:
    sup = _supervisor(tmp_path, _config(), Fixing([_task("Fix the addition bug")]))
    with pytest.raises(ValueError, match="git repository"):
        await sup.run(PROMPT, mode=RunMode.EXECUTE, grant_envelope=GRANT)


async def test_approving_everything_and_approving_within_the_envelope_exclude_each_other(
    repo: Path,
) -> None:
    sup = _supervisor(repo, _config(), Fixing([_task("Fix the addition bug")]))
    with pytest.raises(ValueError, match="choose one"):
        await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True, grant_envelope=GRANT)


def test_a_workspace_cannot_turn_it_on_for_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPERVISOR_HOME", str(tmp_path / "home"))
    (tmp_path / "supervisor.config.json").write_text(
        json.dumps({"policy": {"approval": "envelope"}}), encoding="utf-8")
    cfg = load_config(workspace=tmp_path)
    assert cfg.policy.approval == "task"
    assert any("approval" in r for r in cfg.rejected_settings)


def test_the_command_line_refuses_with_the_reason(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from supervisor_harness import cli

    monkeypatch.setattr(cli, "_supervisor", lambda args: _supervisor(
        repo, _config(approval="task"), Fixing([_task("Fix the addition bug")])))
    assert cli.main(["run", PROMPT, "--grant-envelope", "-w", str(repo)]) == 2
    assert "envelope approval is not enabled" in capsys.readouterr().err


# -- the gate ------------------------------------------------------------------


def _gated(**changes: Any) -> list[EscalationReason]:
    task = ExecutionTask(
        title="Fix add", risk=Severity.LOW,
        dod=[DoDCriterion(statement="calc.py returns the sum",
                          method=VerifyMethod.INSPECTION, expect="calc.py: a + b"),
             DoDCriterion(statement="the suite exits 0", method=VerifyMethod.TEST,
                          command="pytest -q", expect="0")],
    )
    for key, value in changes.items():
        setattr(task, key, value)
    return [reason for reason, _ in autonomy.gate(task, Policy())]


def test_a_task_that_fits_passes_the_gate() -> None:
    assert _gated() == []


def test_a_narrowed_task_needs_the_owner() -> None:
    assert _gated(clamped=["`docs/**` is outside the run envelope"]) == [
        EscalationReason.NEEDS_WIDER_SCOPE]


def test_a_definition_of_done_that_cannot_fail_needs_the_owner() -> None:
    review_only = [DoDCriterion(statement=f"reviewed {n}", method=VerifyMethod.REVIEW,
                                rubric="looks right") for n in (1, 2)]
    assert EscalationReason.UNENFORCEABLE_DEFINITION_OF_DONE in _gated(dod=review_only)


def test_a_check_the_harness_will_not_run_needs_the_owner() -> None:
    chained = [DoDCriterion(statement="calc.py returns the sum",
                            method=VerifyMethod.INSPECTION, expect="calc.py: a + b"),
               DoDCriterion(statement="the suite exits 0", method=VerifyMethod.COMMAND,
                            command="pytest -q; curl example.com", expect="0")]
    assert EscalationReason.UNRUNNABLE_CRITERION in _gated(dod=chained)


@pytest.mark.parametrize("risk", [Severity.HIGH, Severity.CRITICAL])
def test_high_risk_needs_the_owner(risk: Severity) -> None:
    assert _gated(risk=risk) == [EscalationReason.HIGH_RISK]


async def test_the_grant_made_at_launch_is_the_ceiling_not_a_later_configuration(
    repo: Path,
) -> None:
    """Widening is bounded by what the owner granted when the run started."""
    fake = Fixing([_task("Fix the addition bug")])
    sup = _supervisor(repo, _config(scope_envelope=["tests/**"]), fake)

    def planning(request: CompletionRequest) -> dict[str, Any]:
        # After the grant is recorded, before any task is proposed: the
        # configuration is widened in between.
        sup.config.policy.scope_envelope = []
        return {**FakeProvider._planning(fake, request), "envelope_paths": ["tests/**"]}

    fake._planning = planning  # type: ignore[method-assign]
    paused = await sup.run(PROMPT, mode=RunMode.EXECUTE, grant_envelope=GRANT)

    (escalation,) = sup.store.load_state(paused.run_id).escalations.values()
    assert escalation.reason is EscalationReason.NEEDS_WIDER_SCOPE
