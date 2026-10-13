"""Batch J: the goal, held by the supervisor, checked by looking when its task settles.

The first green-field run verified all five tasks of a browser Minesweeper whose
board rendered as a single column. Every judgement was against the planner's
criteria, and none said it should look like Minesweeper; reading the code, the
local model passed the one-column board three times out of three, and shown a
screenshot it failed it every time. See docs/autonomy-plan.md, batch J.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness.agents.brief import build_implementer_brief
from supervisor_harness.contracts import SYNTHESIS_SCHEMA, parse_tasks
from supervisor_harness.core import observe, phases
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    ExecutionTask,
    GoalExpectation,
    RunMode,
    RunState,
    VerifyMethod,
)
from supervisor_harness.providers.base import ChatMessage
from supervisor_harness.providers.ollama import _wire_message

from .test_implementer_conversation import DONE, NativeFake, _calls, _conversing
from .test_send_back_criteria import PROMPT

TITLE = "Add rate limiting to the login endpoint"
PNG = b"\x89PNG\r\n\x1a\n-a-picture"


def _plan(fake: Any, goal: list[dict[str, str]] | None) -> dict[str, Any]:
    data = copy.deepcopy(fake._synthesis(None))
    if goal is None:
        data.pop("goal", None)
    else:
        data["goal"] = goal
    return data


def _expect(statement: str, delivered_by: str = TITLE, look: str = "", build: str = "",
            whole: bool = False) -> dict:
    return {"statement": statement, "delivered_by": delivered_by, "look": look, "build": build,
            "whole": whole}


# -- the plan's goal ------------------------------------------------------------------------


def test_the_plan_must_say_what_the_result_is_for() -> None:
    goal = SYNTHESIS_SCHEMA["properties"]["goal"]
    assert "goal" in SYNTHESIS_SCHEMA["required"] and goal["minItems"] == 1
    assert "too obvious to write" in goal["description"]
    assert goal["items"]["required"] == ["statement", "delivered_by", "look", "build", "whole"]


def test_what_keeps_a_goal_from_being_checked_goes_back(fake: Any) -> None:
    data = _plan(fake, None)
    tasks = parse_tasks(data, "run_X")

    (line,) = phases.goal_problems(data, tasks)
    assert "names no goal" in line

    data = _plan(fake, [_expect("429 after ten tries"), _expect("a log line", "Nope")])
    (line,) = phases.goal_problems(data, parse_tasks(data, "run_X"))
    assert "'a log line'" in line and "'Nope' names no task" in line

    data = _plan(fake, [_expect("429 after ten tries", TITLE.upper())])   # case aside
    assert phases.goal_problems(data, parse_tasks(data, "run_X")) == []
    assert phases.goal_problems({"tasks": []}, []) == [], "a report proposes no work"


def test_each_expectation_is_a_mandatory_review_of_the_task_that_delivers_it(fake: Any) -> None:
    data = _plan(fake, [_expect("429 after ten tries"),
                        _expect("the page shows the limit", look="dist/index.html",
                                build="npm run build"),
                        _expect("an audit line", "No such task")])
    tasks = parse_tasks(data, "run_X")

    goal, unplaced = phases.attach_goal(tasks, data, "Add rate limiting\nmore detail")

    (task,) = tasks
    checks = [c for c in task.dod if c.goal_id]
    assert [c.goal_id for c in checks] == [goal[0].id, goal[1].id]
    assert all(c.method is VerifyMethod.REVIEW and c.mandatory for c in checks)
    assert "The run's goal: Add rate limiting." in checks[0].rubric
    assert "429 after ten tries" in checks[0].rubric and "shown the page" not in checks[0].rubric
    assert "You are shown the page as a person opening it would see it" in checks[1].rubric
    assert (goal[1].look, goal[1].build) == ("dist/index.html", "npm run build")
    assert goal[2].task_id == "" and "is not checked" in unplaced[0], "kept and said, not guessed"


async def test_the_run_holds_the_goal_on_its_log(supervisor: Supervisor, fake: Any) -> None:
    response = await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    store = supervisor.store
    (store.runs_dir / response.run_id / "state.json").unlink()
    state = store.load_state(response.run_id)

    (expectation,) = state.goal
    (task,) = state.tasks.values()
    assert expectation.task_id == task.id and expectation.source == "plan"
    assert any(c.goal_id == expectation.id for c in task.dod)


async def test_an_expectation_no_task_delivers_goes_back_in_the_conversation(
    supervisor: Supervisor,
) -> None:
    """A missing goal is refused for its shape; one that names no task, by the send-back."""
    from .test_planner_conversation import Planner, _planning

    class UndeliveredFirst(Planner):
        def plan(self, weak: bool) -> dict[str, Any]:
            return _plan(self, [_expect("429 after ten tries", "Nope" if weak else TITLE)])

    fake = UndeliveredFirst(weak_first=True)
    response = await _planning(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE,
                                                     auto_approve=True)
    state = supervisor.store.load_state(response.run_id)

    sent = [m.content for m in fake.planner[-1].messages
            if m.role == "user" and "cannot be checked by the harness" in m.content]
    assert sent and "'Nope' names no task in this plan" in sent[0]
    (expectation,) = state.goal
    assert expectation.task_id, "the revision stood, delivered by a task"


def test_a_plan_with_no_goal_is_refused_for_its_shape() -> None:
    from supervisor_harness.core.conversation import shape_problems

    assert any("goal" in p for p in shape_problems({"summary": "s", "recommended_mode": "execute"},
                                                   SYNTHESIS_SCHEMA))


# -- the verifier sees what a person would ------------------------------------------------


class Looked(NativeFake):
    def __init__(self, look: str) -> None:
        super().__init__(_calls(DONE))
        self.overrides["synthesis"] = _plan(self, [_expect("the page shows the limit",
                                                           look=look)])


async def test_the_verifier_is_shown_the_page_an_expectation_names(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, str, str]] = []

    def fake_look(tree: Path, page: str, build: str = "", **_: Any) -> observe.Observation:
        calls.append((tree, page, build))
        return observe.Observation(PNG, f"`{page}`, served from the run's tree")

    monkeypatch.setattr(observe, "look", fake_look)
    fake = Looked("dist/index.html?level=1")
    response = await _conversing(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE,
                                                       auto_approve=True)
    state = supervisor.store.load_state(response.run_id)

    first = fake.verifier[0].messages[0]
    assert calls and calls[0][1] == "dist/index.html?level=1"
    assert first.images == ["iVBORw0KGgotYS1waWN0dXJl"], "the PNG, base64"
    assert "## What the result looks like" in first.content and "Picture 1" in first.content
    assert any(n.text == "looked at the run's page for its goal" for n in state.notes)


async def test_a_page_that_cannot_be_shown_is_judged_from_the_code_and_said(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(observe, "look", lambda *a, **k: observe.Observation(None, "no renderer"))
    fake = Looked("dist/index.html")
    await _conversing(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)

    first = fake.verifier[0].messages[0]
    assert first.images == []
    assert "could not be shown (no renderer)" in first.content and "from the code" in first.content


async def test_a_settled_expectation_is_not_looked_at_again(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from supervisor_harness.models import CriterionStatus

    monkeypatch.setattr(observe, "look", lambda *a, **k: pytest.fail("already settled"))
    task = ExecutionTask(title=TITLE)
    expectation = GoalExpectation(statement="shows the limit", task_id=task.id,
                                  look="dist/index.html")
    criterion = phases.goal_criterion(expectation, PROMPT)
    criterion.status = CriterionStatus.PASS
    task.dod.append(criterion)
    session = supervisor.store.create(RunState(id="run_S", prompt=PROMPT,
                                               goal=[expectation], tasks={task.id: task}))

    assert await supervisor._look_at_the_goal(session, task) == ("", [])


async def test_with_nothing_to_look_at_the_verifier_is_shown_nothing(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(observe, "look", lambda *a, **k: pytest.fail("nothing to look at"))
    fake = Looked("")
    await _conversing(supervisor, fake).run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)

    first = fake.verifier[0].messages[0]
    assert first.images == [] and "What the result looks like" not in first.content


# -- the renderer itself -------------------------------------------------------------------


def test_a_page_that_is_not_there_or_a_build_that_is_refused_is_said(tmp_path: Path) -> None:
    assert "is not a file in the tree" in observe.look(tmp_path, "dist/index.html").note
    (tmp_path.parent / "outside.html").write_text("x", encoding="utf-8")
    assert "is not a file in the tree" in observe.look(tmp_path, "../outside.html").note
    refused = observe.look(tmp_path, "index.html", "curl https://example.com")
    assert refused.png is None and "was not run" in refused.note


def test_a_real_page_is_photographed(tmp_path: Path) -> None:
    pytest.importorskip("playwright.sync_api")
    (tmp_path / "index.html").write_text("<h1>Hello</h1><script>document.title='x'</script>",
                                         encoding="utf-8")
    seen = observe.look(tmp_path, "index.html?x=1")

    if seen.png is None and "no browser" in seen.note:
        pytest.skip(seen.note)
    assert seen.png is not None and seen.png.startswith(b"\x89PNG"), seen.note
    assert "served from the run's tree" in seen.note


def test_from_disk_a_page_that_needs_a_server_says_why(tmp_path: Path) -> None:
    """Minesweeper run 1's build: a module script in its own file, refused on file://."""
    pytest.importorskip("playwright.sync_api")
    (tmp_path / "app.js").write_text("document.body.append('drawn')", encoding="utf-8")
    (tmp_path / "index.html").write_text(
        '<script type="module" crossorigin src="./app.js"></script><p>page</p>', encoding="utf-8")

    served = observe.look(tmp_path, "index.html")
    from_disk = observe.look(tmp_path, "file:index.html")

    if from_disk.png is None and "no browser" in from_disk.note:
        pytest.skip(from_disk.note)
    assert "the page reported" not in served.note, served.note
    assert from_disk.png is not None and "opened from disk" in from_disk.note
    assert "the page reported" in from_disk.note and "CORS" in from_disk.note


# -- carried to the model, and to the implementer ------------------------------------------


def test_pictures_travel_with_the_message() -> None:
    from supervisor_harness.providers.openrouter import _content

    message = ChatMessage("user", "look", images=["QUJD"])
    assert _wire_message(message)["images"] == ["QUJD"]
    assert "images" not in _wire_message(ChatMessage("user", "plain"))
    parts = _content(message)
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,QUJD"
    assert _content(ChatMessage("user", "plain")) == "plain"


def test_the_implementer_knows_what_its_task_delivers_of_the_goal() -> None:
    task = ExecutionTask(title="t")
    run = RunState(prompt="p", goal=[GoalExpectation(statement="the board is a grid",
                                                     task_id=task.id),
                                     GoalExpectation(statement="sounds play", task_id="other")])
    brief = build_implementer_brief(run, AgentSpec(kind=AgentKind.EXECUTION), task)

    assert "## What the result is for" in brief
    assert "- the board is a grid (yours)" in brief and "- sounds play\n" in brief + "\n"
    assert "What the result is for" not in build_implementer_brief(
        RunState(prompt="p"), AgentSpec(kind=AgentKind.EXECUTION), task)
