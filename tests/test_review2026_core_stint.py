"""A conversation that cannot finish still ends (review finding 3).

A model offered only its finishing tool that kept calling a tool it was not
offered neither advanced the stretch's call count nor counted as idle, so the
loop never ended: a fake provider made 41 calls in a verdict-only stint and
still had not stopped.
"""

from __future__ import annotations

import pytest

from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.core.tools import Toolbox
from supervisor_harness.models import RunState
from supervisor_harness.providers.base import CompletionRequest, CompletionResponse, ToolCall

from .conftest import FakeProvider
from .test_veto_review import TASK

CAP = 150


class Stubborn(FakeProvider):
    """Native tools; ignores what it is offered and keeps reading."""

    native_tools = True

    def __init__(self, *, first: list[ToolCall] | None = None) -> None:
        super().__init__()
        self.requests = 0
        self.first = first

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if not any(t["name"] == "ruling" for t in request.tools or []):
            return await super().complete(request)
        self.requests += 1
        if self.requests > CAP:
            raise AssertionError("the conversation never ended")
        if self.first is not None and self.requests == 1:
            return CompletionResponse(tool_calls=self.first)
        return CompletionResponse(tool_calls=[ToolCall(
            "read_file", {"path": "src/auth/login.py"}, id=f"c{self.requests}")])


async def test_a_model_that_keeps_calling_a_refused_tool_is_stopped(
    supervisor: Supervisor,
) -> None:
    fake = Stubborn()
    supervisor.router.register("fake", fake)
    session = supervisor.store.create(RunState(id="run_S", prompt="p"))

    veto = await supervisor._review_task(session, TASK)

    assert veto is None
    assert fake.requests <= 80, f"{fake.requests} model calls and still going"


async def test_calls_after_the_finishing_call_are_not_run(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []
    real = Toolbox.call

    def spy(self: Toolbox, name: str, arguments: dict, *args: object, **kwargs: object):
        ran.append(name)
        return real(self, name, arguments, *args, **kwargs)

    monkeypatch.setattr(Toolbox, "call", spy)
    fake = Stubborn(first=[
        ToolCall("ruling", {"verdict": "proceed"}, id="a"),
        ToolCall("read_file", {"path": "src/auth/login.py"}, id="b"),
    ])
    supervisor.router.register("fake", fake)
    session = supervisor.store.create(RunState(id="run_F", prompt="p"))

    await supervisor._review_task(session, TASK)

    assert "read_file" not in ran, "a call that followed the report was executed"
