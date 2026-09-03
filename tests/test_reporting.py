"""The reporting layer, and the one line the split did not move verbatim.

`core/reporting.py` was lifted out of `core/supervisor.py` with its method
bodies byte-identical -- 16 of the 17 moved methods are exactly the text they
were, which is how "no behaviour change" was established for this refactor
rather than asserted.

`status` is the exception. One expression inside it was 104 characters once the
project's line limit applied, and it could not be folded in place: it was an
implicit concatenation of two f-strings, and splitting the first across lines is
a syntax error before Python 3.12. So it became `_dod_summary`.

That made it the only changed line in the refactor, and nothing in the suite
asserted the string it produces -- `status` was covered, but its `dod` field was
never read by a test. These are that missing assertion.
"""

from __future__ import annotations

from supervisor_harness.core.reporting import _dod_summary
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    CriterionStatus,
    DoDCriterion,
    ExecutionTask,
    RunMode,
)

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"


def _task(*statuses: CriterionStatus, optional: int = 0) -> ExecutionTask:
    dod = [
        DoDCriterion(statement=f"criterion {i}", method="test", mandatory=True, status=s)
        for i, s in enumerate(statuses)
    ]
    dod += [
        DoDCriterion(statement=f"optional {i}", method="test", mandatory=False,
                     status=CriterionStatus.PASS)
        for i in range(optional)
    ]
    return ExecutionTask(title="t", action="a", motivation="m", dod=dod)


def test_the_dod_summary_counts_passed_over_mandatory() -> None:
    assert _dod_summary(_task()) == "0/0"
    assert _dod_summary(_task(CriterionStatus.PASS)) == "1/1"
    assert _dod_summary(_task(CriterionStatus.PASS, CriterionStatus.FAIL)) == "1/2"
    assert _dod_summary(_task(CriterionStatus.FAIL, CriterionStatus.UNVERIFIED)) == "0/2"


def test_optional_criteria_are_not_counted() -> None:
    """`mandatory_criteria`, not `dod` -- the distinction the original made.

    A task whose optional criteria all pass and whose mandatory one does not is
    the case where counting the wrong list reads as finished.
    """
    assert _dod_summary(_task(CriterionStatus.FAIL, optional=3)) == "0/1"


async def test_status_reports_the_dod_summary_for_each_task(
    supervisor: Supervisor
) -> None:
    """The field as a caller of `supervisor status` actually receives it.

    Driven through a real run rather than by writing a task into the state:
    `status` reads the store, so a task that never reached the log is not a task
    it can see -- which is how the first draft of this test managed to assert
    against a run it had not changed.
    """
    final = await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = supervisor.store.load_state(final.run_id)

    view = supervisor.status(final.run_id)

    assert view["tasks"], "the run produced no tasks to report on"
    for reported, task in zip(view["tasks"], state.tasks.values(), strict=True):
        assert reported["dod"] == _dod_summary(task)
        passed, total = reported["dod"].split("/")
        assert int(total) == len(task.mandatory_criteria)
        assert int(passed) <= int(total)
