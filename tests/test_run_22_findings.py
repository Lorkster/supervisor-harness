"""What go-live run 22 (plantsandclimate P3-18) found.

- Two tasks were held for `playwright test --grep '...'` with no count, though
  Playwright prints how many tests passed.
- The task adding the "needs connection" strings was held to the liveness bar,
  because `connection` is one of its trigger words; a task adding two JSON
  strings cannot have a bounded-time test of a contended path, so its verifier
  blocked, and the task was reopened.
- The checkpoint's corrections -- "implement per-shard error handling in
  ResultsSection.tsx", "create e2e/offline.spec.ts" -- named no task, so they
  went to every reopened task, and the i18n task's implementer built the work
  of two tasks held for the owner. Its brief listed only vetoed tasks as held.
"""

from __future__ import annotations

from supervisor_harness.agents.brief import build_implementer_brief
from supervisor_harness.config import Policy
from supervisor_harness.core.dod import apply_quality_bars, counts_passed, unpinned_selection
from supervisor_harness.core.supervisor import corrections_for_task
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    DoDCriterion,
    Escalation,
    EscalationReason,
    ExecutionTask,
    RunState,
    Scope,
    VerifyMethod,
)


def test_a_playwright_grep_is_held_to_its_count_where_it_runs() -> None:
    grep = DoDCriterion(statement="cached plants render offline", method=VerifyMethod.TEST,
                        command="npx playwright test e2e/offline.spec.ts --grep "
                                "'cached plants render'")

    assert counts_passed(grep.command)
    assert unpinned_selection(grep) is None


def test_a_quoted_message_does_not_bring_the_liveness_bar() -> None:
    strings = ExecutionTask(
        title="Add 'needs connection' i18n strings to en.json and sv.json",
        action=('Add the key offline.needsConnection: "Needs a connection to show", '
                "and assert it in tests/i18n/index.test.ts"),
        dod=[DoDCriterion(statement="both locales have the key", method=VerifyMethod.INSPECTION,
                          expect="src/i18n/en.json: needsConnection")])
    reconnect = ExecutionTask(
        title="Retry a tile fetch after the connection returns",
        action="Retry the failed tile fetch in src/data/tiles/loader.ts with backoff",
        dod=[DoDCriterion(statement="the retry happens", method=VerifyMethod.INSPECTION,
                          expect="src/data/tiles/loader.ts: retry")])

    def liveness(task: ExecutionTask) -> bool:
        return any("spin without bound" in c.statement
                   for c in apply_quality_bars(task, Policy()))

    assert not liveness(strings), "a message it shows is not what its code does"
    assert liveness(reconnect), "the words still count outside quotes"


def test_a_correction_about_another_tasks_files_is_not_handed_to_everyone() -> None:
    strings = ExecutionTask(title="Add the strings to en.json and sv.json",
                            action="Add offline.needsConnection to src/i18n/en.json")
    results = ExecutionTask(title="Per-shard handling in the results list",
                            action="Change src/app/results/ResultsSection.tsx to allSettled")
    e2e = ExecutionTask(title="Write the offline Playwright test",
                        action="Create e2e/offline.spec.ts that cuts the network")
    tasks = [strings, results, e2e]
    corrections = [
        "Fix the i18n key parity failure between en.json and sv.json",
        "Implement per-shard error handling in ResultsSection.tsx",
        "Create e2e/offline.spec.ts: load online, then setOffline(true)",
        "Run the whole suite before reporting",
    ]

    mine = corrections_for_task(strings, corrections, tasks)

    assert mine == [corrections[0], corrections[3]], (
        "its own file's correction, and the one addressed to the run; not the others'")
    assert corrections[1] in corrections_for_task(results, corrections, tasks)


def test_every_task_held_for_the_owner_is_in_the_brief_not_only_the_vetoed() -> None:
    run = RunState(prompt="Do P3-18")
    strings = ExecutionTask(title="Add the strings", scope=Scope(paths=["src/"]))
    results = ExecutionTask(title="Per-shard handling in the results list",
                            action="Change ResultsSection.tsx to allSettled")
    run.tasks.update({strings.id: strings, results.id: results})
    held = Escalation(reason=EscalationReason.UNENFORCEABLE_DEFINITION_OF_DONE,
                      task_id=results.id, detail="--grep with no count")
    run.escalations[held.id] = held
    agent = AgentSpec(kind=AgentKind.EXECUTION, task_id=strings.id, scope=strings.scope)

    brief = build_implementer_brief(run, agent, strings)

    assert "## Held for the owner" in brief
    assert "Per-shard handling in the results list" in brief
    assert "held: unenforceable definition of done" in brief
    assert "Do not do their work" in brief
