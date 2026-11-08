"""Blocking work leaves the event loop (review finding 5).

Each of these ran project tests, an install or an agent's shell command
synchronously inside a coroutine, so every other coroutine waited for it.
"""

from __future__ import annotations

import threading

import pytest

from supervisor_harness.core.conversation import Stint
from supervisor_harness.core.review import ROLE as REVIEW_ROLE
from supervisor_harness.core.review import RULING_TOOL
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.core.tools import Toolbox
from supervisor_harness.models import AgentKind, AgentSpec, Phase, RunState, Scope
from supervisor_harness.providers.base import CompletionRequest, CompletionResponse, ToolCall

from .conftest import FakeProvider


async def test_mechanical_verification_runs_off_the_event_loop(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_thread = threading.get_ident()
    seen: list[int] = []
    monkeypatch.setattr(supervisor, "_verify_mechanically",
                        lambda session: seen.append(threading.get_ident()))
    session = supervisor.store.create(RunState(id="run_Loop", prompt="p", phase=Phase.VERIFYING))

    await supervisor._continue_verification(session)

    assert seen and seen[0] != loop_thread


async def test_closing_the_worktree_runs_off_the_event_loop(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_thread = threading.get_ident()
    seen: list[int] = []
    monkeypatch.setattr(supervisor, "_close_worktree",
                        lambda session: seen.append(threading.get_ident()))
    session = supervisor.store.create(RunState(id="run_Close", prompt="p", phase=Phase.IMPROVING))

    await supervisor._run_improvement(session)

    assert seen and seen[0] != loop_thread


class RunsACommand(FakeProvider):
    native_tools = True

    def __init__(self) -> None:
        super().__init__()
        self.sent = False

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if not any(t["name"] == "ruling" for t in request.tools or []):
            return await super().complete(request)
        if not self.sent:
            self.sent = True
            return CompletionResponse(tool_calls=[ToolCall("read_file", {"path": "x"}, id="1")])
        return CompletionResponse(tool_calls=[ToolCall("ruling", {"verdict": "proceed"}, id="2")])


async def test_an_agents_tool_call_runs_off_the_event_loop(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = Toolbox.call

    def spy(self: Toolbox, *args: object, **kwargs: object):
        seen.append(threading.get_ident())
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Toolbox, "call", spy)
    supervisor.router.register("fake", RunsACommand())
    session = supervisor.store.create(RunState(id="run_Tool", prompt="p"))
    reader = AgentSpec(id="r", kind=AgentKind.ANALYSIS, role="reviewer", title="r", scope=Scope(),
                      binding=supervisor.config.binding_for("review"))
    tools = [{"name": "read_file", "description": "", "parameters": {"type": "object",
             "properties": {"path": {"type": "string"}}}}, RULING_TOOL]

    await supervisor._read_then_finish(
        session, reader, [], tools, Stint(), finish="ruling", now="go",
        role=REVIEW_ROLE, stage="review")

    assert seen and all(t != loop_thread for t in seen)


async def test_aclose_closes_the_store_even_if_the_external_tools_fail(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[str] = []

    async def boom() -> None:
        raise RuntimeError("tool server would not stop")

    async def router_close() -> None:
        closed.append("router")

    monkeypatch.setattr(supervisor.external, "aclose", boom)
    monkeypatch.setattr(supervisor.router, "aclose", router_close)
    monkeypatch.setattr(supervisor.store, "close", lambda: closed.append("store"))
    supervisor._owns_store = True

    with pytest.raises(RuntimeError):
        await supervisor.aclose()

    assert closed == ["router", "store"]
