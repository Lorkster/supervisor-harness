"""An implementer driven as one conversation, through native tool calls.

From go-live runs 4-9 (a local model): every supervised turn of the old loop
started from the brief, the agent's last answer and the directive -- whatever
it had read was gone -- and every step had to be one grammar-constrained JSON
object. Implementers read files again each turn until they were stopped for
repeating themselves, and one wrote a translation file back from the first
page it had seen. The same model does well under Turnstone, which keeps one
conversation per agent and uses the provider's own tool calling.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from supervisor_harness.core.conversation import (
    CHECKPOINT_CALLS,
    COMPACTED,
    IMPLEMENTER_SYSTEM,
    KEEP_RECENT_RESULTS,
    NUDGE,
    Stint,
    compact,
    native_tool_specs,
    stint_payload,
)
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import AgentKind, AgentSpec, Resolution, RunMode, Usage
from supervisor_harness.providers.base import (
    ChatMessage,
    CompletionRequest,
    CompletionResponse,
    ToolCall,
)

from .conftest import FakeProvider
from .test_send_back_criteria import PROMPT

Step = Callable[[CompletionRequest], CompletionResponse]


class NativeFake(FakeProvider):
    """The fake, with native tools: an implementer's requests are answered from a script."""

    native_tools = True

    def __init__(self, *steps: Step, verdict: str = "pass") -> None:
        super().__init__()
        self.steps = list(steps)
        self.verdict = verdict
        self.implementer: list[CompletionRequest] = []
        self.verifier: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        names = {t["name"] for t in request.tools or []}
        if "verdict" in names:
            self.verifier.append(request)
            ids = re.findall(r"`(dod_[A-Za-z0-9]+)`", request.messages[0].content)
            return _calls(ToolCall("verdict", {"results": [
                {"criterion_id": i, "status": self.verdict, "evidence": "src/auth/login.py:3"}
                for i in ids]}))(request)
        if "report" not in names:
            return await super().complete(request)
        self.implementer.append(request)
        if self.steps:
            return self.steps.pop(0)(request)
        return _calls(ToolCall("report", {"status": "done", "summary": "done"}))(request)


def _calls(*calls: ToolCall, text: str = "") -> Step:
    return lambda _request: CompletionResponse(
        text=text, tool_calls=list(calls), usage=Usage(input_tokens=10, output_tokens=5))


def _say(text: str) -> Step:
    return lambda _request: CompletionResponse(text=text, usage=Usage(input_tokens=10))


READ = ToolCall("read_file", {"path": "src/auth/login.py"})
EDIT = ToolCall("edit_file", {
    "path": "src/auth/login.py", "old": "    return check(account_key)\n",
    "new": "    rate_limit(account_key)\n    return check(account_key)\n"})
DONE = ToolCall("report", {"status": "done", "summary": "added the limiter call",
                           "criteria": [{"criterion_id": "x", "claim": "met",
                                         "evidence": "src/auth/login.py:4"}]})


def _conversing(supervisor: Supervisor, fake: NativeFake) -> Supervisor:
    supervisor.config.policy.implementer_loop = "conversation"
    supervisor.router.register("fake", fake)
    return supervisor


async def test_an_implementer_reads_edits_and_reports_in_one_conversation(
    supervisor: Supervisor, workspace: Path,
) -> None:
    fake = NativeFake(_calls(READ), _calls(EDIT), _calls(DONE))
    response = await _conversing(supervisor, fake).run(
        PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = supervisor.store.load_state(response.run_id)

    assert "rate_limit(account_key)" in (workspace / "src/auth/login.py").read_text(
        encoding="utf-8"), "the edit landed"
    first, *rest = fake.implementer
    assert first.system == IMPLEMENTER_SYSTEM and first.model_sampling
    assert {t["name"] for t in first.tools or []} >= {"read_file", "edit_file", "report"}
    assert "Your task:" in first.messages[0].content
    # What it read is still in front of it when it edits, and when it reports.
    for later in rest:
        assert any(m.role == "tool" and "account_key = request.form" in m.content
                   for m in later.messages)
    turn = next(t for t in state.turns if t.agent_id in {
        a.id for a in state.agents.values() if a.kind is AgentKind.EXECUTION})
    assert turn.claimed_status.value == "done"
    assert turn.files_touched == ["src/auth/login.py"]
    assert "added the limiter call" in turn.output


async def test_supervision_looks_in_at_a_checkpoint_and_the_conversation_carries_on(
    supervisor: Supervisor,
) -> None:
    reads = [_calls(READ) for _ in range(CHECKPOINT_CALLS)]
    fake = NativeFake(*reads, _calls(EDIT), _calls(DONE))
    response = await _conversing(supervisor, fake).run(
        PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = supervisor.store.load_state(response.run_id)

    implementers = [a for a in state.agents.values() if a.kind is AgentKind.EXECUTION]
    turns = [t for t in state.turns if t.agent_id == implementers[0].id]
    assert turns[0].claimed_status.value == "running", "the checkpoint is a turn on the record"
    after = fake.implementer[CHECKPOINT_CALLS]
    assert after.messages[1].role == "assistant" and after.messages[1].tool_calls, (
        "the first stretch's calls are still in the conversation")
    assert any(m.role == "user" and m is not after.messages[0] for m in after.messages), (
        "and the directive was appended to it, not swapped in for it")


async def test_an_agent_that_only_talks_is_nudged_then_looked_in_on(
    supervisor: Supervisor,
) -> None:
    fake = NativeFake(_say("I will now look at the file."), _say("Thinking about it."),
                      _calls(READ), _calls(EDIT), _calls(DONE))
    await _conversing(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)

    assert fake.implementer[1].messages[-1].content == NUDGE


async def test_a_provider_without_native_tools_keeps_the_turn_contract(
    supervisor: Supervisor, fake: FakeProvider,
) -> None:
    supervisor.config.policy.implementer_loop = "conversation"
    seen: list[CompletionRequest] = []
    original = fake.complete

    async def spy(request: CompletionRequest) -> CompletionResponse:
        seen.append(request)
        return await original(request)

    fake.complete = spy  # type: ignore[method-assign]
    await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    assert seen and not any(r.tools for r in seen)


# -- the parts -------------------------------------------------------------------


def test_an_implementer_is_offered_report_and_a_lens_cannot_write() -> None:
    from supervisor_harness.config import Policy

    implementer = [t["name"] for t in native_tool_specs(
        AgentSpec(kind=AgentKind.EXECUTION), Policy(allow_command_execution=True))]
    lens = [t["name"] for t in native_tool_specs(AgentSpec(kind=AgentKind.ANALYSIS), Policy())]

    assert implementer[-1] == "report" and "run_command" in implementer
    assert "edit_file" in implementer and "write_file" in implementer
    assert not {"edit_file", "write_file", "run_command"} & set(lens)
    read = next(t for t in native_tool_specs(AgentSpec(), Policy()) if t["name"] == "read_file")
    assert "Reading changes nothing" in read["description"]


def test_a_stretch_becomes_a_turn_with_or_without_a_report() -> None:
    stint = Stint(text=["looking"])
    stint.record(READ, True, "src/auth/login.py")
    stint.record(EDIT, True, "")
    stint.record(ToolCall("write_file", {"path": "infra/x.tf"}), False, "")

    running = stint_payload(stint, None)
    reported = stint_payload(stint, {"status": "blocked", "summary": "need infra",
                                     "blocked_on": "infra/x.tf is outside my scope"})

    assert running["status"] == "running"
    assert running["files_touched"] == ["src/auth/login.py"], "a refused write touched nothing"
    assert "write_file infra/x.tf (refused)" in running["output"]
    assert reported["status"] == "blocked" and "outside my scope" in reported["blocked_on"]
    assert stint_payload(stint, {"status": "whatever", "summary": "s"})["status"] == "done"


def test_compaction_shrinks_the_oldest_results_and_keeps_the_rest() -> None:
    big = "x" * 50_000
    messages = [ChatMessage("user", "the brief")]
    for n in range(KEEP_RECENT_RESULTS + 4):
        messages += [ChatMessage("assistant", f"call {n}"), ChatMessage("tool", big)]

    shrunk = compact(messages, limit=200_000)
    tools = [m for m in messages if m.role == "tool"]

    assert shrunk and all(m.content == COMPACTED for m in tools[:shrunk])
    assert all(m.content == big for m in tools[-KEEP_RECENT_RESULTS:]), "recent ones stay"
    assert messages[0].content == "the brief"
    assert all(m.content.startswith("call") for m in messages if m.role == "assistant")
    assert compact(messages, limit=10**9) == 0


async def test_the_router_carries_tools_and_will_not_start_one_on_a_fallback_route(
    supervisor: Supervisor,
) -> None:
    from supervisor_harness.models import ModelBinding

    fake = NativeFake(_calls(READ))
    supervisor.router.register("fake", fake)
    await supervisor.router.complete("execution", CompletionRequest(
        messages=[ChatMessage("user", "x")], tools=[{"name": "report"}],
        model_sampling=True), binding=ModelBinding(provider="fake", model="m"))

    assert fake.implementer[0].tools == [{"name": "report"}]
    assert fake.implementer[0].model_sampling
    assert supervisor.router.native_tools(ModelBinding(provider="fake", model="m"))
    assert not supervisor.router.native_tools(
        ModelBinding(provider="fake", model="m", fallbacks=["other:model"])), (
        "a conversation in native tool calls cannot be carried on by a provider without them")
    assert not supervisor.router.native_tools(ModelBinding(provider="nope", model="m"))


async def test_a_verifier_judges_the_open_criteria_and_its_verdict_counts(
    supervisor: Supervisor,
) -> None:
    """Measured: on the turn contract, a verifier returned empty answers three times."""
    fake = NativeFake(_calls(READ), _calls(EDIT), _calls(DONE), verdict="fail")
    response = await _conversing(supervisor, fake).run(
        PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = supervisor.store.load_state(response.run_id)
    (task,) = state.tasks.values()

    assert fake.verifier, "the verifier was driven as a conversation"
    brief = fake.verifier[0].messages[0].content
    assert brief.startswith("# Verify:") and "Criteria to judge" in brief
    judged = [c for c in task.dod if c.verified_by and c.verified_by != "harness"]
    assert judged and all(c.status.value == "fail" for c in judged), (
        "the verdict was applied to the criteria it named")
    settled = [c for c in task.dod if c.verified_by == "harness"]
    assert all(f"`{c.id}`" not in brief for c in settled), (
        "criteria the harness had already settled are not handed to the verifier")



class LongReader(NativeFake):
    """A verifier that reads for as long as reading is offered, as twelve of thirteen
    did in go-live run 17, and once more after it is not."""

    def __init__(self, *steps: Step) -> None:
        super().__init__(*steps)
        self.tried_to_read_at_the_end = False

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        names = {t["name"] for t in request.tools or []}
        if "verdict" in names and names != {"verdict"}:
            self.verifier.append(request)
            return _calls(READ)(request)
        if names == {"verdict"} and not self.tried_to_read_at_the_end:
            self.verifier.append(request)
            self.tried_to_read_at_the_end = True
            return _calls(READ)(request)
        return await super().complete(request)


async def test_a_verifier_that_keeps_reading_is_brought_to_a_verdict(
    supervisor: Supervisor,
) -> None:
    """Measured (go-live run 17): the verifier's stretches shared one tool-call count,
    so every stretch after the first ended before the model was called -- "call
    verdict now" was never sent, and 12 of 13 verifiers left their criteria unjudged."""
    from supervisor_harness.core.conversation import CHECKPOINT_CALLS, VERDICT_NOW

    fake = LongReader(_calls(READ), _calls(EDIT), _calls(DONE))
    response = await _conversing(supervisor, fake).run(
        PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    (task,) = supervisor.store.load_state(response.run_id).tasks.values()

    assert len(fake.verifier) > 2 * CHECKPOINT_CALLS, "the stretches after the first were worked"
    assert any(m.content == VERDICT_NOW for m in fake.verifier[-1].messages)
    assert [t["name"] for t in fake.verifier[-1].tools or []] == ["verdict"], (
        "at the end, the verdict is all there is")
    assert any(m.role == "tool" and "read_file is not available now" in m.content
               for m in fake.verifier[-1].messages), "a read it was not offered is refused"
    judged = [c for c in task.dod if c.verified_by and c.verified_by != "harness"]
    assert judged and all(c.status.value == "pass" for c in judged)


# -- widening at the moment of a write ---------------------------------------------

WRITE_CACHE = ToolCall("write_file", {"path": "src/cache.py", "content": "LIMIT = 10\n"})


def _scoped_to_auth(fake: NativeFake) -> None:
    plan = fake._synthesis(None)  # type: ignore[arg-type]
    plan["tasks"][0]["scope_paths"] = ["src/auth/**"]
    plan["tasks"][0]["action"] = "Add the limiter to the login handler"
    fake.overrides["synthesis"] = plan


async def test_a_write_the_task_needs_widens_its_scope_within_the_grant(
    supervisor: Supervisor, workspace: Path,
) -> None:
    """Measured: the plan's envelope named `reporting/` for a task whose job was
    `core/reporting.py`, and its implementer was refused the one file it needed."""
    fake = NativeFake(_calls(WRITE_CACHE), _calls(DONE))
    _scoped_to_auth(fake)
    response = await _conversing(supervisor, fake).run(
        PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = supervisor.store.load_state(response.run_id)
    (task,) = state.tasks.values()

    assert (workspace / "src/cache.py").read_text(encoding="utf-8") == "LIMIT = 10\n"
    assert "src/cache.py" in task.scope.paths, "replayed from the log, not only in memory"
    assert any("scope widened within the run's envelope" in n.text for n in state.notes)


async def test_beyond_the_grant_the_write_is_refused_as_before(
    supervisor: Supervisor, workspace: Path,
) -> None:
    fake = NativeFake(_calls(WRITE_CACHE), _calls(DONE))
    _scoped_to_auth(fake)
    supervisor.config.policy.scope_envelope = ["src/auth/**", "tests/**"]
    response = await _conversing(supervisor, fake).run(
        PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    (task,) = supervisor.store.load_state(response.run_id).tasks.values()

    assert not (workspace / "src/cache.py").exists()
    assert "src/cache.py" not in task.scope.paths


async def test_not_into_a_running_peers_scope(supervisor: Supervisor) -> None:
    from supervisor_harness.core.tools import Toolbox
    from supervisor_harness.models import AgentStatus, ExecutionTask, RunState, Scope

    session = supervisor.store.create(RunState(id="run_W", prompt="p"))
    task = ExecutionTask(title="mine", scope=Scope(paths=["src/auth/**"]))
    me = AgentSpec(id="agt_me", kind=AgentKind.EXECUTION, task_id=task.id,
                   scope=Scope(paths=["src/auth/**"]), status=AgentStatus.RUNNING)
    peer = AgentSpec(id="agt_peer", kind=AgentKind.EXECUTION,
                     scope=Scope(paths=["src/cache.py"]), status=AgentStatus.RUNNING)
    session.state.tasks[task.id] = task
    session.state.agents.update({me.id: me, peer.id: peer})

    await supervisor._widen_for_write(session, me, Toolbox(supervisor.workspace,
                                                           supervisor.config.policy),
                                      "src/cache.py")
    assert me.scope.paths == ["src/auth/**"], "two writers stay apart"


def test_no_scope_is_widened_onto_the_floor(tmp_path: Path) -> None:
    """A path no agent may write is not one a scope may be widened to."""
    from supervisor_harness.config import Policy
    from supervisor_harness.core.tools import Toolbox

    box = Toolbox(tmp_path, Policy())
    assert box.writable_path(".git/hooks/pre-commit") is None
    assert box.writable_path("../outside.py") is None
    assert box.writable_path("src/cache.py") == "src/cache.py"


def _writer(supervisor: Supervisor, run_id: str) -> tuple[Any, AgentSpec]:
    """A run whose plan's envelope is src/ and tests/, granted the whole workspace."""
    from supervisor_harness.models import (
        AgentStatus,
        EnvelopeGrant,
        ExecutionTask,
        RunState,
        Scope,
        ScopeEnvelope,
    )

    session = supervisor.store.create(RunState(id=run_id, prompt="p"))
    session.state.envelope = ScopeEnvelope(paths=["src/", "tests/"], source="run plan")
    session.state.envelope_grant = EnvelopeGrant(by="the owner")
    task = ExecutionTask(title="mine", scope=Scope(paths=["src/auth/**"]))
    me = AgentSpec(id="agt_me", kind=AgentKind.EXECUTION, task_id=task.id,
                   scope=Scope(paths=["src/auth/**"]), status=AgentStatus.RUNNING)
    session.state.tasks[task.id] = task
    session.state.agents[me.id] = me
    return session, me


async def test_at_a_write_the_scope_widens_only_within_the_runs_envelope(
    supervisor: Supervisor,
) -> None:
    """Measured (go-live run 16): with the whole workspace granted, an implementer
    added twenty `scripts/tmp-*.mjs` patch scripts to the envelope, one write at a
    time."""
    from supervisor_harness.core.tools import Toolbox

    session, me = _writer(supervisor, "run_E")
    box = Toolbox(supervisor.workspace, supervisor.config.policy)
    await supervisor._widen_for_write(session, me, box, "scripts/tmp-patch.mjs")
    await supervisor._widen_for_write(session, me, box, "src/cache.py")

    assert me.scope.paths == ["src/auth/**", "src/cache.py"], "inside the plan, not beyond"
    assert session.state.envelope is not None
    assert session.state.envelope.paths == ["src/", "tests/"], "the envelope never grows here"


async def test_not_into_a_task_held_for_the_owner(supervisor: Supervisor) -> None:
    """Measured (go-live run 16): the reviewer vetoed adding the e2e suite to `npm
    run check`, and a peer made that very change in package.json."""
    from supervisor_harness.core.tools import Toolbox
    from supervisor_harness.models import (
        Escalation,
        EscalationReason,
        ExecutionTask,
        Scope,
        TaskStatus,
    )

    session, me = _writer(supervisor, "run_H")
    session.state.envelope.paths.append("package.json")  # type: ignore[union-attr]
    vetoed = ExecutionTask(title="Wire e2e into check", scope=Scope(paths=["package.json"]),
                           status=TaskStatus.BLOCKED)
    session.state.tasks[vetoed.id] = vetoed
    escalation = Escalation(reason=EscalationReason.REVIEW_VETO, task_id=vetoed.id)
    session.state.escalations[escalation.id] = escalation
    box = Toolbox(supervisor.workspace, supervisor.config.policy)

    await supervisor._widen_for_write(session, me, box, "package.json")
    assert me.scope.paths == ["src/auth/**"]
    assert any("held for the owner" in n.text for n in session.state.notes)

    escalation.resolution = Resolution.DECLINE
    await supervisor._widen_for_write(session, me, box, "package.json")
    assert "package.json" in me.scope.paths, "once the owner has answered, it is not held"
