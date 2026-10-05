"""A task's tests must fail without the task: the verifier that is not a model.

Every batch of this harness's own development found the same defect at least
once: a first-draft test that passed against the unfixed code. It looked like a
test, it ran green, and it proved nothing, because it would have run green
before the change too. The check that caught it each time was mechanical: run
the new tests against the previous commit and watch them go red.

This module is that check, run by the harness itself:

1. a temporary git worktree at the run's baseline commit;
2. the test files the task added or changed, copied into it;
3. those test modules run there, and then in the working tree;
4. a per-test comparison of the two runs.

It **passes** when at least one test that passes now did not pass on the
baseline, and **fails**, naming the tests, when every one of them already passed
there: those tests do not detect the change. It is ``BLOCKED`` when the check
itself could not run (no worktree, no report from the runner), which is the
state a verifier agent may still judge.

## What counts as "did not pass on the baseline"

A test that failed or errored there, and a test that did not exist there because
its module could not even be imported without the change. The second is the
common case for brand-new code (`from mod import new_function` at the top of a
test file), and it does show the test depends on the change. It does not show
the test *asserts* anything, so the evidence says when that is the only reason a
test counted, and the review bars still apply.

## Running the baseline's code, not the working tree's

A project installed in editable mode resolves its own package to the working
tree, whichever directory the tests run from. Run naively, the baseline side
would test the changed code and every test would "pass on the baseline". This
repository's own suite hides the problem by putting ``src`` on ``sys.path`` in
its conftest, which is how the probe for this module almost missed it. So the
baseline run is given ``PYTHONPATH`` pointing at the worktree's own ``src`` and
root, which Python searches before site-packages and before an editable
install's finder. A project that puts its package somewhere else can still leak,
and that failure is the safe direction: a false "already passes on the baseline"
fails the task, it never passes one.

## Scope

Only pytest, for now: the runner is the one whose per-test results can be read
without guessing (``--junitxml``), and the harness's own projects use it. Any
other runner, and a criterion the harness is not permitted to run, go to the
verifier agent with the procedure written into the criterion's rubric, and the
verdict is recorded as that agent's claim, as every delegated criterion is.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ..models import CriterionStatus, DoDCriterion
from .baseline import STORE_DIRECTORY, _git
from .dod import (
    VerificationOutcome,
    executable_name,
    pytest_argv,
    run_bounded,
    unsafe_command,
)
from .paths import matches_any, scope_relative

#: Directory names whose contents are test material: fixtures and conftest
#: files as well as test modules. Copied to the baseline with the modules, so a
#: test that needs a fixture the task added is not failed for the fixture's
#: absence.
TEST_DIRECTORIES = frozenset({"tests", "test", "testing", "__tests__"})

_TEST_MODULE = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.py$")

#: How much of each run's output is kept as evidence.
_TAIL = 800

#: How many test ids the evidence names before summarising.
_NAMED = 8


def is_test_module(path: str) -> bool:
    """A file pytest collects by default: ``test_*.py`` or ``*_test.py``."""
    return bool(_TEST_MODULE.search(path))


def is_test_path(path: str) -> bool:
    """A test module, a ``conftest.py``, or anything under a test directory."""
    parts = path.split("/")
    return (
        is_test_module(path)
        or parts[-1] == "conftest.py"
        or any(part in TEST_DIRECTORIES for part in parts[:-1])
    )


def safe_relative(raw: str, workspace: Path) -> str | None:
    """``raw`` as an existing file's workspace-relative path, or ``None``.

    These paths reach a command line, and in a host-delegated run they are the
    agent's own report of what it touched. So: inside the workspace, an existing
    file, no component a runner could read as an option, and nothing under
    ``.git`` or the harness's own store.
    """
    rel = scope_relative(raw, str(workspace))
    if not rel:
        return None
    parts = rel.split("/")
    if any(not part or part == ".." or part.startswith("-") for part in parts):
        return None
    if parts[0] in (".git", STORE_DIRECTORY):
        return None
    full = (workspace / rel).resolve()
    try:
        full.relative_to(workspace.resolve())
    except ValueError:
        return None
    return rel if full.is_file() else None


def select_test_material(touched: Iterable[str], workspace: Path) -> list[str]:
    """The test material among ``touched``, made safe, de-duplicated and sorted."""
    return sorted({
        rel for raw in touched
        if (rel := safe_relative(raw, workspace)) is not None and is_test_path(rel)
    })


def changed_since(workspace: Path, baseline: str, scope: list[str]) -> list[str]:
    """Files changed or added since ``baseline``, within ``scope`` when it names any.

    The fallback for a task whose agents did not report what they touched. It
    cannot tell one task's changes from another's in a shared tree, which is why
    the agents' own reports are read first and this is narrowed to the scope.
    """
    listed: list[str] = []
    for args in (("diff", "--name-only", baseline, "--"),
                 ("ls-files", "--others", "--exclude-standard")):
        out = _git(workspace, *args)
        if out:
            listed.extend(out.splitlines())
    return [path for path in listed if not scope or matches_any(path, scope)]


@dataclass
class PytestRun:
    """What one pytest run reported, test by test."""

    exit_code: int
    passed: set[str] = field(default_factory=set)
    not_passed: set[str] = field(default_factory=set)
    #: Modules that could not be collected at all, by the report's own name.
    uncollectable: set[str] = field(default_factory=set)
    reported: bool = True
    timed_out: bool = False
    tail: str = ""


def read_junit(path: Path) -> tuple[set[str], set[str], set[str]] | None:
    """Passed ids, not-passed ids and uncollectable modules, or ``None`` if unreadable.

    A test id is ``classname::name``, which pytest writes relative to the
    rootdir, so the same test has the same id in the worktree and the working
    tree. Skipped tests are neither: they ran in neither sense.
    """
    try:
        root = ET.parse(path).getroot()  # noqa: S314 - pytest's own output, local file
    except (OSError, ET.ParseError):
        return None
    passed: set[str] = set()
    not_passed: set[str] = set()
    uncollectable: set[str] = set()
    for case in root.iter("testcase"):
        test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
        if case.find("skipped") is not None:
            continue
        error = case.find("error")
        if error is not None and "collection failure" in (error.get("message") or ""):
            uncollectable.add(case.get("name", "") or case.get("classname", ""))
            continue
        if error is not None or case.find("failure") is not None:
            not_passed.add(test_id)
        else:
            passed.add(test_id)
    return passed, not_passed, uncollectable


def run_tests(argv: list[str], modules: list[str], cwd: Path, report: Path,
              timeout: float, extra_env: dict[str, str] | None = None) -> PytestRun:
    """Run ``modules`` with pytest in ``cwd`` and read its per-test report."""
    command = [*argv, "-p", "no:cacheprovider", f"--junitxml={report}", "--", *modules]
    try:
        completed = run_bounded(command, cwd, timeout, extra_env=extra_env)
    except subprocess.TimeoutExpired:
        return PytestRun(exit_code=-1, reported=False, timed_out=True,
                       tail=f"timed out after {timeout:.0f}s")
    except OSError as exc:
        return PytestRun(exit_code=-1, reported=False, tail=f"could not run: {exc}")
    tail = (completed.stdout + completed.stderr).strip()[-_TAIL:]
    parsed = read_junit(report)
    if parsed is None:
        return PytestRun(exit_code=completed.returncode, reported=False, tail=tail)
    passed, not_passed, uncollectable = parsed
    return PytestRun(completed.returncode, passed, not_passed, uncollectable, tail=tail)


def baseline_environment(tree: Path) -> dict[str, str]:
    """``PYTHONPATH`` for a run that must import the baseline's code, not the working tree's."""
    roots = [tree / "src", tree] if (tree / "src").is_dir() else [tree]
    return {"PYTHONPATH": os.pathsep.join(map(str, roots))}


def _named(ids: Iterable[str]) -> str:
    ordered = sorted(ids)
    shown = ", ".join(ordered[:_NAMED])
    return shown + (f", and {len(ordered) - _NAMED} more" if len(ordered) > _NAMED else "")


def _blocked(reason: str) -> VerificationOutcome:
    return VerificationOutcome(CriterionStatus.BLOCKED, reason)


def verify_fails_before(
    criterion: DoDCriterion,
    files: list[str],
    workspace: Path,
    baseline: str,
    timeout: float = 300,
) -> VerificationOutcome:
    """Prove that the tests in ``files`` fail on ``baseline`` and pass now.

    ``files`` is the task's test material, already made safe by
    :func:`select_test_material`. Only its test modules are run; the rest (conftest files,
    fixtures) is copied to the baseline so the modules can be collected there.
    """
    if not baseline:
        return _blocked("no baseline commit was recorded for this run, so there is "
                        "nothing to compare against")
    modules = [f for f in files if is_test_module(f)]
    if not modules:
        looked = ", ".join(files) or "nothing"
        return VerificationOutcome(
            CriterionStatus.FAIL,
            "this task changed no test module, so nothing shows the change is tested "
            f"(test material it touched: {looked})",
        )

    refusal = unsafe_command(criterion.command)
    argv = pytest_argv(criterion.command) if refusal is None else None
    if argv is None:
        return _blocked(refusal or f"{criterion.command!r} is not a pytest invocation; "
                        "this check reads pytest's per-test report")
    runner = shutil.which(argv[0])
    if runner is None:
        return _blocked(f"{executable_name(argv[0])!r} is not on PATH")
    argv = [runner, *argv[1:]]

    with tempfile.TemporaryDirectory(prefix="supervisor-fails-before-",
                                     ignore_cleanup_errors=True) as scratch:
        tmp = Path(scratch)
        after = run_tests(argv, modules, workspace, tmp / "after.xml", timeout)
        shown = f"$ {criterion.command} -- {' '.join(modules)}"
        if after.timed_out:
            return VerificationOutcome(CriterionStatus.FAIL,
                                       f"{shown}\nwith the change: {after.tail}")
        if not after.reported:
            return _blocked(f"{shown}\nwith the change, pytest wrote no report "
                            f"(exit {after.exit_code}):\n{after.tail}")
        if after.not_passed or after.uncollectable:
            failing = after.not_passed | after.uncollectable
            return VerificationOutcome(
                CriterionStatus.FAIL,
                f"{shown}\nthe task's own tests do not pass with the change: "
                f"{_named(failing)}\n{after.tail}",
            )
        if not after.passed:
            return VerificationOutcome(
                CriterionStatus.FAIL,
                f"{shown}\nselected no test with the change, so this proves nothing",
            )

        tree = tmp / "baseline"
        added = _git(workspace, "worktree", "add", "--detach", str(tree), baseline,
                     timeout=timeout)
        if added is None:
            return _blocked(f"could not create a worktree at the baseline {baseline}")
        try:
            for rel in files:
                target = tree / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(workspace / rel, target)
            before = run_tests(argv, modules, tree, tmp / "before.xml", timeout,
                               extra_env=baseline_environment(tree))
        finally:
            _remove_worktree(workspace, tree)

    if before.timed_out:
        return _blocked(f"{shown}\non the baseline {baseline}: {before.tail}")
    if not before.reported:
        return _blocked(f"{shown}\non the baseline {baseline}, pytest wrote no report "
                        f"(exit {before.exit_code}):\n{before.tail}")

    detecting = after.passed - before.passed
    if not detecting:
        return VerificationOutcome(
            CriterionStatus.FAIL,
            f"{shown}\nevery one of these tests already passes on the baseline "
            f"{baseline}, so none of them detects this change: {_named(after.passed)}. "
            "A test that passes without the change does not test it.",
        )

    by_import = {t for t in detecting if t not in before.not_passed}
    evidence = (
        f"{shown}\n{len(detecting)} of {len(after.passed)} test(s) pass with the change "
        f"and did not pass on the baseline {baseline}: {_named(detecting)}"
    )
    if by_import == detecting and before.uncollectable:
        evidence += (
            "\n\n[supervisor] every one of them counted only because its module could "
            f"not be imported without the change ({_named(before.uncollectable)}). That "
            "shows the tests depend on the new code, not that they assert on it."
        )
    return VerificationOutcome(CriterionStatus.PASS, evidence)


def _remove_worktree(workspace: Path, tree: Path) -> None:
    """Take the worktree down, and its registration with it, whatever state it is in."""
    if _git(workspace, "worktree", "remove", "--force", str(tree)) is None:
        shutil.rmtree(tree, ignore_errors=True)
        _git(workspace, "worktree", "prune")
