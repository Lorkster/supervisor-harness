"""Supervision behaviour: drift correction, budgets, definitions of done."""

from __future__ import annotations

from pathlib import Path

import pytest

from supervisor_harness.config import Policy, default_config
from supervisor_harness.core.blackboard import Blackboard, detect_contradictions
from supervisor_harness.core.dod import (
    apply_quality_bars,
    validate_criteria,
    verify_inspection,
)
from supervisor_harness.core.drift import (
    TurnContext,
    assess_heuristically,
    decide_directive,
)
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    AgentSpec,
    AgentStatus,
    AgentTurn,
    Budget,
    CriterionStatus,
    DirectiveKind,
    DoDCriterion,
    ExecutionTask,
    Finding,
    Message,
    MessageKind,
    RunMode,
    RunState,
    Scope,
    Severity,
    VerifyMethod,
)

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"


def _agent(**kwargs) -> AgentSpec:
    defaults = dict(
        id="agt_test",
        role="security",
        title="Security",
        objectives=[
            "Identify trust boundaries and untrusted input",
            "Check authentication and authorisation failure modes",
        ],
        scope=Scope(paths=["src/auth/**"], forbidden_paths=["infra/**"],
                    out_of_scope=["performance tuning"]),
        budget=Budget(max_turns=4),
    )
    defaults.update(kwargs)
    return AgentSpec(**defaults)


def _assess(agent: AgentSpec, turn: AgentTurn, index: int = 0, previous=()) -> object:
    ctx = TurnContext(
        agent=agent, turn=turn, previous_turns=list(previous),
        brief="Find exploitable weaknesses. " + " ".join(agent.objectives),
        task_prompt=PROMPT, turn_index=index,
    )
    return assess_heuristically(ctx)


# --------------------------------------------------------------------------
# Drift
# --------------------------------------------------------------------------


def test_solid_turn_is_not_flagged() -> None:
    """Real evidence inside scope produces no drift signal."""
    agent = _agent()
    turn = AgentTurn(
        output=(
            "src/auth/login.py:34 accepts unlimited POST attempts. The trust boundary is "
            "the unauthenticated handler; untrusted input crosses it as the credential "
            "pair. Authorisation fails closed on provider error at guard.py:18."
        ),
        findings=[Finding(title="Login endpoint is unthrottled", severity=Severity.HIGH)],
        files_touched=["src/auth/login.py"],
    )
    assessment = _assess(agent, turn)
    assert assessment.on_task
    assert assessment.score == 0.0


def test_forbidden_path_stops_immediately() -> None:
    """Writing to a forbidden path cannot be corrected after the fact."""
    agent = _agent()
    turn = AgentTurn(output="Rewrote the terraform module.", files_touched=["infra/waf.tf"])
    assessment = _assess(agent, turn)
    directive = decide_directive(assessment, agent, turn, Policy(), turns_used=1)

    assert assessment.score == 1.0
    assert directive.kind is DirectiveKind.STOP
    assert "forbidden" in directive.corrections[0].lower()


def test_excluded_topic_is_corrected_before_it_is_stopped() -> None:
    """First offence narrows the agent; a repeat after correction stops it."""
    agent = _agent()
    turn = AgentTurn(
        output="Performance tuning is the priority here: performance tuning of the token "
               "exchange and further performance tuning of the session store.",
        files_touched=["src/auth/login.py"],
    )
    assessment = _assess(agent, turn, index=2)

    first = decide_directive(assessment, agent, turn, Policy(), 3, prior_corrections=0)
    repeat = decide_directive(assessment, agent, turn, Policy(), 3, prior_corrections=1)

    assert first.kind is DirectiveKind.NARROW
    assert repeat.kind is DirectiveKind.STOP
    assert any("performance tuning" in c for c in first.corrections)


def test_budget_exhaustion_stops_the_agent() -> None:
    agent = _agent(budget=Budget(max_turns=2))
    turn = AgentTurn(output="Still working through the handler.", files_touched=["src/auth/a.py"])
    directive = decide_directive(_assess(agent, turn), agent, turn, Policy(), turns_used=2)

    assert directive.kind is DirectiveKind.STOP
    assert "budget" in directive.rationale


def test_completion_claim_is_refused_when_coverage_is_thin() -> None:
    """An agent cannot close itself out by asserting it is done."""
    agent = _agent()
    turn = AgentTurn(output="Looks fine to me overall.", claimed_status=AgentStatus.DONE)
    directive = decide_directive(_assess(agent, turn, index=2), agent, turn, Policy(), 3)

    assert directive.kind is not DirectiveKind.ACCEPT


async def test_drift_correction_happens_inside_a_live_run(
    workspace: Path, config, fake
) -> None:
    """A drifting analysis agent is corrected mid-run and given another turn."""
    from supervisor_harness.host.detect import HostInfo
    from supervisor_harness.providers.router import ModelRouter
    from supervisor_harness.store.runstore import RunStore

    # First analysis turn goes off the rails; the loop should not accept it.
    fake.overrides["analysis"] = {
        "output": "The marketing homepage hero image is cropped badly on mobile and the "
                  "newsletter modal fires too eagerly for first-time blog readers, which "
                  "hurts conversion across the funnel considerably this quarter.",
        "findings": [],
        "status": "running",
    }

    store = RunStore(workspace / ".supervisor")
    host = HostInfo(name="test-host", workspace=str(workspace), confidence=1.0)
    router = ModelRouter(config, host_name=host.name)
    router.register("fake", fake)
    supervisor = Supervisor(workspace=workspace, config=config, store=store,
                            host=host, router=router)

    response = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    while response.action not in ("await_approval", "complete", "failed"):
        response = await supervisor.advance(response.run_id)

    state = supervisor.store.load_state(response.run_id)
    corrections = [
        d for d in state.directives
        if d.kind in (DirectiveKind.REFOCUS, DirectiveKind.NARROW, DirectiveKind.DEEPEN)
    ]
    assert corrections, "a drifting agent should have been corrected"
    assert any(d.corrections for d in corrections), "corrections must be actionable"
    assert any(state.drift[a].score > 0.5 for a in state.drift)


# --------------------------------------------------------------------------
# Definitions of done
# --------------------------------------------------------------------------


def test_unfalsifiable_criteria_are_rejected() -> None:
    criteria = [
        DoDCriterion(statement="The code works correctly", method=VerifyMethod.INSPECTION),
        DoDCriterion(statement="Quality is good", method=VerifyMethod.INSPECTION),
    ]
    problems = " ".join(i.problem for i in validate_criteria(criteria, Policy()))

    assert "unfalsifiable" in problems
    assert "does not say what proves it" in problems


def test_compound_and_incomplete_criteria_are_flagged() -> None:
    criteria = [
        DoDCriterion(statement="Tests pass and docs are updated", method=VerifyMethod.COMMAND),
        DoDCriterion(statement="Reviewed for safety", method=VerifyMethod.REVIEW),
    ]
    problems = [i.problem for i in validate_criteria(criteria, Policy())]

    assert any("compound" in p for p in problems)
    assert any("no command given" in p for p in problems)
    assert any("no rubric" in p for p in problems)


def test_quality_bars_apply_only_to_code_tasks() -> None:
    code_task = ExecutionTask(
        title="Add a rate limiter",
        action="Implement middleware in src/auth/login.py",
        dod=[DoDCriterion(statement="Returns 429 on the eleventh attempt",
                          method=VerifyMethod.COMMAND, command="pytest -q", expect="0")],
    )
    prose_task = ExecutionTask(
        title="Rewrite the launch announcement",
        action="Shorten the announcement paragraph for the newsletter",
        dod=[DoDCriterion(statement="Under 120 words", method=VerifyMethod.INSPECTION,
                          expect="draft.md: words")],
    )

    added_code = apply_quality_bars(code_task, Policy())
    added_prose = apply_quality_bars(prose_task, Policy())

    assert {c.method for c in added_code} >= {VerifyMethod.TEST, VerifyMethod.REVIEW}
    assert all(c.mandatory for c in added_code)
    assert added_prose == []


def test_quality_bars_are_not_suppressed_by_the_word_author() -> None:
    """'author' is not 'auth': only a criterion about authorisation covers the bar."""
    innocent = ExecutionTask(
        title="Add a rate limiter",
        action="Implement middleware in src/limits.py",
        dod=[DoDCriterion(statement="The module docstring names the author",
                          method=VerifyMethod.INSPECTION, expect="src/limits.py: Author")],
    )
    covered = ExecutionTask(
        title="Add a rate limiter",
        action="Implement middleware in src/limits.py",
        dod=[DoDCriterion(statement="Authorisation is checked before the counter is read",
                          method=VerifyMethod.INSPECTION, expect="src/limits.py: require_role")],
    )

    added_innocent = apply_quality_bars(innocent, Policy())
    added_covered = apply_quality_bars(covered, Policy())

    assert any("weakness" in c.statement for c in added_innocent), \
        "the security bar was suppressed by the word 'author'"
    assert not any("weakness" in c.statement for c in added_covered), \
        "a criterion about authorisation should still cover the security bar"


def test_quality_bars_are_not_suppressed_by_the_word_inspection() -> None:
    """'inspection' is not 'spec': the test bar survives it."""
    task = ExecutionTask(
        title="Add a rate limiter",
        action="Implement middleware in src/limits.py",
        dod=[DoDCriterion(statement="A manual inspection shows the counter is shared",
                          method=VerifyMethod.INSPECTION, expect="src/limits.py: redis")],
    )

    added = apply_quality_bars(task, Policy())

    assert any(c.method is VerifyMethod.TEST for c in added), \
        "the test bar was suppressed by the word 'inspection'"


def test_quality_bars_are_not_suppressed_by_a_criterion_about_output_format() -> None:
    """'the output format' is not 'formatting': the code-quality bar survives it."""
    innocent = ExecutionTask(
        title="Emit the run report as JSON",
        action="Change the reporter in src/report.py to serialise its result",
        dod=[DoDCriterion(statement="The output format is JSON with a top-level 'items' key",
                          method=VerifyMethod.INSPECTION, expect="src/report.py: json.dumps")],
    )
    covered = ExecutionTask(
        title="Emit the run report as JSON",
        action="Change the reporter in src/report.py to serialise its result",
        dod=[DoDCriterion(statement="The formatter reports no diff on the files touched",
                          method=VerifyMethod.COMMAND, command="ruff format --check src",
                          expect="0")],
    )

    added_innocent = apply_quality_bars(innocent, Policy())
    added_covered = apply_quality_bars(covered, Policy())

    assert any("conventions" in c.statement for c in added_innocent),         "the code-quality bar was suppressed by the word 'format'"
    assert not any("conventions" in c.statement for c in added_covered),         "a criterion about the formatter should still cover the code-quality bar"


def test_quality_bars_are_not_suppressed_by_test_data_or_security_cameras() -> None:
    """The same weakness in the other two lists: a subject word is not a check."""
    task = ExecutionTask(
        title="Import the security cameras feed",
        action="Add the importer in src/cameras.py",
        dod=[
            DoDCriterion(statement="The seed test data loads without a manual step",
                         method=VerifyMethod.INSPECTION, expect="src/cameras.py: seed"),
            DoDCriterion(statement="Every security cameras record keeps its site id",
                         method=VerifyMethod.INSPECTION, expect="src/cameras.py: site_id"),
        ],
    )

    added = apply_quality_bars(task, Policy())

    assert any(c.method is VerifyMethod.TEST for c in added),         "the test bar was suppressed by the phrase 'test data'"
    assert any("weakness" in c.statement for c in added),         "the security bar was suppressed by the phrase 'security cameras'"


def test_task_is_not_done_until_every_mandatory_criterion_passes() -> None:
    task = ExecutionTask(
        title="t",
        dod=[
            DoDCriterion(statement="a", status=CriterionStatus.PASS),
            DoDCriterion(statement="b", status=CriterionStatus.UNVERIFIED),
            DoDCriterion(statement="c", mandatory=False),
        ],
    )
    assert not task.dod_satisfied()
    assert [c.statement for c in task.unmet_criteria()] == ["b"]

    task.dod[1].status = CriterionStatus.WAIVED
    assert task.dod_satisfied()


def test_inspection_criterion_reads_the_real_file(workspace: Path) -> None:
    passing = DoDCriterion(
        statement="the limiter keys on the account",
        method=VerifyMethod.INSPECTION,
        expect="src/auth/login.py: account_key",
    )
    failing = DoDCriterion(
        statement="the limiter uses redis",
        method=VerifyMethod.INSPECTION,
        expect="src/auth/login.py: redis_client",
    )
    escaping = DoDCriterion(
        statement="reads outside the workspace",
        method=VerifyMethod.INSPECTION,
        expect="../../../etc/passwd: root",
    )

    assert verify_inspection(passing, workspace).status is CriterionStatus.PASS
    assert verify_inspection(failing, workspace).status is CriterionStatus.FAIL
    assert verify_inspection(escaping, workspace).status is CriterionStatus.BLOCKED


# --------------------------------------------------------------------------
# Blackboard
# --------------------------------------------------------------------------


def test_message_to_an_unknown_agent_is_broadcast_not_dropped() -> None:
    state = RunState(prompt=PROMPT)
    state.agents["agt_a"] = AgentSpec(id="agt_a", role="security")
    state.agents["agt_b"] = AgentSpec(id="agt_b", role="technical")

    board = Blackboard(state.id)
    routing = board.route(
        Message(sender="agt_a", recipient="agt_ghost", kind=MessageKind.WARNING,
                content="the limiter must key on account"),
        state,
    )

    assert routing.deliver_to == ["agt_b"]
    assert "unknown agent" in routing.message.supervisor_note
    assert routing.escalate, "warnings are surfaced to the supervisor"


def test_inbox_excludes_the_sender_and_delivered_messages() -> None:
    state = RunState(prompt=PROMPT)
    state.agents["agt_a"] = AgentSpec(id="agt_a")
    state.agents["agt_b"] = AgentSpec(id="agt_b")
    state.messages = [
        Message(id="m1", sender="agt_a", recipient="*", content="one"),
        Message(id="m2", sender="agt_b", recipient="*", content="two"),
        Message(id="m3", sender="agt_a", recipient="agt_b", content="three",
                delivered_to=["agt_b"]),
    ]

    inbox = Blackboard.inbox_for("agt_b", state)
    assert [m.id for m in inbox] == ["m1"]


def test_a_broadcast_reaches_every_recipient_not_just_the_first() -> None:
    """Delivery is tracked per recipient, so one agent cannot consume a broadcast."""
    state = RunState(prompt=PROMPT)
    for aid in ("agt_a", "agt_b", "agt_c"):
        state.agents[aid] = AgentSpec(id=aid)
    state.messages = [Message(id="m1", sender="agt_a", recipient="*", content="heads up")]

    assert [m.id for m in Blackboard.inbox_for("agt_b", state)] == ["m1"]
    assert [m.id for m in Blackboard.inbox_for("agt_c", state)] == ["m1"]

    # agt_b receives it; agt_c must still be able to.
    state.messages[0].delivered_to.append("agt_b")
    assert Blackboard.inbox_for("agt_b", state) == []
    assert [m.id for m in Blackboard.inbox_for("agt_c", state)] == ["m1"]
    # And never back to its sender.
    assert Blackboard.inbox_for("agt_a", state) == []


def _route_and_fold(state: RunState, message: Message) -> RunState:
    """Route a message the way the supervisor does, then rebuild state from the log.

    The routing decision is only real if it survives the round trip through the
    event log: the supervisor emits ``MESSAGE_SENT`` and every reader -- resume,
    audit, the next turn's inbox -- sees the folded state, not the ``Routing``.
    """
    from supervisor_harness.serde import to_jsonable
    from supervisor_harness.store.events import Event, EventType, fold

    routing = Blackboard(state.id).route(message, state)
    event = Event(
        seq=1,
        run_id=state.id,
        type=EventType.MESSAGE_SENT,
        actor=message.sender,
        payload={"message": to_jsonable(routing.message),
                 "deliver_to": routing.deliver_to,
                 "escalated": routing.escalate},
    )
    return fold([event], state)


def test_a_message_to_an_unknown_agent_is_still_delivered_after_the_fold() -> None:
    """The broadening survives persistence; deliver_to alone would be discarded."""
    state = RunState(prompt=PROMPT)
    for aid in ("agt_a", "agt_b", "agt_c"):
        state.agents[aid] = AgentSpec(id=aid)

    folded = _route_and_fold(
        state,
        Message(id="m1", sender="agt_a", recipient="agt_ghost",
                kind=MessageKind.WARNING, content="the limiter must key on account"),
    )

    assert [m.id for m in folded.pending_messages("agt_b")] == ["m1"]
    assert [m.id for m in folded.pending_messages("agt_c")] == ["m1"]
    assert folded.pending_messages("agt_a") == [], "never back to its sender"
    assert "agt_ghost" in folded.messages[0].supervisor_note


def test_a_message_to_a_known_agent_reaches_only_that_inbox_after_the_fold() -> None:
    state = RunState(prompt=PROMPT)
    for aid in ("agt_a", "agt_b", "agt_c"):
        state.agents[aid] = AgentSpec(id=aid)

    folded = _route_and_fold(
        state,
        Message(id="m1", sender="agt_a", recipient="agt_b", content="just for you"),
    )

    assert [m.id for m in Blackboard.inbox_for("agt_b", folded)] == ["m1"]
    assert Blackboard.inbox_for("agt_c", folded) == []
    assert folded.messages[0].supervisor_note == ""


def test_contradictions_between_lenses_are_detected() -> None:
    findings = [
        Finding(lens="security", title="Session cookie is not secure",
                detail="The session cookie is missing the Secure flag and is not protected",
                confidence=0.9),
        Finding(lens="technical", title="Session cookie is secure",
                detail="The session cookie is protected and validated, flag handled correctly",
                confidence=0.9),
        Finding(lens="quality", title="Tests are missing", detail="No coverage", confidence=0.9),
    ]
    conflicts = detect_contradictions(findings)

    assert len(conflicts) == 1
    assert "security" in conflicts[0] and "technical" in conflicts[0]


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_stage_routing_falls_back_from_specific_to_general() -> None:
    cfg = default_config()
    cfg.routing["analysis"] = "ollama:qwen3.8-code:latest|host"
    cfg.routing["analysis.security"] = "openrouter:anthropic/claude-opus-4.1"

    assert cfg.binding_for("analysis.security").ref() == "openrouter:anthropic/claude-opus-4.1"
    assert cfg.binding_for("analysis.architecture").ref() == "ollama:qwen3.8-code:latest"
    assert cfg.binding_for("analysis.architecture").fallbacks == ["host"]
    assert cfg.binding_for("something.unknown").ref() == "host"


def test_config_layers_merge(tmp_path: Path, monkeypatch) -> None:
    from supervisor_harness.config import load_config

    (tmp_path / "supervisor.config.json").write_text(
        '{"policy": {"require_tests": false, "max_parallel_agents": 9},'
        ' "routing": {"drift": "ollama:qwen3.5:9b"}}',
        encoding="utf-8",
    )
    monkeypatch.delenv("SUPERVISOR_HOME", raising=False)
    cfg = load_config(tmp_path)

    assert cfg.policy.require_tests is False
    assert cfg.policy.max_parallel_agents == 9
    assert cfg.policy.require_security_review is True     # untouched default survives
    assert cfg.binding_for("drift").ref() == "ollama:qwen3.5:9b"


# --------------------------------------------------------------------------
# Verification authority
# --------------------------------------------------------------------------


def test_test_command_is_detected_from_the_project(tmp_path: Path) -> None:
    """An inserted test criterion gets a runnable command, not an empty one."""
    from supervisor_harness.core.dod import detect_test_command

    (tmp_path / "empty").mkdir()
    assert detect_test_command(tmp_path / "empty") == ""

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert detect_test_command(tmp_path) == "pytest -q"

    node = tmp_path / "node"
    node.mkdir()
    (node / "package.json").write_text('{"scripts": {"test": "vitest"}}', encoding="utf-8")
    assert detect_test_command(node) == "npm test --silent"

    task = ExecutionTask(title="Add a limiter", action="Implement it in src/auth/login.py")
    added = apply_quality_bars(task, Policy(), tmp_path)
    test_criterion = next(c for c in added if c.method is VerifyMethod.TEST)
    assert test_criterion.command == "pytest -q"


async def test_mechanical_verdict_outranks_an_agent_claim(
    supervisor: Supervisor, fake
) -> None:
    """An agent cannot talk a failing mechanical check into a pass."""
    fake.overrides["synthesis"] = {
        "summary": "The limiter is missing.",
        "recommended_mode": "execute",
        "tasks": [
            {
                "title": "Add a limiter to src/auth/login.py",
                "action": "Add a Redis-backed limiter in src/auth/login.py",
                "motivation": "Login is unthrottled.",
                "dod": [
                    {
                        "statement": "src/auth/login.py defines a rate_limit decorator",
                        "method": "inspection",
                        # The fixture's login.py contains no such symbol, so the
                        # harness can prove this false by reading the file.
                        "expect": "src/auth/login.py: rate_limit",
                        "mandatory": True,
                    }
                ],
                "scope_paths": ["src/auth/**"],
                "suggested_role": "implementer",
            }
        ],
    }

    response = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    while response.action != "await_approval":
        response = await supervisor.advance(response.run_id)

    task_id = response.tasks[0]["id"]
    criterion_id = next(
        c["id"] for c in response.tasks[0]["definition_of_done"]
        if c["method"] == "inspection"
    )
    # The verification agent insists everything passed, with confident evidence.
    fake.overrides["verification"] = {
        "results": [
            {"criterion_id": criterion_id, "status": "pass",
             "evidence": "I reviewed the file and the decorator is present."}
        ],
        "summary": "all good",
    }

    await supervisor.approve(response.run_id, [{"task_id": task_id, "decision": "approve"}])
    state = supervisor.store.load_state(response.run_id)
    criterion = next(c for c in state.tasks[task_id].dod if c.id == criterion_id)

    assert criterion.status is CriterionStatus.FAIL
    assert criterion.verified_by == "harness"
    assert "does not contain" in criterion.evidence
    assert not state.tasks[task_id].dod_satisfied()


# --------------------------------------------------------------------------
# Phase settlement: every path out of an agent has to be terminal
# --------------------------------------------------------------------------


async def test_turn_budget_exhaustion_leaves_no_agent_active(
    supervisor: Supervisor, fake
) -> None:
    """An agent whose supervised loop ends without a terminal directive still settles.

    ``Budget.exhausted`` reads ``max_turns=0`` as unlimited, so no STOP directive
    is ever issued, and the driving loop runs zero turns. Before this was closed
    the agent stayed in ``ACTIVE_AGENT_STATUSES`` and the phase drove it again on
    every advance until the run was failed for not settling.
    """
    from supervisor_harness.models import ACTIVE_AGENT_STATUSES, AgentKind
    from supervisor_harness.store.events import EventType

    supervisor.config.policy.default_max_turns = 0

    response = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    for _ in range(20):
        if response.action in ("complete", "failed", "await_approval"):
            break
        response = await supervisor.advance(response.run_id)

    assert response.action != "failed", response.message
    assert "did not settle" not in response.message

    session = supervisor.store.open(response.run_id)
    analysts = [a for a in session.state.agents.values() if a.kind is AgentKind.ANALYSIS]
    assert analysts, "the run should have spawned analysis agents"
    assert all(a.status not in ACTIVE_AGENT_STATUSES for a in analysts)

    notes = [e for e in session.events() if e.type is EventType.NOTE]
    exhausted = [e for e in notes if "turn budget" in e.payload.get("text", "")]
    assert exhausted, "budget exhaustion must be on the log"
    assert {e.actor for e in exhausted} >= {a.id for a in analysts}


async def test_autonomous_run_fails_on_a_host_routed_stage(
    workspace: Path, config, fake
) -> None:
    """A stage routed to the host is named, not spun on, in an autonomous run."""
    from supervisor_harness.host.detect import HostInfo
    from supervisor_harness.providers.router import ModelRouter
    from supervisor_harness.store.runstore import RunStore

    config.routing["synthesis"] = "host"
    store = RunStore(workspace / ".supervisor")
    host = HostInfo(name="test-host", workspace=str(workspace), confidence=1.0)
    router = ModelRouter(config, host_name=host.name)
    router.register("fake", fake)
    supervisor = Supervisor(workspace=workspace, config=config, store=store,
                            host=host, router=router)

    with pytest.raises(ValueError, match="synthesis"):
        await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)

    # The same configuration reaching the loop -- because the precondition was
    # bypassed, or the routing changed mid-run -- ends the run instead of
    # re-dispatching a packet nothing here can execute.
    def no_host_stages() -> list[str]:
        return []

    supervisor._host_routed_stages = no_host_stages
    response = await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)

    assert response.action == "failed"
    assert "cannot consume" in response.message
    assert "did not settle" not in response.message
