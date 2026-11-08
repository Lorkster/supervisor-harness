"""Batch P: a test must catch a break of what it tests.

Minesweeper runs 11, 23 and 24 each failed the owner's chord test with a
different bug in the engine, and each run's own chord test could not have seen
it: one asserted every distance was an integer of at least 1, one checked a
single neighbour, one used a board chosen so that nothing floods. The ledger
had quoted the rule whole and the testers could not read the engine. What
passed the tests was the faithfulness review -- a model -- and rewording or
isolating it, replayed on the local model, only moved which run's test it saw
through.

The owner's own suite is held to a bar that does not need a model's judgement:
it is "proven to discriminate" by breaking a reference engine one way at a
time, and each break must turn a test red (``sabotage.py`` in the hidden
suite). This module holds a run's tests to that bar:

1. a model -- the saboteur -- reads the builder's code and one requirement's
   clauses, and proposes a break per clause: in a file, a string found there
   exactly once, and what it becomes (`SABOTEUR_SYSTEM`, `BREAKS_TOOL`);
2. the harness applies each break *in place*, runs the testing task's command
   with a per-test report, and restores the file byte for byte
   (`try_break`). In place because a fresh git worktree has none of the
   project's installed dependencies; the owner's ``sabotage.py`` restores each
   file the same way;
3. a break that leaves no other test passing proves nothing -- "a bad sabotage
   looks exactly like a bad test" -- and is discarded and said (`invalid`);
4. the verdict is the harness's (`outcome`): it passes when every valid break
   turns a marked test red, fails when one leaves them all green, with the
   break and the green tests as evidence, and is waived -- the faithfulness
   review alone stands for the requirement, as before this batch -- when no
   valid break could be made.

The saboteur proposes; it never rules. Whether the tests notice is a fact the
harness reads from the runner's report. A tester who answers that a break does
not violate its clause reports blocked, and the owner decides (`told`): a model
ruling on the dispute would be the judge this batch removes.

## Scope

Runners whose per-test report carries each test's name, so the tests marked
``[Rn]`` can be told apart: vitest (``--reporter=json``) and Playwright
(``PLAYWRIGHT_JSON_OUTPUT_NAME``). pytest marks a requirement in a docstring or
comment, which its report does not carry; such a criterion is not added.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import Break, CriterionStatus, DoDCriterion, Requirement, VerifyMethod
from .dod import VerificationOutcome, resolve_runner, run_bounded, shell_split, unsafe_command
from .fails_before import is_test_path, safe_relative
from .paths import matches_any

#: Breaks proposed per requirement in one round, at most: one per clause.
PER_REQUIREMENT = 3
#: Browser breaks, each a production build and a browser run (owner, 2026-11-08).
BROWSER_PER_REQUIREMENT = 1
BROWSER_PER_TASK = 5
#: How much of a break's text is shown in evidence and follow-ups.
_SHOWN = 160

VITEST, PLAYWRIGHT = "vitest", "playwright"
_PACKAGE_RUNNERS = ("npm", "pnpm", "yarn")
_TEST_FILE_SUFFIXES = (".test.", ".spec.")


def statement(rid: str) -> str:
    return f"The tests marked [{rid}] catch a break of each clause of it"


def criterion(rid: str, command: str) -> DoDCriterion:
    """The harness's bar for one requirement's tests, run with the testing task's command."""
    return DoDCriterion(statement=statement(rid), method=VerifyMethod.CATCHES_BREAKS,
                        command=command, requirement=rid, mandatory=True)


# -- running a project's tests with a per-test report ---------------------------------


def _script(argv: list[str]) -> str:
    """The package script ``npm test`` / ``npm run x`` names, or ``""``."""
    if len(argv) >= 2 and argv[1] in ("test", "t"):
        return "test"
    if len(argv) >= 3 and argv[1] in ("run", "run-script"):
        return argv[2]
    if argv[0] == "yarn" and len(argv) >= 2 and not argv[1].startswith("-"):
        return argv[1]
    return ""


def runner_of(command: str, tree: Path) -> str:
    """Which runner ``command`` ends in -- ``vitest``, ``playwright`` -- or ``""``."""
    argv = shell_split(command)
    if not argv:
        return ""
    text = " ".join(argv)
    if argv[0] in _PACKAGE_RUNNERS:
        name = _script(argv)
        try:
            scripts = json.loads((tree / "package.json").read_text(encoding="utf-8")
                                 ).get("scripts", {})
        except (OSError, ValueError, AttributeError):
            scripts = {}
        text = str(scripts.get(name, "")) if name else ""
    if "playwright" in text:
        return PLAYWRIGHT
    if "vitest" in text:
        return VITEST
    return ""


def reporting(command: str, runner: str, report: Path) -> tuple[list[str], dict[str, str]]:
    """``command`` with the runner told to write its per-test report to ``report``."""
    argv = shell_split(command)
    extra = ["--reporter=json"]
    env: dict[str, str] = {}
    if runner == VITEST:
        extra.append(f"--outputFile={report}")
    else:
        env["PLAYWRIGHT_JSON_OUTPUT_NAME"] = str(report)
    if argv[0] in ("npm", "pnpm"):
        extra = ["--", *extra]
    return [*argv, *extra], env


def _playwright_tests(suite: dict[str, Any], titles: list[str]) -> list[tuple[str, bool]]:
    here = [*titles, str(suite.get("title", ""))] if suite.get("title") else titles
    out: list[tuple[str, bool]] = []
    for spec in suite.get("specs", []) or []:
        name = " > ".join([*here, str(spec.get("title", ""))])
        for test in spec.get("tests", []) or []:
            status = test.get("status", "")
            if status != "skipped":
                out.append((name, status in ("expected", "flaky")))
    for inner in suite.get("suites", []) or []:
        out += _playwright_tests(inner, here)
    return out


def results(runner: str, report: Path) -> list[tuple[str, bool]] | None:
    """Each test the report names, with whether it passed; ``None`` with no report."""
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if runner == VITEST:
        return [(str(a.get("fullName", a.get("title", ""))), a.get("status") == "passed")
                for f in data.get("testResults", []) or []
                for a in f.get("assertionResults", []) or []
                if a.get("status") in ("passed", "failed")]
    return [t for s in data.get("suites", []) or [] for t in _playwright_tests(s, [])]


def marked(name: str, rid: str) -> bool:
    return f"[{rid}]" in name


# -- one break -------------------------------------------------------------------------


@dataclass
class Tried:
    """What one break did: ``caught``, ``survived`` or ``invalid``, and why."""

    brk: Break
    verdict: str
    red: list[str] = field(default_factory=list)
    green: list[str] = field(default_factory=list)
    note: str = ""


def refusal(brk: Break, tree: Path, builder_paths: list[str],
            tester_paths: list[str]) -> str | None:
    """Why this break may not be applied, or ``None``. Checked before anything runs."""
    rel = safe_relative(brk.file, tree)
    if rel is None:
        return f"{brk.file!r} is not a file in the workspace"
    if is_test_path(rel) or any(s in rel.rsplit("/", 1)[-1] for s in _TEST_FILE_SUFFIXES):
        return f"{rel} is a test file: a break is made in the code the tests test"
    # A tester's scope may hold the code it tests -- run 11's held the engine,
    # before batch M -- so its scope bars only what the builder's does not hold.
    in_builder = not builder_paths or matches_any(rel, builder_paths)
    if tester_paths and matches_any(rel, tester_paths) and not in_builder:
        return f"{rel} is among the testing task's own files"
    if not in_builder:
        return f"{rel} is outside the scope of the work that delivers {brk.requirement}"
    if not brk.find or brk.find == brk.replace:
        return "it changes nothing"
    try:
        count = (tree / rel).read_text(encoding="utf-8").count(brk.find)
    except (OSError, UnicodeDecodeError):
        return f"{rel} could not be read as text"
    if count != 1:
        return f"the text to replace is found {count} times in {rel}, not once"
    return None


def run_tests(tree: Path, command: str, timeout: float,
              brk: Break | None = None) -> tuple[dict[str, bool] | None, str]:
    """The tests and whether each passed, with ``brk`` applied in place if given.

    ``None`` and why when they could not be read. The broken file is put back
    byte for byte however the run ends.
    """
    runner = runner_of(command, tree)
    if not runner:
        return None, "the test command's runner does not report each test by name"
    if unsafe_command(command) is not None:
        return None, "the test command is not one the harness runs"
    path: Path | None = None
    original = b""
    if brk is not None:
        rel = safe_relative(brk.file, tree)
        if rel is None:
            return None, f"{brk.file!r} is not a file in the workspace"
        path = tree / rel
        original = path.read_bytes()
    with tempfile.TemporaryDirectory(prefix="supervisor-break-") as tmp:
        report = Path(tmp) / "report.json"
        argv, env = reporting(command, runner, report)
        executable, _ = resolve_runner(argv[0])
        if executable is None:
            return None, f"{argv[0]!r} is not on PATH"
        argv[0] = executable
        try:
            if path is not None and brk is not None:
                path.write_bytes(original.decode("utf-8").replace(brk.find, brk.replace, 1)
                                 .encode("utf-8"))
            run_bounded(argv, tree, timeout, extra_env=env)
        except subprocess.TimeoutExpired:
            return None, f"the tests did not finish in {timeout:.0f}s"
        except (OSError, UnicodeDecodeError) as exc:
            return None, f"it could not be applied or run: {exc}"
        finally:
            if path is not None:
                path.write_bytes(original)
        tests = results(runner, report)
    if path is not None and path.read_bytes() != original:  # pragma: no cover
        raise RuntimeError(f"{path} was not restored after a break")
    if not tests:
        return None, "the runner reported no tests"
    return dict(tests), ""


def judge_break(brk: Break, before: dict[str, bool],
                after: dict[str, bool] | None, why: str = "") -> Tried:
    """What one break did, read against the tests as they ran with nothing broken.

    A marked test counts as catching the break only if it passed before it.
    A break after which no other test that passed before still passes may have
    broken everything rather than its clause, and is not counted.
    """
    if after is None:
        return Tried(brk, "invalid", note=why or "the tests could not be run with the break")
    rid = brk.requirement
    mine = [n for n, ok in before.items() if ok and marked(n, rid)]
    others = [n for n, ok in before.items() if ok and not marked(n, rid)]
    if not mine:
        return Tried(brk, "invalid", note=f"no test marked [{rid}] passed with nothing broken")
    if not any(after.get(n, False) for n in others):
        return Tried(brk, "invalid", note="no other test passed with the break, so it may "
                                          "have broken everything rather than the clause")
    red = [n for n in mine if not after.get(n, False)]
    green = [n for n in mine if after.get(n, False)]
    return Tried(brk, "caught" if red else "survived", red=red, green=green)


def verify(crit: DoDCriterion, tree: Path, timeout: float, builder_paths: list[str],
           tester_paths: list[str]) -> tuple[VerificationOutcome, list[Tried]]:
    """Run the tests with nothing broken, then with each of the criterion's breaks."""
    rid = crit.requirement
    before, why = run_tests(tree, crit.command, timeout)
    if before is None:
        return VerificationOutcome(
            CriterionStatus.WAIVED,
            f"The tests marked [{rid}] could not be read with nothing broken ({why}), so "
            "its faithfulness review alone stands for it."), []
    tried: list[Tried] = []
    for brk in crit.breaks:
        reason = refusal(brk, tree, builder_paths, tester_paths)
        if reason is not None:
            tried.append(Tried(brk, "invalid", note=reason))
            continue
        after, why = run_tests(tree, crit.command, timeout, brk)
        tried.append(judge_break(brk, before, after, why))
    return outcome(tried, rid), tried


# -- the verdict -----------------------------------------------------------------------


def _clip(text: str) -> str:
    one = " ".join(text.split())
    return one if len(one) <= _SHOWN else one[:_SHOWN] + "..."


def shown(brk: Break) -> str:
    """A break as the evidence and the tester see it: where, and what became what."""
    return f"in {brk.file}, `{_clip(brk.find)}` became `{_clip(brk.replace)}`"


def outcome(tried: list[Tried], rid: str) -> VerificationOutcome:
    """The harness's verdict on one requirement's tests, from the breaks it tried."""
    invalid = [f"- clause {t.brk.clause}, {shown(t.brk)}: not counted, {t.note}"
               for t in tried if t.verdict == "invalid"]
    valid = [t for t in tried if t.verdict != "invalid"]
    if not valid:
        return VerificationOutcome(
            CriterionStatus.WAIVED,
            f"No break of {rid} could be made that left the project running, so its "
            "faithfulness review alone stands for it.\n" + "\n".join(invalid))
    survived = [t for t in valid if t.verdict == "survived"]
    lines = [
        (f"- clause {t.brk.clause}, {shown(t.brk)}: "
         + (f"caught by {_clip(t.red[0])}" if t.verdict == "caught"
            else f"every test marked [{rid}] still passed ({len(t.green)})"))
        for t in valid
    ]
    if survived:
        head = (f"{len(survived)} of {len(valid)} break(s) of {rid} left every test marked "
                f"[{rid}] passing:")
        return VerificationOutcome(CriterionStatus.FAIL, "\n".join([head, *lines, *invalid]))
    head = f"Each of {len(valid)} break(s) of {rid} turned a test marked [{rid}] red:"
    return VerificationOutcome(CriterionStatus.PASS, "\n".join([head, *lines, *invalid]))


def told(tried: list[Tried], req: Requirement, clauses: list[str]) -> str:
    """What the tester is told when a break got through: the break, and how to answer it."""
    rid = req.id
    lines = [f"The harness broke {rid} this way, and every test marked [{rid}] still passed:"]
    for t in tried:
        if t.verdict != "survived":
            continue
        n = t.brk.clause
        said = f' ("{_clip(clauses[n - 1])}")' if 0 < n <= len(clauses) else ""
        lines.append(f"- clause {n}{said}: {shown(t.brk)}")
    lines.append(f"Add or strengthen a test marked [{rid}] so that it fails on that break, "
                 "and passes on the code as it is. If the break does not violate the clause "
                 "it names, report blocked and say why: the owner decides.")
    return "\n".join(lines)


# -- the saboteur ----------------------------------------------------------------------

SABOTEUR_SYSTEM = """\
You break working code on purpose, so the harness can check whether its tests notice. \
You are given one requirement, its clauses numbered, and the files of the work that \
delivers it.

Read the code that implements the requirement. Then, for each clause you can, propose \
one small change that makes the code do something that clause says it must not: a wrong \
value, a missing case, an off-by-one, a step skipped. The code must still compile, load \
and run -- a change that breaks everything proves nothing. Change code, never tests.

Each break is a find and a replace in one file. Copy the text to find exactly from the \
file, long enough to occur there only once. You do not judge the tests; the harness runs \
them against your change.
"""

NUDGE = "Read the code that implements the requirement, then call breaks."
NOW = "Your reading time is up. Call breaks now, with the breaks you have."

BREAKS_TOOL: dict[str, Any] = {
    "name": "breaks",
    "description": "Your proposed breaks of the requirement. Call it once.",
    "parameters": {
        "type": "object",
        "properties": {
            "breaks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "clause": {"type": "integer",
                                   "description": "The number of the clause this violates"},
                        "file": {"type": "string",
                                 "description": "Workspace-relative path of the file to change"},
                        "find": {"type": "string",
                                 "description": "Text copied exactly from the file, found once"},
                        "replace": {"type": "string", "description": "What it becomes"},
                    },
                    "required": ["clause", "file", "find", "replace"],
                },
            },
        },
        "required": ["breaks"],
    },
}

ROLE = (SABOTEUR_SYSTEM, "breaks", NUDGE)


def brief(req: Requirement, clauses: list[str], builder_paths: list[str],
          tester_paths: list[str]) -> str:
    numbered = "\n".join(f"{n}. {c}" for n, c in enumerate(clauses, 1))
    where = ", ".join(f"`{p}`" for p in builder_paths) or "the workspace"
    avoid = ", ".join(f"`{p}`" for p in tester_paths) or "(none)"
    return (
        f"# Break {req.id}\n\n{req.quote}\n\n## Its clauses\n\n{numbered}\n\n"
        f"## Where\n\nThe work that delivers it: {where}. Do not change the tests "
        f"({avoid}) or any other test file.\n\n"
        f"Propose at most one break per clause, {PER_REQUIREMENT} at most in all, "
        "and call breaks."
    )


def parse_breaks(result: dict[str, Any] | None, rid: str) -> list[Break]:
    """The saboteur's proposals as breaks of ``rid``, at most `PER_REQUIREMENT`."""
    raw = (result or {}).get("breaks")
    out: list[Break] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            clause = int(item.get("clause", 0))
        except (TypeError, ValueError):
            clause = 0
        brk = Break(requirement=rid, clause=clause, file=str(item.get("file", "")).strip(),
                    find=str(item.get("find", "")), replace=str(item.get("replace", "")))
        if brk.file and brk.find and all((b.file, b.find, b.replace) !=
                                         (brk.file, brk.find, brk.replace) for b in out):
            out.append(brk)
    return out[:PER_REQUIREMENT]


def capped(breaks: list[Break], browser: bool, taken: int) -> list[Break]:
    """Browser breaks capped per requirement and per task; ``taken`` already used."""
    if not browser:
        return breaks
    return breaks[:max(0, min(BROWSER_PER_REQUIREMENT, BROWSER_PER_TASK - taken))]
