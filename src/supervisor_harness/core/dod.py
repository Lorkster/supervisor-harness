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

    existing = " ".join(c.statement.lower() + " " + c.command.lower() for c in task.dod)
    added: list[DoDCriterion] = []

    if policy.require_tests and not re.search(r"\btest|spec|coverage\b", existing):
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

    if policy.require_security_review and not re.search(
        r"\bsecurity|injection|auth|secret|validat|sanitis|sanitiz\b", existing
    ):
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

    if policy.require_code_quality and not re.search(r"\blint|style|format|quality\b", existing):
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


def verify_command(
    criterion: DoDCriterion,
    workspace: Path,
    timeout: int = 300,
) -> VerificationOutcome:
    """Run a command criterion and judge it by exit code and expected output.

    Only reached when the caller has established that running commands here is
    permitted; see :func:`verify_criterion`.
    """
    command = criterion.command.strip()
    if not command:
        return VerificationOutcome(CriterionStatus.BLOCKED, "no command specified")

    try:
        completed = subprocess.run(  # noqa: S602 - the command is the criterion
            command,
            shell=True,
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
    """Best-effort tokenisation, used for display rather than execution."""
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()
