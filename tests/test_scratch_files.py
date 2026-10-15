"""An implementer may remove the scratch output it left, wherever it left it.

Minesweeper run 19: the engine's implementer compiled its file into
`.tmp-verify/engine.js` to try it, outside its scope. The project's lint then
failed on that file, and the engine was sent back three times for it. Each
attempt said it could not delete the file "with the available tools", and failed.
The harness would have let it: a file a failing check names widens the scope
within the owner's grant. Its rules now say so.
"""

from __future__ import annotations

from supervisor_harness.core.conversation import IMPLEMENTER_SYSTEM
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.core.tools import Toolbox
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    AgentStatus,
    CriterionStatus,
    DoDCriterion,
    EnvelopeGrant,
    ExecutionTask,
    RunState,
    Scope,
    ScopeEnvelope,
    VerifyMethod,
)


async def test_the_scratch_file_a_failing_check_names_can_be_deleted(
    supervisor: Supervisor,
) -> None:
    root = supervisor.workspace
    (root / ".tmp-verify").mkdir()
    (root / ".tmp-verify" / "engine.js").write_text("exports.x = 1;\n", encoding="utf-8")
    session = supervisor.store.create(RunState(id="run_Scratch", prompt="p"))
    session.state.envelope = ScopeEnvelope(paths=["src/", "tests/"], source="run plan")
    session.state.envelope_grant = EnvelopeGrant(by="the owner", paths=[])  # the workspace
    lint = DoDCriterion(statement="The project's `lint` script passes",
                        method=VerifyMethod.COMMAND, command="npm run lint",
                        status=CriterionStatus.FAIL,
                        evidence=(f"$ npm run lint\nexit=1\n\n{root / '.tmp-verify' / 'engine.js'}"
                                  "\n  124:30  error  'exports' is not defined  no-undef"))
    task = ExecutionTask(title="Engine", scope=Scope(paths=["src/game/engine.ts"]), dod=[lint])
    me = AgentSpec(id="agt_me", kind=AgentKind.EXECUTION, task_id=task.id,
                   scope=Scope(paths=["src/game/engine.ts"]), status=AgentStatus.RUNNING)
    session.state.tasks[task.id] = task
    session.state.agents[me.id] = me
    box = Toolbox(root, supervisor.config.policy)

    assert not box.call("delete_file", {"path": ".tmp-verify/engine.js"}, me).ok, (
        "outside its scope, before the harness widens it")
    await supervisor._widen_for_write(session, me, box, ".tmp-verify/engine.js")
    assert box.call("delete_file", {"path": ".tmp-verify/engine.js"}, me).ok
    assert not (root / ".tmp-verify" / "engine.js").exists()


def test_the_implementer_is_told_it_may() -> None:
    flat = " ".join(IMPLEMENTER_SYSTEM.split())
    assert "Leave nothing behind that the project's checks would read" in flat
    assert ("A file a failing check names that no task wrote -- your own scratch output -- "
            "you may delete, wherever it is: the harness lets you.") in flat
