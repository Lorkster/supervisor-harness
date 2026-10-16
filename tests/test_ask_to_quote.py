"""A verifier whose pass on an inspection quotes nothing from its file is asked once more.

Minesweeper run 21: the scaffold's "no 'dependencies' key at all" went to the
verifier as an inspection whose expected text was a sentence. The verifier passed
it three times, rightly -- "top-level keys are exactly name, private, version,
type, scripts, devDependencies (line 14)" -- quoting nothing that is in the file
as written. Each pass was thrown out, the verifier never told why, and the task
failed three times.
"""

from __future__ import annotations

from typing import Any

from supervisor_harness.agents.brief import INSPECTION_HANDED_OVER
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    AgentStatus,
    Backend,
    CriterionStatus,
    DoDCriterion,
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
    Usage,
)

from .conftest import FakeProvider

PACKAGE = ('{\n  "name": "minesweeper",\n  "private": true,\n  "type": "module",\n'
           '  "scripts": { "test": "vitest run" },\n  "devDependencies": {\n'
           '    "vite": "^5.4.0"\n  }\n}\n')
SUMMED = ("package.json:1-9 top-level keys are exactly name, private, type, scripts, "
          "devDependencies (line 6). There is no top-level \"dependencies\" property.")
QUOTED = 'package.json:6 `"devDependencies": {` -- the only dependency block; no other.'


class Verifier(FakeProvider):
    """Answers each verdict request with the next scripted evidence for one criterion."""

    native_tools = True

    def __init__(self, criterion_id: str, *evidence: str) -> None:
        super().__init__()
        self.criterion_id, self.evidence = criterion_id, list(evidence)
        self.asked: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if "verdict" not in {t["name"] for t in request.tools or []}:
            return await super().complete(request)
        self.asked.append(request)
        said = self.evidence.pop(0) if self.evidence else SUMMED
        return CompletionResponse(text="", usage=Usage(input_tokens=10), tool_calls=[
            ToolCall("verdict", {"summary": "s", "results": [
                {"criterion_id": self.criterion_id, "status": "pass", "evidence": said}]})])


def _setup(supervisor: Supervisor, run_id: str, *evidence: str) -> tuple[Any, Any, Any, Verifier]:
    (supervisor.workspace / "package.json").write_text(PACKAGE, encoding="utf-8")
    crit = DoDCriterion(statement="package.json has devDependencies only -- no 'dependencies' "
                                  "key at all", method=VerifyMethod.INSPECTION,
                        expect='package.json: the file contains no top-level "dependencies" '
                               "property")
    task = ExecutionTask(title="Scaffold", scope=Scope(paths=["package.json"]),
                         status=TaskStatus.AWAITING_VERIFICATION, attempts=1, dod=[crit])
    agent = AgentSpec(id="agt_v", kind=AgentKind.VERIFICATION, task_id=task.id,
                      status=AgentStatus.RUNNING, attempt=1, scope=Scope(paths=["package.json"]),
                      binding=supervisor.config.binding_for("verification"))
    session = supervisor.store.create(RunState(id=run_id, prompt="p", backend=Backend.AUTONOMOUS,
                                               tasks={task.id: task},
                                               agents={agent.id: agent}))
    fake = Verifier(crit.id, *evidence)
    supervisor.router.register("fake", fake)
    task = session.state.tasks[task.id]          # the run's own copy
    return session, task, task.dod[0], fake


async def test_asked_once_more_it_quotes_and_the_pass_stands(supervisor: Supervisor) -> None:
    session, _, crit, fake = _setup(supervisor, "run_Quote", SUMMED, QUOTED)

    await supervisor._converse_verifier(session, session.state.agents["agt_v"])

    assert crit.status is CriterionStatus.PASS
    assert len(fake.asked) == 2
    asked = fake.asked[1].messages[-1].content
    assert "Your pass on these quotes nothing that is in the file each one names" in asked
    assert f"`{crit.id}`" in asked and "-- in package.json" in asked
    assert "copy the lines that show what is there instead" in asked
    assert any("asked once more to quote the file for 1 inspection(s)" in n.text
               for n in session.state.notes)


async def test_still_quoting_nothing_it_is_thrown_out_as_before(supervisor: Supervisor) -> None:
    session, _, crit, fake = _setup(supervisor, "run_Still", SUMMED, SUMMED, SUMMED)

    await supervisor._converse_verifier(session, session.state.agents["agt_v"])

    assert crit.status is CriterionStatus.FAIL and "quotes nothing" in crit.evidence
    assert len(fake.asked) == 2, "once more, not until it gives in"


async def test_a_pass_that_quotes_is_not_asked_again(supervisor: Supervisor) -> None:
    session, _, crit, fake = _setup(supervisor, "run_First", QUOTED)

    await supervisor._converse_verifier(session, session.state.agents["agt_v"])

    assert crit.status is CriterionStatus.PASS and len(fake.asked) == 1


def test_the_verifier_is_told_how_up_front() -> None:
    flat = " ".join(INSPECTION_HANDED_OVER.split())
    assert "copy the lines of that file that show it, exactly, in backticks" in flat
    assert ("For a statement that something is absent, copy the lines that show what is "
            "there") in flat
