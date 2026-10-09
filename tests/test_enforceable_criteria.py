"""Criteria the harness can read for itself, read rather than sent back.

The schema asks every criterion for a ``command`` and an ``expect``, so a model
decoding against it does not drop them. For an inspection a local model fills
them as it would at a terminal: ``grep -q 'setOffline' e2e/offline.spec.ts``,
"exit 0". For three gates it writes one command, ``a && b && c``. In a planner
measurement every one-shot plan carried such criteria, and each was sent back as
unenforceable though it said exactly what to look for, and where, or what to run.
"""

from __future__ import annotations

from supervisor_harness.config import Policy
from supervisor_harness.core import phases
from supervisor_harness.core.dod import (
    fill_what_the_harness_can,
    read_grep_inspections,
    split_chained_commands,
)
from supervisor_harness.models import DoDCriterion, ExecutionTask, VerifyMethod


def _inspection(command: str, expect: str = "exit 0") -> ExecutionTask:
    return ExecutionTask(title="t", dod=[DoDCriterion(
        statement="the spec takes the browser offline", method=VerifyMethod.INSPECTION,
        command=command, expect=expect, mandatory=True)])


def _command(command: str, expect: str = "exit 0",
             method: VerifyMethod = VerifyMethod.COMMAND) -> ExecutionTask:
    return ExecutionTask(title="t", dod=[DoDCriterion(
        statement="the gates pass", method=method, command=command, expect=expect,
        mandatory=True)])


# -- an inspection written as a grep ------------------------------------------------------


def test_a_plain_grep_of_one_file_is_read_as_its_file_and_text() -> None:
    task = _inspection("grep -q 'setOffline' e2e/offline.spec.ts")

    notes = read_grep_inspections(task)

    (crit,) = task.dod
    assert crit.expect == "e2e/offline.spec.ts: setOffline" and crit.command == ""
    assert notes and "grep -q 'setOffline'" in notes[0]
    assert phases.unenforceable_criteria([task], Policy(min_dod_criteria=1)) == []


def test_each_text_of_a_chained_check_becomes_a_criterion() -> None:
    task = _inspection("test -f index.html && grep -q '<input' index.html "
                       "&& grep -qF '<button>' index.html")

    read_grep_inspections(task)

    assert [c.expect for c in task.dod] == ["index.html: <input", "index.html: <button>"]
    assert len({c.statement for c in task.dod}) == 2, "two criteria, told apart"
    assert all(c.mandatory and c.method is VerifyMethod.INSPECTION for c in task.dod)


def test_a_file_that_must_exist_is_an_inspection_of_the_file() -> None:
    task = _inspection("test -f style.css")
    read_grep_inspections(task)
    assert task.dod[0].expect == "style.css:"


def test_every_file_a_chain_checks_is_kept() -> None:
    task = _inspection("test -f index.html && test -f app.js && grep -q 'app.js' index.html")
    read_grep_inspections(task)
    assert [c.expect for c in task.dod] == ["index.html: app.js", "app.js:"]


def test_a_command_that_only_checks_files_is_an_inspection() -> None:
    """Measured: `test -s styles.css` as a `command`, refused as no check runner."""
    task = _command("test -s styles.css && test -s app.js", expect="exit code 0")

    read_grep_inspections(task)

    assert [(c.method, c.expect, c.command) for c in task.dod] == [
        (VerifyMethod.INSPECTION, "styles.css:", ""), (VerifyMethod.INSPECTION, "app.js:", "")]
    absence = _command("grep -q 'navigator.onLine' src/app/x.tsx", expect="exit code 1")
    assert read_grep_inspections(absence) == [], "a grep expected to fail claims absence"
    assert absence.dod[0].method is VerifyMethod.COMMAND


def test_what_a_substring_cannot_say_is_left_for_the_send_back() -> None:
    for command in [
        "grep -qE 'needsConnection|offline' e2e/offline.spec.ts",   # alternation
        "grep -q 'from.*i18n' src/app/Placeholder.tsx",               # a pattern
        "! grep -q 'navigator.onLine' src/app/results/ResultsSection.tsx",  # absence
        "grep -c 'try {' src/app/results/ResultsSection.tsx",         # a count
        "grep -rq 'fetch-failed' src/app",                             # a tree
        "grep -q x src/a.ts | wc -l",                                  # a pipe
        "grep -q 'x' src/*.ts",                                        # a glob
        "node -e \"require('./src/i18n/en.json')\"",                   # a program
        "test -f a.txt && grep -qE 'x|y' a.txt",                       # one link of it
    ]:
        task = _inspection(command)
        assert read_grep_inspections(task) == [], command
        assert task.dod[0].command == command and len(task.dod) == 1, command


def test_an_inspection_that_already_names_its_file_is_left_alone() -> None:
    task = _inspection("grep -q 'b' other.ts", expect="a.ts: a")
    assert read_grep_inspections(task) == []
    assert task.dod[0].expect == "a.ts: a"


# -- gates chained into one command -------------------------------------------------------


def test_chained_gates_become_one_criterion_each() -> None:
    task = _command("npm run check && pwsh scripts/verify.ps1 && npm run test:e2e",
                    expect="all commands exit 0", method=VerifyMethod.TEST)

    notes = split_chained_commands(task)

    assert [c.command for c in task.dod] == [
        "npm run check", "pwsh scripts/verify.ps1", "npm run test:e2e"]
    assert all(c.expect == "" and c.method is VerifyMethod.TEST and c.mandatory
               for c in task.dod)
    assert len({c.statement for c in task.dod}) == 3
    assert notes and "3 commands" in notes[0]
    assert phases.unenforceable_criteria([task], Policy()) == []


def test_a_chain_is_kept_whole_when_splitting_it_would_guess() -> None:
    for command, expect in [
        ("npm run check && npm test", "12 passed"),         # which link prints it?
        ("npm run check && python -c 'print(1)'", "exit 0"),  # a link it will not run
        ("npm run check | tee out.txt", "exit 0"),          # not a chain of commands
        ("npm run check", "exit 0"),                        # nothing to split
    ]:
        task = _command(command, expect)
        assert split_chained_commands(task) == [], command
        assert task.dod[0].command == command, command


def test_the_harness_reads_both_before_it_judges_a_plan() -> None:
    task = ExecutionTask(title="t", dod=[
        *_inspection("grep -q 'needs a connection' src/i18n/en.json").dod,
        *_command("npm run check && npm run test:e2e").dod,
    ])
    assert phases.unenforceable_criteria([task], Policy())

    fill_what_the_harness_can(task, None)

    assert phases.unenforceable_criteria([task], Policy()) == []
