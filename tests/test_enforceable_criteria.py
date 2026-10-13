"""Criteria the harness can read for itself, read rather than sent back.

The schema asks every criterion for a ``command`` and an ``expect``, so a model
decoding against it does not drop them. For an inspection a local model fills
them as it would at a terminal: ``grep -q 'setOffline' e2e/offline.spec.ts``,
"exit 0". For three gates it writes one command, ``a && b && c``. In a planner
measurement every one-shot plan carried such criteria, and each was sent back as
unenforceable though it said exactly what to look for, and where, or what to run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from supervisor_harness.config import Policy
from supervisor_harness.core import phases
from supervisor_harness.core.dod import (
    check_what_a_review_names,
    commands_a_review_names,
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


# -- a review that says named commands pass ------------------------------------------------


def _review(statement: str, rubric: str = "", mandatory: bool = True) -> ExecutionTask:
    return ExecutionTask(title="t", dod=[DoDCriterion(
        statement=statement, method=VerifyMethod.REVIEW, rubric=rubric, mandatory=mandatory)])


GATES = ("All three acceptance gates pass as separate commands: npm run check, "
         "pwsh scripts/verify.ps1, npm run test:e2e")


def test_a_review_that_says_gates_pass_gets_a_run_of_each() -> None:
    """Baseline 3: the verifier blocked this review for want of runs it could quote."""
    task = _review(GATES, "Each of the three commands exits 0 when run individually.")

    notes = check_what_a_review_names(task)

    *runs, review = task.dod
    assert [c.command for c in runs] == ["npm run check", "pwsh scripts/verify.ps1",
                                         "npm run test:e2e"]
    assert all(c.method is VerifyMethod.COMMAND and c.mandatory and not c.expect for c in runs)
    assert len({c.statement for c in runs}) == 3, "three criteria, told apart"
    assert review.method is VerifyMethod.REVIEW and review.statement == GATES, (
        "the review keeps its rubric, now with the runs to read")
    assert notes and "3 command(s)" in notes[0]


def test_a_command_the_task_already_runs_is_not_run_twice() -> None:
    task = _review(GATES)
    task.dod.insert(0, DoDCriterion(statement="the e2e suite passes", method=VerifyMethod.COMMAND,
                                    command="npm  run test:e2e", mandatory=True))

    check_what_a_review_names(task)
    check_what_a_review_names(task)   # the send-back and the preparation both repair

    assert [c.command for c in task.dod if c.command] == [
        "npm  run test:e2e", "npm run check", "pwsh scripts/verify.ps1"]


def test_an_optional_review_adds_optional_runs() -> None:
    task = _review("`pytest -q tests/test_x.py` passes", mandatory=False)

    check_what_a_review_names(task)

    assert [(c.command, c.mandatory) for c in task.dod[:-1]] == [("pytest -q tests/test_x.py",
                                                                    False)]


def test_a_review_that_expects_a_failure_gets_no_clean_exit_run() -> None:
    """A clean exit would prove the opposite of what it says."""
    for statement in ("`npm run lint` fails on the unused import, and `npm test` passes",
                      "npm run check rejects the old config; the rest pass"):
        task = _review(statement)
        assert check_what_a_review_names(task) == [] and len(task.dod) == 1, statement


def test_a_review_that_claims_nothing_passes_is_left_alone() -> None:
    task = _review("The README documents `npm run check` for contributors")

    assert check_what_a_review_names(task) == [] and len(task.dod) == 1


def test_only_a_review_is_given_runs() -> None:
    """A criterion that runs or reads something already says what settles it."""
    for method in (VerifyMethod.COMMAND, VerifyMethod.TEST, VerifyMethod.INSPECTION):
        task = ExecutionTask(title="t", dod=[DoDCriterion(
            statement="npm run check and npm run test:e2e pass", method=method,
            command="npm run check", mandatory=True)])
        assert check_what_a_review_names(task) == [] and len(task.dod) == 1, method


@pytest.mark.parametrize(("text", "named"), [
    ("npm run check passes after the change", ["npm run check"]),
    ("npm run check and npm run test:e2e both pass", ["npm run check", "npm run test:e2e"]),
    ("npm run all passes", ["npm run all"]),
    ("npm run check, npm run lint and npm test each exit 0",
     ["npm run check", "npm run lint", "npm test"]),
    ("`make lint` and `pytest -q tests/x.py` pass", ["make lint", "pytest -q tests/x.py"]),
    ("python tools/check_doc_refs.py exits 0 in the finished tree",
     ["python tools/check_doc_refs.py"]),
    # Runners that begin English sentences count only in backticks.
    ("Make sure the docs pass review and go through the changelog", []),
    # Nothing the harness would refuse to run as a criterion.
    ("`curl https://example.com` and `python -c 'import os'` pass", []),
    ("`npm run check && npm test` passes", []),
])
def test_the_commands_a_review_names(text: str, named: list[str]) -> None:
    assert commands_a_review_names(text) == named


def test_the_verifier_reads_the_runs_the_review_named(tmp_path: Path) -> None:
    """End to end: repaired, run by the harness, and in front of the verifier."""
    from supervisor_harness.agents.brief import build_verifier_brief
    from supervisor_harness.core.dod import verify_criterion
    from supervisor_harness.models import RunState

    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "gate.py").write_text("print('gate green')\n", encoding="utf-8")
    task = _review("All gates pass: python tools/gate.py, pytest --version")

    fill_what_the_harness_can(task, tmp_path)
    for crit in task.dod:
        outcome = verify_criterion(crit, tmp_path, Policy(), allow_commands=True)
        if outcome is not None:
            crit.status, crit.evidence = outcome.status, outcome.evidence

    brief = build_verifier_brief(RunState(), task)
    ran = brief.split("What the harness already ran", 1)[1]
    assert "$ python tools/gate.py\nexit=0" in ran.replace("  ", "") and "gate green" in ran
    assert "$ pytest --version" in ran


# -- an install is judged by its exit --------------------------------------------------------


def test_an_install_checked_for_its_wording_is_checked_for_its_exit() -> None:
    """Minesweeper run 3: `npm install` expecting "added" failed on "up to date", three times."""
    from supervisor_harness.core.dod import judge_installs_by_their_exit

    task = _command("npm install", expect="added")
    notes = judge_installs_by_their_exit(task)

    (crit,) = task.dod
    assert crit.expect == "" and crit.command == "npm install"
    assert notes and "judged by its exit" in notes[0]


@pytest.mark.parametrize("command", ["npm ci", "npm i", "pnpm install", "yarn", "yarn install",
                                     "pip install -r requirements.txt",
                                     "python -m pip install -e .", "uv sync", "poetry install"])
def test_every_installer_is_read_the_same(command: str) -> None:
    from supervisor_harness.core.dod import judge_installs_by_their_exit

    task = _command(command, expect="Successfully installed")
    judge_installs_by_their_exit(task)
    assert task.dod[0].expect == "", command


@pytest.mark.parametrize(("command", "expect"), [
    ("npm test", "20 passed"),           # not an install: its output is the point
    ("npm run build", "built in"),
    ("npm install", "exit 0"),           # already an exit
    ("npm install", "1"),                # an exit code, whatever it means
    ("npm install", ""),                 # nothing asked
])
def test_what_is_left_as_written(command: str, expect: str) -> None:
    from supervisor_harness.core.dod import judge_installs_by_their_exit

    task = _command(command, expect=expect)
    assert judge_installs_by_their_exit(task) == [] and task.dod[0].expect == expect


def test_the_harness_reads_installs_before_it_judges_a_plan() -> None:
    task = _command("npm install", expect="added")
    fill_what_the_harness_can(task, None)
    assert task.dod[0].expect == ""
