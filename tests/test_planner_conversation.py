"""The planner as a conversation: batch I's remedy applied to the half that plans.

After go-live run 22 nearly every failure started before code was written, in a
synthesis that ran in one shot, with thinking off and no tools: 39 of 70 task
scopes were the whole run envelope, and tasks were built on code that does not
exist. With ``policy.planner_loop = "conversation"`` the synthesis reads with
read-only tools and answers through ``propose_plan``.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from supervisor_harness.core.planner import PLANNER_TOOLS
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import RunMode
from supervisor_harness.providers.base import CompletionRequest, CompletionResponse, ToolCall

from .conftest import FakeProvider
from .test_implementer_conversation import NativeFake, _calls
from .test_send_back_criteria import PROMPT

WRITERS = {"edit_file", "write_file", "delete_file", "run_command", "report"}


class Planner(NativeFake):
    """Reads a file, then proposes the fake's plan -- or a weak one first."""

    def __init__(self, *, weak_first: bool = False, plans: bool = True) -> None:
        super().__init__()
        self.weak_first, self.plans = weak_first, plans
        self.planner: list[CompletionRequest] = []

    def plan(self, weak: bool) -> dict[str, Any]:
        data = copy.deepcopy(self._synthesis(CompletionRequest()))
        if weak:
            for task in data["tasks"]:
                task["dod"][0] = {"statement": "it is covered by tests", "method": "test",
                                  "mandatory": True}
        return data

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        names = {t["name"] for t in request.tools or []}
        if "propose_plan" not in names:
            return await super().complete(request)
        self.planner.append(request)
        if not self.plans:
            return CompletionResponse(text="I would rather not.")
        if not any(m.role == "tool" for m in request.messages):
            return _calls(ToolCall("read_file", {"path": "src/auth/login.py"}))(request)
        sent_back = any("cannot be checked by the harness" in m.content
                        for m in request.messages if m.role == "user")
        return _calls(ToolCall("propose_plan",
                               self.plan(self.weak_first and not sent_back)))(request)


def _planning(supervisor: Supervisor, fake: FakeProvider) -> Supervisor:
    supervisor.config.policy.planner_loop = "conversation"
    supervisor.router.register("fake", fake)
    return supervisor


async def test_the_planner_reads_before_it_plans_and_answers_with_the_plan(
    supervisor: Supervisor,
) -> None:
    fake = Planner()
    response = await _planning(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE,
                                                     auto_approve=True)
    state = supervisor.store.load_state(response.run_id)

    assert fake.planner, "the synthesis was driven as a conversation"
    first = fake.planner[0]
    names = {t["name"] for t in first.tools or []}
    assert "propose_plan" in names and not names & WRITERS, "a reader, not a writer"
    assert PLANNER_TOOLS.strip()[:30] in first.system
    assert any(m.role == "tool" and "def login" in m.content
               for m in fake.planner[-1].messages), "it planned on what it read"
    assert [t.title for t in state.tasks.values()] == ["Add rate limiting to the login endpoint"]
    assert any(n.text == "planned in a conversation" for n in state.notes)


async def test_unenforceable_criteria_go_back_inside_the_conversation(
    supervisor: Supervisor,
) -> None:
    fake = Planner(weak_first=True)
    response = await _planning(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE,
                                                     auto_approve=True)
    state = supervisor.store.load_state(response.run_id)
    (task,) = state.tasks.values()

    assert any("synthesis sent back once" in n.text for n in state.notes)
    assert any("cannot be checked by the harness" in m.content
               for m in fake.planner[-1].messages), "the same conversation, not a new call"
    assert all(c.statement != "it is covered by tests" for c in task.dod), "the revision stood"


async def test_no_plan_and_no_native_tools_both_fall_back_to_the_one_shot_call(
    supervisor: Supervisor,
) -> None:
    silent = Planner(plans=False)
    response = await _planning(supervisor, silent).run(PROMPT, mode=RunMode.EXECUTE,
                                                       auto_approve=True)
    state = supervisor.store.load_state(response.run_id)
    assert silent.planner and state.tasks, "the one-shot synthesis planned instead"
    assert any("ended without a plan" in n.text for n in state.notes)

    plain = FakeProvider()                      # no native tools
    supervisor.router.register("fake", plain)
    assert not supervisor._plans_in_conversation()
    supervisor.config.policy.planner_loop = "one_shot"
    assert not supervisor._plans_in_conversation()


async def test_batch_f_can_call_the_planner_either_way(tmp_path: Path, config: Any) -> None:
    from supervisor_harness.evals.cases import parse_case
    from supervisor_harness.evals.roles import Variant, run_role
    from supervisor_harness.providers.router import ModelRouter

    case = parse_case({"id": "p", "role": "planner", "fixture": {"kind": "files", "files": {}},
                       "request": PROMPT})
    fake = Planner(weak_first=True)
    router = ModelRouter(config, host_name="eval")
    router.register("fake", fake)
    config.policy.planner_loop = "conversation"

    out = await run_role(case, tmp_path, tmp_path / "s",
                         Variant(name="conv", planner="conversation"), base=config, router=router)

    assert fake.planner and out.tasks and not out.error
    assert out.tasks[0].dod[0].statement == "it is covered by tests", (
        "its first answer, not the harness's repair of it")


def test_the_plan_schema_asks_for_what_a_model_would_otherwise_leave_out() -> None:
    """Decoding against a schema, a model leaves optional fields out. In batch F's
    first measurement the one-shot planner gave every task an empty scope -- read
    as the whole workspace -- and no `test` criterion a command."""
    from supervisor_harness.contracts import _DOD, SYNTHESIS_SCHEMA
    from supervisor_harness.core.review import REVISION_SCHEMA

    task = SYNTHESIS_SCHEMA["properties"]["tasks"]["items"]
    assert {"scope_paths", "depends_on"} <= set(task["required"])
    assert task["properties"]["scope_paths"]["minItems"] == 1
    assert {"command", "expect"} <= set(_DOD["required"])
    assert "scope_paths" in REVISION_SCHEMA["required"]


def test_a_list_sent_as_json_text_is_read_as_the_list() -> None:
    """Batch F: five of 21 planner conversations sent "tasks" as the JSON of the
    list, and the plan read as having no tasks."""
    import json

    from supervisor_harness.core.conversation import as_specified
    from supervisor_harness.core.planner import PLAN_TOOL

    tasks = [{"title": "t", "action": "a"}]
    fixed = as_specified({"tasks": json.dumps(tasks), "summary": "{\"a\": 1}",
                          "recommended_mode": "execute"}, PLAN_TOOL)
    assert fixed["tasks"] == tasks
    assert fixed["summary"] == "{\"a\": 1}", "a string the schema wants stays a string"
    assert as_specified({"tasks": json.dumps(tasks) + "]}"}, PLAN_TOOL)["tasks"] == tasks, (
        "a list closed with a bracket too many, as the local model sent it")
    assert as_specified({"tasks": "not json"}, PLAN_TOOL)["tasks"] == "not json"
    assert as_specified({"tasks": '{"a": 1}'}, PLAN_TOOL)["tasks"] == '{"a": 1}', (
        "decoded only to the type the schema names")


class TextPlanner(Planner):
    """Proposes its plan with the tasks as JSON text, as the local model did."""

    def plan(self, weak: bool) -> dict[str, Any]:
        import json

        data = super().plan(weak)
        return {**data, "tasks": json.dumps(data["tasks"])}


async def test_a_plan_whose_tasks_came_as_text_still_plans(supervisor: Supervisor) -> None:
    response = await _planning(supervisor, TextPlanner()).run(PROMPT, mode=RunMode.EXECUTE,
                                                              auto_approve=True)
    state = supervisor.store.load_state(response.run_id)
    assert [t.title for t in state.tasks.values()] == ["Add rate limiting to the login endpoint"]
    assert not any("ended without a plan" in n.text for n in state.notes)
