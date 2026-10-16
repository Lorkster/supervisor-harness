"""Batch K, part 1: agents in the conversation loop can tell the supervisor things again.

The blackboard routes agents' messages through the supervisor -- question,
finding, warning, contradiction, handoff -- and delivers each agent's inbox with
its next directive. That traffic was carried in the turn contract's JSON; batch
I's conversation loop offered file and command tools and `report`, so in the
first Minesweeper run to reach its last task no agent sent anything, and the
one that found a bug in an earlier task's file could only report itself blocked.

Where a finding goes is the coordinator's to decide (batch O,
`tests/test_coordinator.py`).
"""

from __future__ import annotations

from pathlib import Path

from supervisor_harness.config import Policy
from supervisor_harness.core.conversation import (
    IMPLEMENTER_SYSTEM,
    MESSAGE_TOOL,
    Stint,
    native_tool_specs,
    stint_payload,
)
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import AgentKind, AgentSpec, MessageKind, RunMode
from supervisor_harness.providers.base import ToolCall

from .test_implementer_conversation import DONE, NativeFake, _calls, _conversing
from .test_send_back_criteria import PROMPT

TELL = ToolCall("message", {
    "to": "supervisor", "kind": "warning", "subject": "earlier task's bug",
    "content": "src/auth/login.py:3 never sets data-state on the first render"})


def test_implementers_and_verifiers_may_send_and_lenses_may_not() -> None:
    def names(kind: AgentKind) -> list[str]:
        return [t["name"] for t in native_tool_specs(AgentSpec(kind=kind), Policy())]

    assert "message" in names(AgentKind.EXECUTION)
    assert "message" in names(AgentKind.VERIFICATION)
    assert "message" not in names(AgentKind.ANALYSIS), "the planner and lenses answer otherwise"
    assert names(AgentKind.EXECUTION)[-1] == "report", "the finish call stays last"
    assert MESSAGE_TOOL["parameters"]["required"] == ["to", "kind", "content"]


def test_a_stretch_keeps_what_it_sent_for_its_turn() -> None:
    stint = Stint()
    assert stint.send({"to": "", "kind": "question", "content": "which locale wins?"}).startswith(
        "Sent.")
    assert stint.send({"to": "agt_x", "kind": "finding", "content": "  "}).startswith("Not sent")

    assert stint.messages == [{"recipient": "supervisor", "kind": "question", "subject": "",
                               "content": "which locale wins?"}], "no recipient: the supervisor"
    assert stint_payload(stint, None)["messages"] == stint.messages
    assert stint_payload(stint, {"status": "done", "summary": "s"})["messages"] == stint.messages


def test_the_rules_say_when_to_use_it() -> None:
    assert "tell the supervisor with message" in " ".join(IMPLEMENTER_SYSTEM.split())


async def test_a_message_reaches_the_blackboard_and_the_work_goes_on(
    supervisor: Supervisor, workspace: Path,
) -> None:
    fake = NativeFake(_calls(TELL), _calls(DONE))
    response = await _conversing(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE,
                                                       auto_approve=True)
    state = supervisor.store.load_state(response.run_id)

    (message,) = [m for m in state.messages if m.kind is MessageKind.WARNING]
    implementer = next(a for a in state.agents.values() if a.kind is AgentKind.EXECUTION)
    assert (message.sender, message.recipient) == (implementer.id, "supervisor")
    assert "never sets data-state" in message.content
    replies = [m.content for m in fake.implementer[1].messages if m.role == "tool"]
    assert any(r.startswith("Sent. The supervisor routes it") for r in replies), replies
    (task,) = state.tasks.values()
    assert task.status.value == "verified", "sending is not finishing, or failing"


async def test_a_verifier_may_send_too(supervisor: Supervisor) -> None:
    from supervisor_harness.providers.base import CompletionRequest, CompletionResponse

    class TellingVerifier(NativeFake):
        told = False

        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            names = {t["name"] for t in request.tools or []}
            if "verdict" in names and not self.told:
                self.told = True
                return _calls(ToolCall("message", {"to": "*", "kind": "finding",
                                                   "content": "the board has one column"}))(request)
            return await super().complete(request)

    fake = TellingVerifier(_calls(DONE))
    response = await _conversing(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE,
                                                       auto_approve=True)
    state = supervisor.store.load_state(response.run_id)

    (message,) = [m for m in state.messages if m.content == "the board has one column"]
    verifier = next(a for a in state.agents.values() if a.kind is AgentKind.VERIFICATION)
    assert (message.sender, message.kind) == (verifier.id, MessageKind.FINDING)
