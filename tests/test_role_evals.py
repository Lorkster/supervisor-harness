"""Batch F: each role measured on a fixed case, and a whole run scored against its tree.

Retargeted after go-live run 22 at the roles that plan and judge -- planner,
reviewer, verifier -- because by then the implementer was the half that worked.
Generic by construction: a case may be a repository at a commit, or an empty
directory with nothing but a request, and the generic checks hold for both.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from supervisor_harness.config import HarnessConfig
from supervisor_harness.evals.cases import CaseError, load_cases, parse_case
from supervisor_harness.evals.checks import GENERIC, judge
from supervisor_harness.evals.fixtures import materialise
from supervisor_harness.evals.roles import RoleOutput, Variant, run_role
from supervisor_harness.evals.runner import evaluate, parse_variant, summarise
from supervisor_harness.evals.score import record_counts, render, scorecard
from supervisor_harness.models import (
    DoDCriterion,
    ExecutionTask,
    RunState,
    Scope,
    TaskStatus,
    VerifyMethod,
)
from supervisor_harness.providers.router import ModelRouter

from .conftest import FakeProvider
from .test_implementer_conversation import NativeFake
from .test_veto_review import CI, Reviewer


def _case(role: str, **over: object) -> dict[str, object]:
    return {"id": f"c-{role}", "role": role, "fixture": {"kind": "files", "files": {}},
            "request": "Build it", **over}


def _router(config: HarnessConfig, provider: FakeProvider) -> ModelRouter:
    router = ModelRouter(config, host_name="eval")
    router.register("fake", provider)
    return router


def _task(title: str, paths: list[str], *crit: DoDCriterion) -> ExecutionTask:
    return ExecutionTask(title=title, action=title, scope=Scope(paths=paths), dod=list(crit))


RUN = DoDCriterion(statement="tests pass", method=VerifyMethod.TEST, command="pytest -q")


# -- cases and fixtures -------------------------------------------------------------


def test_a_case_says_what_is_wrong_with_it() -> None:
    with pytest.raises(CaseError, match="role"):
        parse_case(_case("judge"))
    with pytest.raises(CaseError, match="repo and commit"):
        parse_case(_case("review", fixture={"kind": "git", "repo": "x"}))
    case = parse_case(_case("planner", checks=["answered", {"check": "task_count", "max": 3}]))
    assert case.greenfield and case.checks[0] == {"check": "answered"}


def test_an_empty_directory_is_a_workspace(tmp_path: Path) -> None:
    """A green-field run starts from nothing but a prompt."""
    files = parse_case(_case("verifier", fixture={"kind": "files", "files": {"a/b.py": "x"}}))
    with materialise(files, tmp_path) as ws:
        assert (ws / "a" / "b.py").read_text(encoding="utf-8") == "x"
        made = ws
    assert not made.exists(), "the workspace is removed after"
    with materialise(parse_case(_case("planner")), tmp_path) as ws:
        assert ws.is_dir() and not any(ws.iterdir())


def test_a_repository_at_a_commit_with_the_change_on_top(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.email=e@x", "-c", "user.name=e"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    commit = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True,
                            check=True).stdout.strip()
    (repo / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    patch = subprocess.run([*git, "diff"], capture_output=True, text=True, check=True).stdout
    subprocess.run([*git, "checkout", "-q", "--", "a.txt"], check=True)
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "change.patch").write_text(patch, encoding="utf-8", newline="\n")
    (cases / "c.json").write_text(json.dumps(_case("verifier", fixture={
        "kind": "git", "repo": "${EVAL_REPO}", "commit": commit, "patch": "change.patch"})),
        encoding="utf-8")
    (case,) = load_cases([cases])

    with pytest.raises(CaseError, match="environment variable"), \
            materialise(case, tmp_path / "cache"):
        pass
    monkeypatch.setenv("EVAL_REPO", str(repo))
    with materialise(case, tmp_path / "cache") as ws:
        assert (ws / "a.txt").read_text(encoding="utf-8").splitlines() == ["one", "two"]
    assert "eval-" not in subprocess.run([*git, "worktree", "list"], capture_output=True,
                                         text=True, check=True).stdout, "worktree removed"


# -- generic checks -------------------------------------------------------------------


def test_the_planner_is_held_to_scopes_that_mean_something(tmp_path: Path) -> None:
    """39 of 70 go-live task scopes were the whole run envelope."""
    (tmp_path / "src").mkdir()
    case = parse_case(_case("planner", fixture={"kind": "git", "repo": "r", "commit": "c"}))
    same = RoleOutput(tasks=[_task("a", ["src/", "tests/"], RUN),
                             _task("b", ["src/", "tests/"], RUN)])
    lost = RoleOutput(tasks=[_task("a", ["src/styles/tokens.css"], RUN),
                             _task("b", ["src/new.ts", "src/**"], RUN)])

    assert not judge(case, same, tmp_path)["scopes_specific"]["passed"]
    grounded = judge(case, lost, tmp_path)["scopes_grounded"]
    assert not grounded["passed"] and "src/styles/tokens.css" in grounded["detail"]
    assert "src/new.ts" not in grounded["detail"], "a new file where its directory exists"
    greenfield = parse_case(_case("planner"))
    assert judge(greenfield, lost, tmp_path)["scopes_grounded"]["passed"], (
        "in an empty workspace every path is one the plan creates")


def test_the_planner_is_held_to_criteria_the_harness_can_check(tmp_path: Path) -> None:
    case = parse_case(_case("planner"))
    vague = DoDCriterion(statement="it works", method=VerifyMethod.TEST)
    blind = DoDCriterion(statement="looks right", method=VerifyMethod.INSPECTION)
    out = RoleOutput(tasks=[_task("a", ["src/a.py"], vague), _task("b", ["src/b.py"], blind)])
    results = judge(case, out, tmp_path)

    assert not results["criteria_enforceable"]["passed"]
    assert not results["mechanical_criterion"]["passed"]
    assert judge(case, RoleOutput(tasks=[_task("a", ["src/a.py"], RUN)]),
                 tmp_path)["mechanical_criterion"]["passed"]


def test_a_failed_call_fails_every_check_and_says_why(tmp_path: Path) -> None:
    case = parse_case(_case("review"))
    results = judge(case, RoleOutput(error="ConnectError: refused"), tmp_path)
    assert set(results) == set(GENERIC["review"])
    assert not any(r["passed"] for r in results.values())
    assert "refused" in results["answered"]["detail"]


def test_labelled_checks_carry_a_persons_expectation(tmp_path: Path) -> None:
    review = parse_case(_case("review", checks=[{"check": "verdict_in", "verdicts": ["veto"]}]))
    assert judge(review, RoleOutput(ruling="breaks_the_project", why="ci.yml:31"),
                 tmp_path)["verdict_in"]["passed"], "any veto, when the label says veto"
    assert not judge(review, RoleOutput(ruling="proceed"), tmp_path)["verdict_in"]["passed"]
    planner = parse_case(_case("planner", checks=[
        {"check": "no_task_matches", "pattern": r"test:e2e.{0,20}\bcheck\b"},
        {"check": "task_count", "min": 2, "max": 3},
        {"check": "some_scope_covers", "path": "src/core/reporting.py"},
        {"check": "nonsense"}]))
    wired = _task("Wire it", ["package.json"],
                  DoDCriterion(statement="add test:e2e to the check script",
                               method=VerifyMethod.INSPECTION, expect="package.json: test:e2e"))
    results = judge(planner, RoleOutput(tasks=[wired]), tmp_path)
    assert not results["no_task_matches"]["passed"]
    assert not results["task_count"]["passed"]
    assert not results["some_scope_covers"]["passed"]
    assert results["nonsense"] == {"passed": False, "detail": "no such check"}


# -- the roles, called the way the harness calls them --------------------------------


async def test_the_planner_role_is_its_first_answer(tmp_path: Path, config: HarnessConfig) -> None:
    fake = FakeProvider()
    case = parse_case(_case("planner"))
    out = await run_role(case, tmp_path, tmp_path / "store", Variant(name="think", think=True),
                         base=config, router=_router(config, fake))
    assert out.tasks and out.tasks[0].title == "Add rate limiting to the login endpoint"
    assert not out.error


async def test_the_review_role_reads_and_rules(tmp_path: Path, config: HarnessConfig) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(CI, encoding="utf-8")
    task = _task("Wire e2e into check", ["package.json"], RUN)
    case = parse_case(_case("review", input={
        "task": json.loads(json.dumps(task, default=lambda o: o.__dict__)), "plan": []}))
    for verdict, want in (("breaks_the_project", "breaks_the_project"), ("proceed", "proceed")):
        out = await run_role(case, tmp_path, tmp_path / f"store-{verdict}", Variant(),
                             base=config, router=_router(config, Reviewer(verdict)))
        assert out.ruling == want, out.error


async def test_the_verifier_role_rules_on_what_it_is_handed(
    workspace: Path, config: HarnessConfig,
) -> None:
    crit = DoDCriterion(id="dod_eval1", statement="login is throttled", method=VerifyMethod.REVIEW,
                        rubric="cite the line", mandatory=True)
    task = _task("Throttle login", ["src/auth/"], crit)
    task.status = TaskStatus.AWAITING_VERIFICATION
    from supervisor_harness.serde import to_jsonable

    case = parse_case(_case("verifier", input={"task": to_jsonable(task),
                                               "change_summary": "added a limiter"}))
    config.implementer_loop = "conversation"  # type: ignore[attr-defined]
    config.policy.implementer_loop = "conversation"
    out = await run_role(case, workspace, workspace / ".store", Variant(), base=config,
                         router=_router(config, NativeFake(verdict="fail")))
    assert out.criteria and out.criteria[0]["status"] == "fail", out.error
    assert judge(case, out, workspace)["all_ruled"]["passed"]


# -- running and summarising ----------------------------------------------------------


def test_a_variant_is_read_from_its_spec() -> None:
    assert parse_variant("think:think=on,sampling=model,route=ollama:m") == Variant(
        name="think", route="ollama:m", think=True, model_sampling=True)
    assert parse_variant("harness") == Variant()
    with pytest.raises(ValueError, match="unknown key"):
        parse_variant("x:temp=1")


async def test_cases_run_under_variants_and_are_summarised(
    tmp_path: Path, config: HarnessConfig,
) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "p.json").write_text(json.dumps(_case("planner")), encoding="utf-8")
    out = tmp_path / "records.jsonl"
    seen: list[str] = []
    records = await evaluate(load_cases([cases_dir]), [Variant(), Variant(name="think",
                                                                           think=True)],
                             2, out, base=config, router=_router(config, FakeProvider()),
                             progress=seen.append)

    assert len(records) == 4 and len(out.read_text(encoding="utf-8").splitlines()) == 4
    assert len(seen) == 4
    table = summarise(records)
    assert "| planner | harness | 1 | 2 |" in table and "| planner | think | 1 | 2 |" in table
    assert "scopes_grounded" in table


# -- a whole run's scorecard ----------------------------------------------------------


def test_a_run_is_scored_by_its_record_and_by_its_tree(tmp_path: Path) -> None:
    state = RunState(prompt="p")
    done = _task("a", ["src/"], RUN)
    done.status, done.attempts = TaskStatus.VERIFIED, 1
    state.tasks[done.id] = done
    ok = f'"{sys.executable}" -c "raise SystemExit(0)"'
    bad = f'"{sys.executable}" -c "raise SystemExit(3)"'

    card = scorecard(state, [ok, bad], workspace=tmp_path)

    assert record_counts(state)["tasks_verified"] == 1
    assert [a["passed"] for a in card["acceptance"]] == [True, False]
    assert card["accepted"] is False and card["acceptance"][1]["exit"] == 3
    assert "Accepted: no" in render(card) and "FAIL" in render(card)
    nowhere = scorecard(state, [ok])
    assert nowhere["acceptance"][0]["tail"] == "the run left no tree to check"


async def test_a_case_whose_workspace_cannot_be_built_fails_alone(
    tmp_path: Path, config: HarnessConfig,
) -> None:
    """The first full measurement stopped at one patch that did not apply."""
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "a-broken.json").write_text(json.dumps(_case("planner", id="a-broken", fixture={
        "kind": "files", "files": {"x.txt": "x\n"}, "patch": "missing.patch"})),
        encoding="utf-8")
    (cases_dir / "b-fine.json").write_text(json.dumps(_case("planner", id="b-fine")),
                                           encoding="utf-8")

    records = await evaluate(load_cases([cases_dir]), [Variant()], 1, tmp_path / "r.jsonl",
                             base=config, router=_router(config, FakeProvider()))

    by_case = {r["case"]: r for r in records}
    assert set(by_case) == {"a-broken", "b-fine"}, "the evaluation carried on"
    assert not by_case["a-broken"]["passed"]
    assert "fixture:" in by_case["a-broken"]["checks"]["answered"]["detail"]
    assert by_case["b-fine"]["checks"]["answered"]["passed"]
