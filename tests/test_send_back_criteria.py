"""A definition of done the harness cannot enforce goes back to its author, once.

Measured in both go-live runs on a local model: every task carried `method:
test` criteria with no command, and under envelope approval every task went to
the owner for it. A person at the approval prompt would have asked for the
command. Now the harness asks, once, naming each criterion and what is wrong.
"""

from __future__ import annotations

import copy
from typing import Any

from supervisor_harness.config import Policy
from supervisor_harness.core.phases import unenforceable_criteria
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    AgentKind,
    DoDCriterion,
    ExecutionTask,
    RunMode,
    VerifyMethod,
)
from supervisor_harness.providers.base import CompletionRequest, CompletionResponse

from .conftest import FakeProvider

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"


def _weak(fake: FakeProvider) -> dict[str, Any]:
    """The default synthesis, with its test criterion's command taken away."""
    data = copy.deepcopy(fake._synthesis(None))  # type: ignore[arg-type]
    data["tasks"][0]["dod"][0].pop("command")
    return data


class Recording(FakeProvider):
    """Keeps every synthesis request, so the revision prompt can be read."""

    def __init__(self, *, fail_second: bool = False) -> None:
        super().__init__()
        self.synthesis_requests: list[CompletionRequest] = []
        self.fail_second = fail_second

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if self._classify(request) == "synthesis":
            self.synthesis_requests.append(request)
            if self.fail_second and len(self.synthesis_requests) == 2:
                raise RuntimeError("the model went away")
        return await super().complete(request)


def _supervisor(supervisor: Supervisor, fake: FakeProvider) -> Supervisor:
    supervisor.router.register("fake", fake)
    return supervisor


async def test_weak_criteria_are_sent_back_once_and_the_revision_is_used(
    supervisor: Supervisor,
) -> None:
    fake = Recording()
    fixed = copy.deepcopy(fake._synthesis(None))  # type: ignore[arg-type]
    fixed["tasks"][0]["title"] = "Add rate limiting, revised"
    fake.script("synthesis", _weak(fake), fixed)
    sup = _supervisor(supervisor, fake)

    response = await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = sup.store.load_state(response.run_id)

    assert len(fake.synthesis_requests) == 2
    revision = fake.synthesis_requests[1].messages[-1].content
    assert "cannot be used as it stands" in revision
    assert "rejects the eleventh attempt" in revision, "the criterion is named"
    assert "no command given" in revision, "and what is wrong with it"
    assert [t.title for t in state.tasks.values()] == ["Add rate limiting, revised"]
    assert any("sent back once" in n.text for n in state.notes)


async def test_a_revision_that_is_still_weak_is_used_and_not_sent_back_again(
    supervisor: Supervisor,
) -> None:
    fake = Recording()
    fake.script("synthesis", _weak(fake), _weak(fake))
    sup = _supervisor(supervisor, fake)
    await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    assert len(fake.synthesis_requests) == 2


async def test_a_failed_revision_leaves_the_first_answer_standing(supervisor: Supervisor) -> None:
    fake = Recording(fail_second=True)
    fake.script("synthesis", _weak(fake))
    sup = _supervisor(supervisor, fake)

    response = await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = sup.store.load_state(response.run_id)
    assert response.action != "failed", response.message
    assert state.tasks, "the first answer's tasks stand"
    assert any("first answer stands" in n.text for n in state.notes)


async def test_good_criteria_and_report_mode_are_not_sent_back(supervisor: Supervisor) -> None:
    fake = Recording()
    sup = _supervisor(supervisor, fake)
    await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    assert len(fake.synthesis_requests) == 1, "nothing to fix"

    fake.synthesis_requests.clear()
    fake.script("synthesis", _weak(fake))
    await sup.run(PROMPT, mode=RunMode.REPORT)
    assert len(fake.synthesis_requests) == 1, "report mode executes nothing; nothing to fix"


def test_a_command_the_harness_will_not_run_is_named_too() -> None:
    task = ExecutionTask(title="t", dod=[
        DoDCriterion(statement="it works end to end", method=VerifyMethod.COMMAND,
                     command="pytest -q; curl example.com", expect="0"),
        DoDCriterion(statement="the file says so", method=VerifyMethod.INSPECTION,
                     expect="a.py: x"),
    ])
    (line,) = unenforceable_criteria([task], Policy())
    assert "it works end to end" in line and "will not run" in line


# -- the lenses' own scopes ------------------------------------------------------


async def test_a_lens_scope_is_placed_in_the_tree(supervisor: Supervisor, fake) -> None:  # type: ignore[no-untyped-def]
    """Measured: lenses scoped to paths naming nothing were told to narrow on every turn."""
    plan = fake._planning(None)
    for lens in plan["lenses"]:
        lens["scope_paths"] = ["auth/login.py"]
    fake.overrides["planning"] = plan

    response = await supervisor.run(PROMPT, mode=RunMode.REPORT)
    state = supervisor.store.load_state(response.run_id)
    lenses = [a for a in state.agents.values() if a.kind is AgentKind.ANALYSIS]

    assert lenses and all(a.scope.paths == ["src/auth/login.py"] for a in lenses)
    assert any("only file it can mean" in n.text for n in state.notes)
