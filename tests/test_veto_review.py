"""Batch G: a second reading of each task the gate passed, which can only veto.

Measured in three go-live runs (plantsandclimate P3-18, a local model): the
gate passed, and every check verified, the same task -- add the Playwright
suite to `npm run check` -- though the project's CI runs `npm run check` before
it installs Playwright's browser. A person reading the workflow would have
stopped it. The reviewer reads it.
"""

from __future__ import annotations

from pathlib import Path

from supervisor_harness.core.review import RULING_TOOL, VETO_REASONS, parse_ruling, review_brief
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    DoDCriterion,
    EscalationReason,
    ExecutionTask,
    RunState,
    Scope,
    TaskStatus,
    VerifyMethod,
)
from supervisor_harness.providers.base import (
    CompletionRequest,
    CompletionResponse,
    ToolCall,
)

from .conftest import FakeProvider

CI = ("jobs:\n  check:\n    steps:\n      - run: npm ci\n      - run: npm run check\n"
      "      - run: npx playwright install --with-deps chromium\n")
TASK = ExecutionTask(title="Wire the Playwright e2e test into npm run check",
                     action="Add npm run test:e2e to the check script in package.json",
                     scope=Scope(paths=["package.json"]))


class Reviewer(FakeProvider):
    """Native tools; reads the CI workflow, then rules as told."""

    native_tools = True

    def __init__(self, verdict: str = "breaks_the_project") -> None:
        super().__init__()
        self.verdict = verdict
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if not any(t["name"] == "ruling" for t in request.tools or []):
            return await super().complete(request)
        self.requests.append(request)
        if not any(m.role == "tool" for m in request.messages):
            return CompletionResponse(tool_calls=[ToolCall(
                "read_file", {"path": ".github/workflows/ci.yml"})])
        return CompletionResponse(tool_calls=[ToolCall("ruling", {
            "verdict": self.verdict,
            "why": ".github/workflows/ci.yml runs npm run check before playwright install"})])


def _gate_passes(title: str = TASK.title) -> ExecutionTask:
    """A task the deterministic gate lets through, so only the review can stop it."""
    return ExecutionTask(title=title, action=TASK.action, scope=Scope(paths=["src/"]),
                         status=TaskStatus.PROPOSED, risk="low", dod=[DoDCriterion(
                             statement="the check script runs the e2e suite",
                             method=VerifyMethod.INSPECTION, expect="package.json: test:e2e",
                             mandatory=True), DoDCriterion(
                             statement="the e2e config is unchanged",
                             method=VerifyMethod.INSPECTION,
                             expect="playwright.config.ts: chromium", mandatory=True)])


def _with_ci(workspace: Path) -> None:
    (workspace / ".github" / "workflows").mkdir(parents=True)
    (workspace / ".github" / "workflows" / "ci.yml").write_text(CI, encoding="utf-8")


def test_only_a_named_veto_is_a_veto() -> None:
    assert parse_ruling({"verdict": "breaks_the_project", "why": "ci.yml:5"}) == (
        "breaks_the_project", "ci.yml:5")
    assert parse_ruling({"verdict": "proceed"}) is None
    assert parse_ruling({"verdict": "looks dodgy"}) is None, "off the menu is not a veto"
    assert parse_ruling(None) is None, "no ruling is not a veto"
    assert set(RULING_TOOL["parameters"]["properties"]["verdict"]["enum"]) == {
        "proceed", *VETO_REASONS}


def test_the_brief_carries_the_request_the_task_and_the_menu() -> None:
    brief = review_brief(RunState(prompt="Do P3-18; green when npm run check passes"), TASK)
    assert "Do P3-18" in brief and "test:e2e" in brief and "`package.json`" in brief
    assert all(f"`{name}`" in brief for name in VETO_REASONS)


async def test_the_reviewer_reads_the_workflow_and_vetoes(
    supervisor: Supervisor, workspace: Path,
) -> None:
    _with_ci(workspace)
    fake = Reviewer()
    supervisor.router.register("fake", fake)
    session = supervisor.store.create(RunState(id="run_R", prompt="Do P3-18"))

    veto = await supervisor._review_task(session, TASK)

    assert veto is not None and veto[0] == "breaks_the_project"
    assert any(m.role == "tool" and "npx playwright install" in m.content
               for m in fake.requests[-1].messages), "it ruled on what it read"
    assert fake.requests[0].tools and {t["name"] for t in fake.requests[0].tools}.isdisjoint(
        {"edit_file", "write_file", "delete_file", "run_command", "report"}), "read-only"
    assert any("veto review" in n.text and "vetoed" in n.text for n in session.state.notes)


async def test_a_reviewer_that_proceeds_or_cannot_run_vetoes_nothing(
    supervisor: Supervisor, workspace: Path,
) -> None:
    _with_ci(workspace)
    supervisor.router.register("fake", Reviewer(verdict="proceed"))
    session = supervisor.store.create(RunState(id="run_P", prompt="p"))
    assert await supervisor._review_task(session, TASK) is None

    supervisor.router.register("fake", FakeProvider())          # no native tools
    session = supervisor.store.create(RunState(id="run_N", prompt="p"))
    assert await supervisor._review_task(session, TASK) is None
    assert any("veto review skipped" in n.text for n in session.state.notes)


async def test_a_veto_parks_the_task_for_its_owner(supervisor: Supervisor) -> None:
    async def veto(session: object, task: ExecutionTask) -> tuple[str, str]:
        return "breaks_the_project", "ci.yml runs check before playwright install"

    supervisor._review_task = veto  # type: ignore[method-assign,assignment]
    session = supervisor.store.create(RunState(id="run_V", prompt="p"))
    task = _gate_passes()
    session.state.tasks[task.id] = task

    await supervisor._approve_within_envelope(session, [task])

    (escalation,) = session.state.escalations.values()
    assert escalation.reason is EscalationReason.REVIEW_VETO
    assert "breaks_the_project" in escalation.detail and "ci.yml" in escalation.detail
    assert task.status is TaskStatus.BLOCKED


async def test_the_review_can_be_turned_off_only_where_policy_allows(
    supervisor: Supervisor,
) -> None:
    from supervisor_harness.config import PROTECTED_SETTINGS

    assert ("policy", "veto_review") in PROTECTED_SETTINGS
    called: list[str] = []

    async def review(session: object, task: ExecutionTask) -> None:
        called.append(task.id)

    supervisor._review_task = review  # type: ignore[method-assign,assignment]
    for on, run_id in ((True, "run_On"), (False, "run_Off")):
        supervisor.config.policy.veto_review = on
        session = supervisor.store.create(RunState(id=run_id, prompt="p"))
        task = _gate_passes()
        session.state.tasks[task.id] = task
        await supervisor._approve_within_envelope(session, [task])
        assert (task.id in called) is on, "asked when on, and only then"


def test_every_implementer_is_told_what_is_held_for_the_owner() -> None:
    """Measured (go-live run 16): the veto parked the task, and a peer whose scope
    also held package.json made the vetoed change anyway."""
    from supervisor_harness.agents.brief import build_implementer_brief
    from supervisor_harness.models import AgentKind, AgentSpec, Escalation, Resolution

    run = RunState(prompt="Do P3-18")
    vetoed = ExecutionTask(title=TASK.title, action=TASK.action, scope=TASK.scope)
    peer = ExecutionTask(title="Create the offline e2e test", scope=Scope(paths=["e2e/"]))
    run.tasks.update({vetoed.id: vetoed, peer.id: peer})
    escalation = Escalation(reason=EscalationReason.REVIEW_VETO, task_id=vetoed.id,
                            detail="review_veto: breaks_the_project: ci.yml:31 runs "
                                   "check before playwright install")
    run.escalations[escalation.id] = escalation
    agent = AgentSpec(kind=AgentKind.EXECUTION, task_id=peer.id, scope=peer.scope)

    brief = build_implementer_brief(run, agent, peer)
    assert "## Held for the owner" in brief
    assert "Add npm run test:e2e to the check script" in brief and "ci.yml:31" in brief
    assert "Do not make them, in any file" in brief
    assert "Held for the owner" not in build_implementer_brief(run, agent, vetoed), (
        "not its own task's")

    escalation.resolution = Resolution.GRANT
    assert "Held for the owner" not in build_implementer_brief(run, agent, peer), (
        "answered, it is no longer held")


class LongReader(Reviewer):
    """Reads for as long as reading is offered, then rules."""

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        names = {t["name"] for t in request.tools or []}
        if "ruling" in names and names != {"ruling"}:
            self.requests.append(request)
            return CompletionResponse(tool_calls=[ToolCall(
                "read_file", {"path": ".github/workflows/ci.yml"})])
        return await super().complete(request)


async def test_a_reviewer_that_keeps_reading_is_brought_to_a_ruling(
    supervisor: Supervisor, workspace: Path,
) -> None:
    """Measured (go-live run 17): the review's stretches shared one tool-call count,
    so it was never asked for its ruling -- "no ruling" on most tasks."""
    from supervisor_harness.core.conversation import CHECKPOINT_CALLS

    _with_ci(workspace)
    fake = LongReader()
    supervisor.router.register("fake", fake)
    session = supervisor.store.create(RunState(id="run_L", prompt="Do P3-18"))

    veto = await supervisor._review_task(session, TASK)

    assert veto is not None and veto[0] == "breaks_the_project"
    assert len(fake.requests) > 2 * CHECKPOINT_CALLS
    assert [t["name"] for t in fake.requests[-1].tools or []] == ["ruling"]


def test_the_reviewer_is_shown_the_rest_of_the_plan() -> None:
    """Measured (go-live run 18): two tasks were vetoed for not requiring the
    network-cut Playwright test, which was another task of the same plan, carried
    out and verified. The reviewer had been shown each task alone."""
    from supervisor_harness.core.review import PLAN_ACTION_CHARS, REVIEWER_SYSTEM

    run = RunState(prompt="Do P3-18")
    results = ExecutionTask(title="Offline state in ResultsSection", action="Catch it")
    e2e = ExecutionTask(title="Write the Playwright offline test",
                        action="Drive the app with the network cut " * 20)
    dropped = ExecutionTask(title="Rejected idea", action="x", status=TaskStatus.REJECTED)
    run.tasks.update({t.id: t for t in (results, e2e, dropped)})

    brief = review_brief(run, results)
    plan = brief.split("## The rest of the plan\n", 1)[1].split("\n\n", 1)[0]

    assert "**Write the Playwright offline test**: Drive the app" in plan
    assert "Offline state in ResultsSection" not in plan, "not the task itself"
    assert "Rejected idea" not in plan
    assert len(plan) < PLAN_ACTION_CHARS + 80 and plan.endswith("..."), "each one is short"
    assert "another task in the plan covers" in REVIEWER_SYSTEM
    assert "## The rest of the plan" not in review_brief(RunState(prompt="p"), TASK), (
        "a task alone has no plan to show")


REVISED = {
    "action": "Write e2e/offline.spec.ts only; leave npm run check as it is",
    "scope_paths": ["src/", "infra/"],
    "dod": [
        {"statement": "the spec cuts the network", "method": "inspection",
         "expect": "src/offline.spec.ts: setOffline(true)", "mandatory": True},
        {"statement": "the spec asserts the needs-connection text", "method": "inspection",
         "expect": "src/offline.spec.ts: needsConnection", "mandatory": True},
    ],
}


def _vetoing(supervisor: Supervisor, *, times: int) -> list[str]:
    """The review vetoes the first `times` readings, then lets the task through.

    The workspace gets a test runner, as a real project has: a revised definition
    of done gets the harness's own bars again, and the test bar runs the suite.
    """
    (supervisor.workspace / "package.json").write_text(
        '{"scripts": {"test": "vitest run"}}', encoding="utf-8")
    seen: list[str] = []

    async def review(session: object, task: ExecutionTask) -> tuple[str, str] | None:
        seen.append(task.action)
        if len(seen) <= times:
            return "breaks_the_project", "ci.yml:31 runs check before playwright install"
        return None

    supervisor._review_task = review  # type: ignore[method-assign,assignment]
    return seen


async def test_a_vetoed_task_is_revised_once_and_goes_ahead(supervisor: Supervisor) -> None:
    """Measured (go-live run 21): three tasks vetoed, each rightly and each with its
    fix in the reason; a veto ended each one, and nothing revised them."""
    from supervisor_harness.models import ScopeEnvelope

    fake = FakeProvider()
    fake.script("analysis", REVISED)
    supervisor.router.register("fake", fake)
    seen = _vetoing(supervisor, times=1)
    session = supervisor.store.create(RunState(id="run_Rv", prompt="Do P3-18"))
    session.state.envelope = ScopeEnvelope(paths=["src/"], source="run plan")
    task = _gate_passes()
    session.state.tasks[task.id] = task

    await supervisor._approve_within_envelope(session, [task])

    assert task.status is TaskStatus.APPROVED and not session.state.escalations
    assert seen == [TASK.action, REVISED["action"]], "the revision was read again"
    assert "infra/" not in task.scope.paths, "a revision is held to the run's envelope"
    assert any("setOffline(true)" in c.expect for c in task.dod)
    assert any("revised after a breaks_the_project veto" in n.text
               for n in session.state.notes)


async def test_a_second_veto_goes_to_the_owner_with_the_first_named(
    supervisor: Supervisor,
) -> None:
    fake = FakeProvider()
    fake.script("analysis", REVISED)
    supervisor.router.register("fake", fake)
    _vetoing(supervisor, times=2)
    session = supervisor.store.create(RunState(id="run_R2", prompt="Do P3-18"))
    task = _gate_passes()
    session.state.tasks[task.id] = task

    await supervisor._approve_within_envelope(session, [task])

    (escalation,) = session.state.escalations.values()
    assert escalation.reason is EscalationReason.REVIEW_VETO
    assert "after one revision; first veto: breaks_the_project" in escalation.detail
    assert task.status is TaskStatus.BLOCKED


async def test_a_revision_that_comes_back_empty_leaves_the_veto(supervisor: Supervisor) -> None:
    fake = FakeProvider()
    fake.script("analysis", {"action": "", "dod": []})
    supervisor.router.register("fake", fake)
    seen = _vetoing(supervisor, times=1)
    session = supervisor.store.create(RunState(id="run_Re", prompt="Do P3-18"))
    task = _gate_passes()
    session.state.tasks[task.id] = task

    await supervisor._approve_within_envelope(session, [task])

    assert len(seen) == 1 and task.status is TaskStatus.BLOCKED
    assert any("came back empty" in n.text for n in session.state.notes)


async def test_a_revision_faces_the_gate_again(supervisor: Supervisor) -> None:
    """A revision is a new proposal: one the gate would refuse is refused, and the
    reviewer is not asked about it."""
    unsafe = {**REVISED, "dod": [*REVISED["dod"], {
        "statement": "the cache is cleared first", "method": "command",
        "command": "rm -rf node_modules", "mandatory": True}]}
    fake = FakeProvider()
    fake.script("analysis", unsafe)
    supervisor.router.register("fake", fake)
    seen = _vetoing(supervisor, times=1)
    session = supervisor.store.create(RunState(id="run_Rg", prompt="Do P3-18"))
    task = _gate_passes()
    session.state.tasks[task.id] = task

    await supervisor._approve_within_envelope(session, [task])

    (escalation,) = session.state.escalations.values()
    assert escalation.reason is not EscalationReason.REVIEW_VETO
    assert len(seen) == 1 and task.status is TaskStatus.BLOCKED
