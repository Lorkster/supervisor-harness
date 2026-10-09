"""How a role's output is judged.

Two kinds, kept apart on purpose:

- **generic** checks are properties that hold for any project, green field
  included -- a scope names something that exists or that the task will
  create; a criterion is one the harness can enforce; a veto points at a file.
  A case that lists no checks is judged by its role's generic set.
- **labelled** checks carry a person's expectation for one case -- this task
  should be vetoed; no task should wire the browser suite into ``check``. They
  are only as good as the person who wrote them, which is why a case says
  who that was (``labelled_by``).

These are measurements of a model, never gates on a run: a check here that
reads prose is reporting, not deciding anything.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import Policy
from ..core import phases
from ..core.dod import fill_suite_commands, inspectable
from ..core.paths import _META
from ..models import CriterionStatus, ExecutionTask, VerifyMethod
from .cases import Case
from .roles import RoleOutput

Result = tuple[bool, str]
Check = Callable[[Case, RoleOutput, Path, dict[str, Any]], Result]

GENERIC: dict[str, tuple[str, ...]] = {
    "planner": ("answered", "criteria_enforceable", "mechanical_criterion",
                "scopes_specific", "scopes_grounded", "inspections_grounded"),
    "review": ("answered", "ruled", "veto_cites_file"),
    "verifier": ("answered", "all_ruled", "evidence_cites_file"),
}

#: A file named in prose: `ci.yml`, `src/app/x.tsx:31`.
_FILE_CITED = re.compile(r"[\w./-]+\.[A-Za-z]{1,5}\b")


def _grounded(path: str, workspace: Path, greenfield: bool) -> bool:
    """A path that exists, or that a task could create where it says."""
    clean = path.strip().rstrip("/")
    if not clean or clean.startswith(("/", "..")) or ":" in clean[:3]:
        return False
    cut = min((clean.index(ch) for ch in _META if ch in clean), default=len(clean))
    head = clean[:cut].rsplit("/", 1)[0] if cut < len(clean) else clean
    if greenfield:
        return True
    target = workspace / head
    return target.exists() or target.parent.is_dir()


def answered(case: Case, out: RoleOutput, ws: Path, p: dict[str, Any]) -> Result:
    """The call returned something usable."""
    if out.error:
        return False, out.error[:300]
    if case.role == "planner" and not out.tasks:
        return False, "proposed no tasks"
    return True, ""


def criteria_enforceable(case: Case, out: RoleOutput, ws: Path, p: dict[str, Any]) -> Result:
    """No proposed criterion is one the harness would have to send back."""
    tasks = [_filled(t, ws) for t in out.tasks]
    weak = phases.unenforceable_criteria(tasks, Policy())
    return not weak, "; ".join(weak)[:600]


def _filled(task: ExecutionTask, ws: Path) -> ExecutionTask:
    fill_suite_commands(task, ws)   # what the harness fills in is not the model's miss
    return task


def mechanical_criterion(case: Case, out: RoleOutput, ws: Path, p: dict[str, Any]) -> Result:
    """Every task has a criterion the harness checks itself, not by a judge."""
    def checked(task: ExecutionTask) -> bool:
        return any((c.method in (VerifyMethod.COMMAND, VerifyMethod.TEST) and c.command.strip())
                   or (c.method is VerifyMethod.INSPECTION and inspectable(c.expect))
                   for c in task.dod)
    missing = [t.title for t in out.tasks if not checked(t)]
    return not missing, f"no mechanical criterion: {missing}" if missing else ""


def scopes_specific(case: Case, out: RoleOutput, ws: Path, p: dict[str, Any]) -> Result:
    """Each task names what it may change, and no two name the same.

    Measured across thirteen go-live runs: 39 of 70 task scopes were the whole
    run envelope -- every task allowed to change everything, so nothing kept a
    task to its own work.
    """
    if any(not t.scope.paths for t in out.tasks):
        return False, "a task's scope is the whole workspace"
    seen: dict[frozenset[str], str] = {}
    for task in out.tasks:
        key = frozenset(task.scope.paths)
        if key in seen:
            return False, f"{task.title!r} and {seen[key]!r} have the same scope"
        seen[key] = task.title
    return True, ""


def scopes_grounded(case: Case, out: RoleOutput, ws: Path, p: dict[str, Any]) -> Result:
    """Every scope path exists, or sits where the task could create it."""
    bad = [f"{t.title!r}: {path}" for t in out.tasks for path in t.scope.paths
           if not _grounded(path, ws, case.greenfield)]
    return not bad, "; ".join(bad)[:600]


def inspections_grounded(case: Case, out: RoleOutput, ws: Path, p: dict[str, Any]) -> Result:
    """An inspection looks in a file that exists or that its task creates."""
    bad = []
    for task in out.tasks:
        for crit in task.dod:
            if crit.method is VerifyMethod.INSPECTION and inspectable(crit.expect):
                path = crit.expect.partition(":")[0].strip()
                if not _grounded(path, ws, case.greenfield):
                    bad.append(f"{task.title!r}: {path}")
    return not bad, "; ".join(bad)[:600]


def ruled(case: Case, out: RoleOutput, ws: Path, p: dict[str, Any]) -> Result:
    """The reviewer gave a ruling, not silence."""
    return bool(out.ruling), "" if out.ruling else "no ruling"


def veto_cites_file(case: Case, out: RoleOutput, ws: Path, p: dict[str, Any]) -> Result:
    """A veto points at the file that justifies it."""
    if out.ruling in ("", "proceed"):
        return True, ""
    return bool(_FILE_CITED.search(out.why)), "" if _FILE_CITED.search(out.why) else out.why[:200]


def verdict_in(case: Case, out: RoleOutput, ws: Path, p: dict[str, Any]) -> Result:
    """Labelled: the ruling is one of these (``proceed`` or veto names)."""
    wanted = [str(v) for v in p.get("verdicts", [])]
    if "veto" in wanted and out.ruling not in ("", "proceed"):
        return True, out.ruling
    return out.ruling in wanted, f"ruled {out.ruling or 'nothing'}; wanted {wanted}"


def all_ruled(case: Case, out: RoleOutput, ws: Path, p: dict[str, Any]) -> Result:
    """The verifier ruled on every criterion it was handed."""
    open_ = [c["statement"][:60] for c in out.criteria
             if c["status"] == str(CriterionStatus.UNVERIFIED)]
    return not open_, f"unjudged: {open_}" if open_ else ""


def evidence_cites_file(case: Case, out: RoleOutput, ws: Path, p: dict[str, Any]) -> Result:
    """Every verdict cites the file it rests on."""
    bare = [c["statement"][:60] for c in out.criteria
            if c["status"] != str(CriterionStatus.UNVERIFIED)
            and not _FILE_CITED.search(c["evidence"])]
    return not bare, f"no file cited: {bare}" if bare else ""


def statuses(case: Case, out: RoleOutput, ws: Path, p: dict[str, Any]) -> Result:
    """Labelled: criteria whose statement contains a key end with that status."""
    wrong = []
    for key, want in dict(p.get("expect", {})).items():
        got = [c["status"] for c in out.criteria if key.lower() in c["statement"].lower()]
        if got != [want] * len(got) or not got:
            wrong.append(f"{key!r}: {got or 'absent'}, wanted {want}")
    return not wrong, "; ".join(wrong)


def no_task_matches(case: Case, out: RoleOutput, ws: Path, p: dict[str, Any]) -> Result:
    """Labelled: no task's action or criteria match ``pattern`` (a regex)."""
    pattern = re.compile(str(p.get("pattern", "")), re.IGNORECASE)
    hits = [t.title for t in out.tasks
            if pattern.search(" ".join([t.title, t.action,
                                        *(f"{c.statement} {c.command} {c.expect}"
                                          for c in t.dod)]))]
    return not hits, f"matched: {hits}" if hits else ""


def some_scope_covers(case: Case, out: RoleOutput, ws: Path, p: dict[str, Any]) -> Result:
    """Labelled: some task may change ``path``."""
    from ..core.paths import matches_any

    path = str(p.get("path", ""))
    ok = any(matches_any(path, t.scope.paths) for t in out.tasks)
    return ok, "" if ok else f"no task's scope covers {path}"


def task_count(case: Case, out: RoleOutput, ws: Path, p: dict[str, Any]) -> Result:
    """Labelled: the plan has between ``min`` and ``max`` tasks."""
    lo, hi = int(p.get("min", 1)), int(p.get("max", 99))
    return lo <= len(out.tasks) <= hi, f"{len(out.tasks)} tasks; wanted {lo}-{hi}"


CHECKS: dict[str, Check] = {f.__name__: f for f in (
    answered, criteria_enforceable, mechanical_criterion, scopes_specific, scopes_grounded,
    inspections_grounded, ruled, veto_cites_file, verdict_in, all_ruled, evidence_cites_file,
    statuses, no_task_matches, some_scope_covers, task_count)}


def judge(case: Case, out: RoleOutput, workspace: Path) -> dict[str, dict[str, Any]]:
    """Every check the case asks for, or its role's generic set, with the result of each."""
    wanted = case.checks or [{"check": name} for name in GENERIC[case.role]]
    results: dict[str, dict[str, Any]] = {}
    for spec in wanted:
        name = str(spec.get("check", ""))
        check = CHECKS.get(name)
        if check is None:
            results[name] = {"passed": False, "detail": "no such check"}
            continue
        passed, detail = (False, "the call failed") if out.error and name != "answered" \
            else check(case, out, workspace, spec)
        results[name] = {"passed": passed, "detail": detail}
    return results
