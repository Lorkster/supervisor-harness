"""Batch L: every requirement of the brief, accounted for and tested.

Minesweeper run 4 verified all six of its tasks, and the hidden suite failed
five things -- the mine count, a flood beside a flag, the timer, the best time,
the state changing before an animation -- each a sentence of the brief, none a
criterion of the plan. The verifier judges what the plan asks, and the plan
asked what one planner happened to write down.

So before the plan, a reader lists what the request and the documents it points
to require, each as a quote and where it is (`REQUIREMENTS_SCHEMA`). Only a
quote found in its source is kept (`grounded`): no paraphrase becomes a
requirement. A second reading is shown the list and asked what it leaves out.
The run holds the result, numbered ``R1``, ``R2``, ... (`Requirement`).

The plan then accounts for each: delivered by a task, or set aside with a
reason; and each delivered one tested by a *different* task, written after the
work from the quote, or listed as not tested with a reason
(`coverage_problems`, in the plan's one send-back). `attach_requirements` turns
that into checks: the builder is reviewed against its quotes; the tester must
mark each test with its id, pass its command and have each test judged against
its quote; the tests come after the work they test; and the tester's files are
forbidden to the builder, so the work cannot be passed by rewriting its test.
The reasons for setting aside and not testing are the planner's, and are shown
to the owner. They pass nothing.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..contracts import REQUIREMENTS_SCHEMA, parse_quoted, parse_reasoned
from ..models import DoDCriterion, ExecutionTask, Requirement, VerifyMethod
from .dod import TESTS_BAR
from .paths import globs_may_overlap, globs_within

#: More than this and the plan becomes bookkeeping. The Minesweeper brief, about
#: 1,500 words, states about fifty things someone could check.
MAX_REQUIREMENTS = 60
#: A quote shorter than this, flattened, could be found almost anywhere.
MIN_QUOTE_CHARS = 12
#: How much of a source document a quote is looked for in.
MAX_SOURCE_BYTES = 400_000

_ELLIPSIS = re.compile(r"\.{3,}|…")
_DASHES = re.compile(r"\s*(?:" + chr(0x2014) + "|" + chr(0x2013) + r"|--)\s*")
_MARKS = str.maketrans({"*": None, "`": None, "|": None, chr(0x201C): '"', chr(0x201D): '"',
                        chr(0x2018): "'", chr(0x2019): "'"})
_GLOB = re.compile(r"[*?\[{]")


def _flat(text: str) -> str:
    """Text as a quote is compared: no emphasis, code or table marks, one kind of
    dash and quote, single spaces, any case. A reader copying `**Immutable.**`
    writes "Immutable.", and the brief's `—` comes back as `--` or `-`."""
    return " ".join(_DASHES.sub(" - ", text.translate(_MARKS)).split()).casefold()


def grounded(quote: str, text: str) -> bool:
    """Whether ``quote`` is in ``text``, as `_flat` reads both.

    A quote shortened with an ellipsis is found when each piece is, in order.
    """
    flat = _flat(text)
    pieces = [p for p in (_flat(piece) for piece in _ELLIPSIS.split(quote)) if p]
    if not pieces or max(len(p) for p in pieces) < MIN_QUOTE_CHARS:
        return False
    at = 0
    for piece in pieces:
        found = flat.find(piece, at)
        if found < 0:
            return False
        at = found + len(piece)
    return True


def _source_text(source: str, request: str, workspace: Path) -> str | None:
    """The text a quote's source names: the request, or a file in the workspace."""
    if source.strip().casefold() in ("", "request", "the request", "prompt"):
        return request
    try:
        path = (workspace / source.strip()).resolve()
        path.relative_to(workspace.resolve())
        if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        return None


def build_ledger(
    held: list[Requirement], raw: dict[str, Any], *, request: str, workspace: Path,
    found_by: str,
) -> tuple[list[Requirement], list[str]]:
    """``held`` with what a reading ``raw`` adds, and a note for each quote refused.

    A quote is kept only where it is found: in the source it names, or else in
    the request (a reader that read the brief from the request may name either).
    One already held, or inside one held, or holding one, is the same
    requirement and is not added twice. Numbered on from the last held.
    """
    ledger = list(held)
    notes: list[str] = []
    seen = [(_flat(r.quote), r.source) for r in ledger]
    for item in parse_quoted(raw):
        quote, source = item["quote"], item["source"]
        text = _source_text(source, request, workspace)
        if text is None or not grounded(quote, text):
            if source != "request" and grounded(quote, request):
                source = "request"
            else:
                notes.append(f"requirement not kept: {quote[:120]!r} is not in {source}")
                continue
        flat = _flat(quote)
        if any(src == source and (flat in other or other in flat) for other, src in seen):
            continue
        if len(ledger) >= MAX_REQUIREMENTS:
            notes.append(f"requirements past {MAX_REQUIREMENTS} not kept, from "
                         f"{quote[:80]!r} on")
            break
        seen.append((flat, source))
        ledger.append(Requirement(id=f"R{len(ledger) + 1}", quote=quote, source=source,
                                  found_by=found_by, whole=bool(item["whole"])))
    return ledger, notes


# -- the readings ------------------------------------------------------------

READER_SYSTEM = """\
You list what a request requires of its result, before anyone plans the work. \
What you list is held by the supervisor: the plan must deliver each item, and \
someone other than its builder will write a test for it.

Read the request. If it points to a brief, a specification or other documents in \
the workspace, read them in full: they are part of the request.

List each requirement: one thing someone could check when they try the result -- \
a behaviour, a rule, an interface they rely on, a limit, how it must run. For each, \
quote the words that state it, copied exactly (shorten a long passage with ..., but \
change no word), and say where they are: 'request', or the document's path.

- One requirement per thing to check. A sentence stating three rules is three; \
a table row stating one element's contract is one.
- Leave out background, motivation, examples that only illustrate, and \
instructions about how to work.
- Do not leave out what seems too obvious to state: that is what a plan forgets.
- Mark whole: true where it can only hold once everything is built -- the gates \
that must pass at the end, a 'done when', what every part contributes to -- so it \
is checked last, not on the first task that touches it.
- At most 60. If there are more, merge the smallest into their neighbours.

Then call list_requirements with them."""

AUDIT_SYSTEM = """\
You check a list of requirements another reader made from a request, before \
anyone plans the work. Whatever is missing from the list will not be built or \
tested.

Read the request, and every document in the workspace it points to. Then list \
only what they require that the list below leaves out, quoted exactly from where \
it is written (shorten with ..., change no word) with its source: 'request', or \
the document's path. Not a rewording of an item already listed. If nothing is \
missing, list nothing.

Then call list_requirements."""

LIST_TOOL: dict[str, Any] = {
    "name": "list_requirements",
    "description": "Your answer: the requirements, each quoted with its source. Call it once.",
    "parameters": REQUIREMENTS_SCHEMA,
}

READER_NUDGE = "Read what the request points to, then call list_requirements."
READER_NOW = "Your reading time is up. Call list_requirements now with what you have found."


def reader_prompt(request: str) -> str:
    """The first reading: the request itself."""
    return f"# The request\n\n{request}\n"


def audit_prompt(request: str, ledger: list[Requirement]) -> str:
    """The second reading: the request, and the list to find gaps in."""
    listed = "\n".join(f"- {r.id} ({r.source}{', whole result' if r.whole else ''}): "
                       f"{r.quote}" for r in ledger) or "(empty)"
    return f"# The request\n\n{request}\n\n# The list so far\n\n{listed}\n"


# -- the plan ----------------------------------------------------------------


def synthesis_block(ledger: list[Requirement]) -> str:
    """What the planner is told of the ledger, and what it must do with it."""
    if not ledger:
        return ""
    listed = "\n".join(f"- {r.id}{' (whole result)' if r.whole else ''}: {r.quote}"
                       for r in ledger)
    return (
        "\n\n# Requirements\n\n"
        "What the request requires, quoted. The supervisor holds this list; the plan "
        "must account for every item:\n\n"
        "- Each is delivered by a task: put its id in that task's `requirements`. Or "
        "put it in `set_aside` with why no task delivers it.\n"
        "- Each delivered one is tested by a different task, written after the task "
        "that delivers it, from the requirement as quoted: in the testing task's "
        "`acceptance_tests`, a command that runs those tests and the ids they cover. "
        "Or put it in `not_tested` with why. The testing task's scope is the test "
        "files it writes, which the delivering task may not change; each test's "
        "title carries its id in brackets, like `[R3] ...`.\n"
        "- One marked (whole result) holds only once everything is built: give it to "
        "a task that depends on every task delivering requirements, such as a last "
        "task that runs the final gates.\n\n"
        f"{listed}"
    )


def _deps(tasks: list[ExecutionTask]) -> dict[str, set[str]]:
    """Each task's dependencies, as ids, whether they were written as ids or titles."""
    by_title = {t.title.strip().casefold(): t.id for t in tasks}
    ids = {t.id for t in tasks}
    return {t.id: {d if d in ids else by_title.get(str(d).strip().casefold(), "")
                   for d in t.depends_on} - {""} for t in tasks}


def _depends_on(deps: dict[str, set[str]], task_id: str, other: str) -> bool:
    """Whether ``task_id`` depends on ``other``, directly or through others."""
    seen: set[str] = set()
    stack = list(deps.get(task_id, ()))
    while stack:
        current = stack.pop()
        if current == other:
            return True
        if current not in seen:
            seen.add(current)
            stack.extend(deps.get(current, ()))
    return False


def _listed(ids: list[str]) -> str:
    return ", ".join(ids)


def _tested_ids(task: ExecutionTask) -> list[str]:
    return [rid for group in task.acceptance for rid in group.requirements]


def coverage_problems(ledger: list[Requirement], data: dict[str, Any],
                      tasks: list[ExecutionTask]) -> list[str]:
    """What keeps a plan from accounting for every requirement, a line each.

    For the plan's one send-back, beside its criteria and its goal. Grouped by
    what is wrong, so fifty requirements do not make fifty lines.
    """
    if not ledger or not tasks:
        return []
    known = {r.id for r in ledger}
    set_aside, not_tested = parse_reasoned(data.get("set_aside")), parse_reasoned(
        data.get("not_tested"))
    delivered = {rid for t in tasks for rid in t.requirements}
    tested = {rid for t in tasks for rid in _tested_ids(t)}
    lines = [f"task {t.title!r}: its acceptance tests for {_listed(g.requirements)} name no "
             "command that runs them" for t in tasks for g in t.acceptance if not g.command]
    unknown = list(dict.fromkeys(rid for t in tasks for rid in [*t.requirements, *_tested_ids(t)]
                                 if rid not in known))
    if unknown:
        lines.append(f"{_listed(unknown)} {'is' if len(unknown) == 1 else 'are'} not on the "
                     "list of requirements; use the ids listed")
    missing = [r.id for r in ledger if r.id not in delivered and r.id not in set_aside]
    if missing:
        lines.append(f"no task delivers {_listed(missing)}, and the plan does not set "
                     f"{'it' if len(missing) == 1 else 'them'} aside: put each id in the "
                     "`requirements` of the task that delivers it, or in `set_aside` with why")
    untested = [r.id for r in ledger
                if r.id in delivered and r.id not in tested and r.id not in not_tested]
    if untested:
        lines.append(f"no task tests {_listed(untested)}: put each id in the "
                     "`acceptance_tests` of a task other than the one that delivers it, with "
                     "the command that runs its tests, or in `not_tested` with why")
    lines += _who_tests_whom(tasks)
    lines += _too_early(ledger, tasks)
    lines += [f"{rid} is set aside or not tested without a reason; say why"
              for rid, reason in {**set_aside, **not_tested}.items() if not reason]
    return list(dict.fromkeys(lines))


def _who_tests_whom(tasks: list[ExecutionTask]) -> list[str]:
    """A task testing its own work, or work that is built after its tests."""
    deps = _deps(tasks)
    lines: list[str] = []
    for task in tasks:
        own = [rid for rid in _tested_ids(task) if rid in task.requirements]
        if own:
            lines.append(f"task {task.title!r} tests {_listed(own)}, which it also delivers: "
                         "a requirement is tested by a task other than the one that builds it")
        lines += [f"task {task.title!r} tests {rid}, but {builder.title!r}, which delivers it, "
                  "depends on it: the tests come after the work they test"
                  for rid in _tested_ids(task) for builder in tasks
                  if builder is not task and rid in builder.requirements
                  and _depends_on(deps, builder.id, task.id)]
    return lines


def _not_yet_built(task: ExecutionTask, tasks: list[ExecutionTask],
                   whole: set[str], deps: dict[str, set[str]]) -> list[ExecutionTask]:
    """The tasks building a part of the result that ``task`` does not come after.

    A part is a task that delivers a requirement of less than the whole; in a
    plan with no requirements, every task is one.
    """
    builders = [t for t in tasks if set(t.requirements) - whole]
    return [t for t in builders or tasks
            if t is not task and not _depends_on(deps, task.id, t.id)]


def unbuilt_parts(task: ExecutionTask, tasks: list[ExecutionTask],
                  whole: set[str]) -> list[ExecutionTask]:
    """`_not_yet_built`, for a caller holding tasks whose dependencies may be titles."""
    return _not_yet_built(task, tasks, whole, _deps(tasks))


def furthest_after_parts(tasks: list[ExecutionTask], whole: set[str],
                         skip: set[str] | None = None) -> ExecutionTask | None:
    """The task furthest along among those after every part, ``skip`` ids aside."""
    deps = _deps(tasks)
    able = [t for t in tasks if t.id not in (skip or set())
            and not _not_yet_built(t, tasks, whole, deps)]
    # The furthest along: one no other candidate comes after.
    return next((t for t in able if not any(_depends_on(deps, o.id, t.id) for o in able)),
                None)


def _too_early(ledger: list[Requirement], tasks: list[ExecutionTask]) -> list[str]:
    """A requirement of the whole result, given to a task before the parts are built.

    Minesweeper run 6: the brief's "done" clause -- both gates pass, the tests
    cover the rules -- went to the scaffold, the first task, whose verifier
    then found no browser tests and failed it, and every task behind it waited.
    """
    whole = {r.id for r in ledger if r.whole}
    deps = _deps(tasks)
    lines = []
    for task in tasks:
        for rid in [r for r in task.requirements if r in whole]:
            early = _not_yet_built(task, tasks, whole, deps)
            if early:
                lines.append(
                    f"{rid} holds only for the whole result, but {task.title!r} delivers it "
                    f"before {', '.join(repr(t.title) for t in early)}: give it to a task "
                    "that depends on every task delivering requirements")
    return lines


def _deliver_last(tasks: list[ExecutionTask], whole: set[str],
                  notes: dict[str, list[str]]) -> None:
    """Move a whole-result requirement given too early to a task after every part.

    What the send-back did not put right: the first task, in plan order, that
    comes after every task delivering a part, and does not test it. Left where
    it is, and said, when there is none.
    """
    deps = _deps(tasks)
    for task in tasks:
        for rid in [r for r in task.requirements if r in whole]:
            if not _not_yet_built(task, tasks, whole, deps):
                continue
            last = furthest_after_parts(tasks, whole, skip={task.id} | {
                t.id for t in tasks
                if rid in {r for g in t.acceptance for r in g.requirements}})
            if last is None:
                continue
            task.requirements.remove(rid)
            last.requirements.append(rid)
            notes.setdefault(task.id, []).append(
                f"{rid} holds only for the whole result: moved to {last.title!r}, which "
                "comes after every task delivering a part")
            notes.setdefault(last.id, []).append(
                f"delivers {rid}, moved from {task.title!r}: it holds only for the whole result")


def _tests_root(paths: list[str]) -> str:
    """The one directory, or file, a testing task's scope comes down to."""
    bases = []
    for pattern in paths:
        base = _GLOB.split(pattern, maxsplit=1)[0].rstrip("/")
        if _GLOB.search(pattern):
            base = base if pattern[len(base):].startswith("/") else posixpath.dirname(base)
        bases.append(base or ".")
    return (posixpath.commonpath(bases) or ".") if bases else "."


def _quotes(ledger: dict[str, Requirement], ids: list[str]) -> str:
    return "\n".join(f"- {rid}: {ledger[rid].quote}" for rid in ids if rid in ledger)


def builder_criterion(ledger: dict[str, Requirement], ids: list[str]) -> DoDCriterion:
    """The review that holds a task to the requirements it delivers, as quoted."""
    return DoDCriterion(
        statement=f"Delivers {_listed(ids)} as the request states them",
        method=VerifyMethod.REVIEW,
        rubric=("Pass only if the result does each of these as quoted; fail naming each "
                "one it does not, and cite file:line for what you find:\n"
                + _quotes(ledger, ids)),
        mandatory=True,
    )


def tester_criteria(ledger: dict[str, Requirement], ids: list[str],
                    root: str) -> list[DoDCriterion]:
    """A testing task's checks: each id marked in its tests, and each test faithful."""
    marked = [DoDCriterion(statement=f"A test is marked [{rid}]",
                           method=VerifyMethod.INSPECTION, expect=f"{root}: [{rid}]",
                           mandatory=True)
              for rid in ids]
    return [*marked, DoDCriterion(
        statement=f"The tests marked {_listed(ids)} check what each requirement says",
        method=VerifyMethod.REVIEW,
        rubric=("Pass only if each test marked with an id drives the result as a person "
                "or a caller would and asserts what that requirement says -- not the "
                "implementation's internals, and not a weaker claim. Fail naming each test "
                "that does not, and cite file:line:\n" + _quotes(ledger, ids)),
        mandatory=True,
    )]


def attach_requirements(
    tasks: list[ExecutionTask], ledger: list[Requirement], data: dict[str, Any],
) -> tuple[list[Requirement], dict[str, list[str]]]:
    """The plan's account of the ledger, turned into checks; the ledger as accounted.

    After the dependencies are ids. Each builder gains a review of its quotes;
    each tester its marks, its faithfulness review (its commands were added
    with its criteria), a dependency on each builder it tests, and its files
    among the builder's forbidden paths. Returns the ledger with the plan's
    reasons recorded, and notes per task id for the owner.
    """
    notes: dict[str, list[str]] = {}
    if not ledger:
        return ledger, notes
    set_aside, not_tested = parse_reasoned(data.get("set_aside")), parse_reasoned(
        data.get("not_tested"))
    accounted = [replace(r, set_aside=set_aside.get(r.id, ""),
                         not_tested=not_tested.get(r.id, "")) for r in ledger]
    by_id = {r.id: r for r in accounted}
    deps = _deps(tasks)
    for task in tasks:
        task.requirements = [rid for rid in task.requirements if rid in by_id]
    _deliver_last(tasks, {r.id for r in accounted if r.whole}, notes)
    for task in tasks:
        if task.requirements:
            task.dod.append(builder_criterion(by_id, task.requirements))
    for tester in tasks:
        ids = list(dict.fromkeys(rid for g in tester.acceptance for rid in g.requirements
                                 if rid in by_id and rid not in tester.requirements))
        if not ids:
            continue
        tester.dod += tester_criteria(by_id, ids, _tests_root(tester.scope.paths))
        for builder in tasks:
            if builder is tester or not set(ids) & set(builder.requirements):
                continue
            if _depends_on(deps, builder.id, tester.id):
                notes.setdefault(tester.id, []).append(
                    f"tests work by {builder.title!r}, which depends on this task: the tests "
                    "may be written before the work they test")
            elif builder.id not in tester.depends_on:
                tester.depends_on.append(builder.id)
                deps[tester.id].add(builder.id)
                notes.setdefault(tester.id, []).append(
                    f"depends on {builder.title!r}: the tests come after the work they test")
            _fence(builder, tester, notes)
            _blind(builder, tester, notes)
    for builder in tasks:
        testers = [t.title for t in tasks if t is not builder and set(builder.requirements)
                   & {rid for g in t.acceptance for rid in g.requirements}]
        if testers and _one_suite(builder, testers):
            notes.setdefault(builder.id, []).append(
                "the harness's test bar asks only that the suite passes: the tests of its "
                f"requirements are written by {', '.join(repr(t) for t in testers)}")
    return accounted, notes


def _fence(builder: ExecutionTask, tester: ExecutionTask, notes: dict[str, list[str]]) -> None:
    """The tester's files, forbidden to the builder whose work they test."""
    for pattern in tester.scope.paths:
        if pattern in builder.scope.forbidden_paths:
            continue
        if any(globs_within([own], [pattern]) for own in builder.scope.paths):
            notes.setdefault(builder.id, []).append(
                f"its scope lies inside {pattern!r}, where {tester.title!r} writes the tests "
                "of its work; not fenced off")
            continue
        builder.scope.forbidden_paths.append(pattern)
        notes.setdefault(builder.id, []).append(
            f"may not change {pattern!r}: {tester.title!r} writes the tests of its work there")


def _blind(builder: ExecutionTask, tester: ExecutionTask, notes: dict[str, list[str]]) -> None:
    """The builder's files, not shown to the tester of its work.

    Minesweeper run 11 verified every task and failed four of the owner's
    hidden tests, all on chord. The engine listed the chorded cell in
    `lastRevealed`, and its comment said why; the tester's test said the same,
    in nearly the same words, and asserted it. Its brief already said to write
    each test "from the words quoted, not from the code". And its boards never
    put a mine where the engine's win check flagged one on the way -- a test
    shaped by the code it tests goes where the code works. A tester that cannot
    read the code tests what the request says, by running the result.

    Not where the tester's own files may be among them: it must read what it
    writes.
    """
    hidden = [p for p in builder.scope.paths
              if p not in tester.scope.unread_paths
              and not globs_may_overlap([p], tester.scope.paths)]
    if not hidden:
        return
    tester.scope.unread_paths.extend(hidden)
    notes.setdefault(tester.id, []).append(
        f"is not shown {', '.join(hidden)}: it tests {builder.title!r}'s work from the "
        "requirements, by running it")


def _one_suite(builder: ExecutionTask, testers: list[str]) -> bool:
    """The test bar of a builder whose requirements another task tests: the suite passes.

    Minesweeper run 6: the bar asked the engine's builder for tests of its own,
    and it wrote `tests/engine.test.ts` beside the unit tests a separate task
    was writing from the brief. Two suites of the same rules disagreed, one
    broke `npm test`, and the finding about it cost a third task its last
    attempt. The tester's suite is the one; the builder keeps the suite green.

    Mandatory where the project had a suite when the plan was made: its tests
    must not break. In a project built from nothing the builder comes before
    any test, and a runner with none to run fails -- so there it is run and
    reported, and the tester, after it, carries the bar; what the tester finds
    broken goes back to the builder as a finding.
    """
    for i, crit in enumerate(builder.dod):
        if crit.statement == TESTS_BAR:
            builder.dod[i] = replace(crit, mandatory=bool(crit.command.strip()), statement=(
                "The project's test suite passes; the tests of this task's requirements are "
                f"written by {', '.join(repr(t) for t in testers)}"))
            return True
    return False


def report_lines(ledger: list[Requirement], tasks: dict[str, ExecutionTask]) -> list[str]:
    """The ledger in the run's report: each requirement, and what became of it."""
    if not ledger:
        return []
    lines = ["## Requirements", ""]
    for req in ledger:
        builders = [t for t in tasks.values() if req.id in t.requirements]
        testers = [t for t in tasks.values()
                   if any(req.id in g.requirements for g in t.acceptance)]
        quote = " ".join(req.quote.split())
        whole = ", whole result" if req.whole else ""
        lines.append(f"- **{req.id}** ({req.source}{whole}): {quote[:200]}")
        if req.set_aside:
            lines.append(f"    - set aside: {req.set_aside}")
        for label, group in (("delivered by", builders), ("tested by", testers)):
            if group:
                lines.append(f"    - {label}: " + "; ".join(
                    f"{t.title} ({t.status.value})" for t in group))
        if req.not_tested:
            lines.append(f"    - not tested: {req.not_tested}")
        if not req.set_aside and not builders:
            lines.append("    - **not accounted for**")
    lines.append("")
    return lines
