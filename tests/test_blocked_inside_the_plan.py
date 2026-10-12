"""An implementer blocked on a file it may in fact change is told so, once.

The first Minesweeper run to reach its last task: the agent writing the browser
tests found the bug failing eight of them -- cells with no `data-state` until
their first change, at `src/main.ts:156`, a file of a task already verified --
and reported itself blocked, because the file was outside its scope and its
rules said a file outside its scope was a reason to stop. The harness would
have let it write there: `src/main.ts` was in the run's plan and no other agent
was working on it. Granted by the owner with a note saying exactly that, the
next agent changed the file at once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from supervisor_harness.agents.brief import build_implementer_brief
from supervisor_harness.core.conversation import IMPLEMENTER_SYSTEM
from supervisor_harness.core.supervisor import SENT_BACK_BLOCKED, Supervisor
from supervisor_harness.core.tools import Toolbox
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    AgentStatus,
    Escalation,
    EscalationReason,
    ExecutionTask,
    RunMode,
    Scope,
    TaskStatus,
)
from supervisor_harness.providers.base import ToolCall

from .test_implementer_conversation import (
    DONE,
    WRITE_CACHE,
    NativeFake,
    _calls,
    _conversing,
    _scoped_to_auth,
    _writer,
)
from .test_send_back_criteria import PROMPT

BLOCKED = ToolCall("report", {
    "status": "blocked", "summary": "the limiter needs a constant I may not add",
    "blocked_on": "src/cache.py:1 has no LIMIT, and src/cache.py is outside my scope"})


def _cache(workspace: Path) -> None:
    (workspace / "src" / "cache.py").write_text("STORE = {}\n", encoding="utf-8")


def _box(supervisor: Supervisor) -> Toolbox:
    return Toolbox(supervisor.workspace, supervisor.config.policy)


# -- what may be handed back ----------------------------------------------------------


def test_a_block_on_a_file_in_the_plan_is_within_reach(supervisor: Supervisor) -> None:
    _cache(supervisor.workspace)
    session, me = _writer(supervisor, "run_R")

    assert supervisor._blocked_within_reach(session.state, me, BLOCKED.arguments,
                                            _box(supervisor)) == ["src/cache.py"]


def test_what_is_not_handed_back(supervisor: Supervisor) -> None:
    _cache(supervisor.workspace)
    (supervisor.workspace / "scripts").mkdir()
    (supervisor.workspace / "scripts" / "build.py").write_text("", encoding="utf-8")
    session, me = _writer(supervisor, "run_N")
    box = _box(supervisor)

    def reach(**report: Any) -> list[str]:
        return supervisor._blocked_within_reach(session.state, me, report, box)

    assert reach(status="done", summary="src/cache.py") == [], "only a blocked report"
    assert reach(status="blocked", blocked_on="a missing dependency: redis") == []
    assert reach(status="blocked", blocked_on="src/nowhere.py is missing") == [], (
        "only files that are there")
    assert reach(status="blocked", blocked_on="scripts/build.py is outside my scope") == [], (
        "outside the run's plan: the owner's")
    assert reach(status="blocked", blocked_on="src/cache.py and scripts/build.py") == [], (
        "all of it within reach, or none of it handed back")
    assert reach(status="blocked", blocked_on="src/auth/login.py is wrong") == [], (
        "already in its scope: blocked on something else")


def test_not_where_another_agent_is_working_or_a_task_is_held(supervisor: Supervisor) -> None:
    _cache(supervisor.workspace)
    session, me = _writer(supervisor, "run_P")
    peer = AgentSpec(id="agt_peer", kind=AgentKind.EXECUTION, scope=Scope(paths=["src/cache.py"]),
                     status=AgentStatus.RUNNING)
    session.state.agents[peer.id] = peer
    box = _box(supervisor)

    assert supervisor._blocked_within_reach(session.state, me, BLOCKED.arguments, box) == []

    peer.status = AgentStatus.DONE
    held = ExecutionTask(title="Rework the cache", scope=Scope(paths=["src/cache.py"]),
                         status=TaskStatus.BLOCKED)
    session.state.tasks[held.id] = held
    escalation = Escalation(reason=EscalationReason.REVIEW_VETO, task_id=held.id)
    session.state.escalations[escalation.id] = escalation

    assert supervisor._blocked_within_reach(session.state, me, BLOCKED.arguments, box) == []


def test_once_per_agent_read_from_the_log(supervisor: Supervisor) -> None:
    _cache(supervisor.workspace)
    session, me = _writer(supervisor, "run_O")
    session.note(f"{SENT_BACK_BLOCKED}: earlier", actor=me.id)

    assert supervisor._blocked_within_reach(session.state, me, BLOCKED.arguments,
                                            _box(supervisor)) == []


# -- in the conversation -------------------------------------------------------------


async def test_handed_back_it_fixes_the_file_and_nothing_goes_to_the_owner(
    supervisor: Supervisor, workspace: Path,
) -> None:
    _cache(workspace)
    fake = NativeFake(_calls(BLOCKED), _calls(WRITE_CACHE), _calls(DONE))
    _scoped_to_auth(fake)

    response = await _conversing(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE,
                                                       auto_approve=True)
    state = supervisor.store.load_state(response.run_id)
    (task,) = state.tasks.values()

    assert state.escalations == {}, "nothing went to the owner"
    answered = [m.content for m in fake.implementer[1].messages if m.role == "tool"]
    assert any("`src/cache.py` is within the run's plan" in a for a in answered), answered
    assert (workspace / "src" / "cache.py").read_text(encoding="utf-8") == "LIMIT = 10\n"
    assert "src/cache.py" in task.scope.paths
    assert any(n.text.startswith(SENT_BACK_BLOCKED) for n in state.notes)


async def test_blocked_again_it_goes_to_the_owner_as_before(
    supervisor: Supervisor, workspace: Path,
) -> None:
    _cache(workspace)
    fake = NativeFake(_calls(BLOCKED), _calls(BLOCKED))
    _scoped_to_auth(fake)

    response = await _conversing(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE,
                                                       auto_approve=True)
    state = supervisor.store.load_state(response.run_id)

    (escalation,) = state.escalations.values()
    assert escalation.reason is EscalationReason.AGENT_BLOCKED
    assert "src/cache.py" in escalation.detail


async def test_beyond_the_plan_it_goes_to_the_owner_at_once(
    supervisor: Supervisor, workspace: Path,
) -> None:
    _cache(workspace)
    fake = NativeFake(_calls(BLOCKED))
    _scoped_to_auth(fake)
    supervisor.config.policy.scope_envelope = ["src/auth/**", "tests/**"]

    response = await _conversing(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE,
                                                       auto_approve=True)
    state = supervisor.store.load_state(response.run_id)

    assert len(state.escalations) == 1
    assert not any(n.text.startswith(SENT_BACK_BLOCKED) for n in state.notes)


# -- what the implementer is told ------------------------------------------------------


def test_the_rules_and_the_brief_say_what_the_harness_allows(supervisor: Supervisor) -> None:
    session, me = _writer(supervisor, "run_B")
    (task,) = session.state.tasks.values()

    brief = build_implementer_brief(session.state, me, task)

    assert "The run's plan covers: `src/`, `tests/`" in brief
    assert "a bug there that keeps your task from passing is yours to fix" in brief
    assert "a bug in another file of the run's plan" in IMPLEMENTER_SYSTEM
    assert "a file outside the run's plan" in IMPLEMENTER_SYSTEM
    assert "Change only files inside your scope" not in IMPLEMENTER_SYSTEM


def test_an_agent_with_the_whole_workspace_is_not_told_of_a_plan(supervisor: Supervisor) -> None:
    session, me = _writer(supervisor, "run_W2")
    me.scope.paths = []
    (task,) = session.state.tasks.values()

    assert "The run's plan covers" not in build_implementer_brief(session.state, me, task)
