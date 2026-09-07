"""Brief construction.

A brief is the whole contract with an agent: what to do, what not to touch, who
else is working nearby, what past runs learned, and the exact shape of the answer.
Everything the supervisor later measures -- scope fidelity, objective coverage,
drift -- is measured against text written here, so the sections are deliberately
explicit rather than conversational.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..models import (
    BASELINE_FACT,
    AgentSpec,
    Directive,
    DoDCriterion,
    ExecutionTask,
    Lesson,
    Message,
    RunState,
)
from .roles import Role

# Non-negotiables. These are the behaviours the supervisor actively checks for,
# so they are stated to the agent in the same terms it will be judged by.
CORE_RULES = [
    "Ground every claim in evidence you gathered yourself. Cite file:line, quoted "
    "code, or real command output. An assertion with no evidence is a finding you "
    "have not made yet.",
    "Stay inside your scope. If you find something important but out of scope, do "
    "not act on it -- send a message about it and carry on with your objectives.",
    "Say what you could not establish. An honest gap is more useful than a "
    "confident guess, and the supervisor treats padding as a quality failure.",
    "Do not restate your brief back as if it were a result. You are judged on what "
    "you found or changed, not on your plan to find or change it.",
    "Answer only with the JSON object described in the output contract.",
]

# The one prohibition a path scope cannot express, so it is written out rather
# than left to be inferred. Agents in a run share a single working tree,
# separated only by their scopes, and git does not respect a scope: ``git stash``,
# ``git checkout`` and their relatives act on every file at once, including files
# another agent is part-way through writing. A real run had an agent stash and
# pop the whole tree to get itself a clean lint baseline while eight others were
# working in it. Nothing was lost, and nothing but timing prevented it.
SHARED_TREE_RULE = (
    "You share one working tree with the other agents in this run, and your path "
    "scope does not constrain git. Never run `git stash`, `git checkout`, "
    "`git clean`, `git reset` or `git rebase`, nor any other git command that "
    "changes tracked files or moves HEAD (`switch`, `restore`, `merge`, `revert`, "
    "`cherry-pick`, `apply`, `pull`): each acts on the whole tree at once, "
    "including files another agent is part-way through writing, and a `git stash` "
    "that a peer's write lands in the middle of destroys work that was never "
    "yours. Do not `git commit` either -- a commit of this tree captures several "
    "agents' unfinished work as one change. This rule is about the tree, so it "
    "does not forbid read-only inspection (`git status`, `git diff`, `git log`) "
    "-- but whether you can run those at all depends on who runs your commands: "
    "through your host's own tools they work, while the harness's own shell "
    "offers only the project's check runners and no git at all, read-only or "
    "not. If you need a clean tree to measure something, you cannot have one: "
    "say so in your output and measure against the run's baseline commit "
    "instead."
)


@dataclass(frozen=True)
class BriefRefs:
    """Where a by-reference brief points for the things it does not carry.

    Empty strings mean "carry it inline", which is what an autonomous run gets
    and what a host that cannot read files gets. One object rather than three
    keyword arguments on each of three builders: they always travel together,
    and a builder given the contract path but not the result path is a bug
    rather than a configuration.
    """

    rules: str = ""
    contract: str = ""
    result: str = ""


def _section(title: str, body: str) -> str:
    body = body.strip()
    return f"## {title}\n{body}\n" if body else ""


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _lessons_block(lessons: list[Lesson], workspace: str = "") -> str:
    """Lessons for the brief, each tagged with where it was learned.

    The library is shared across every workspace the harness has run in, so a
    lesson reaching this brief may have been drawn from a different project
    entirely. Untagged, they all read as "this is how things go here", and an
    agent has no way to tell a local convention from a borrowed one -- which is
    exactly the judgement it needs to make when the two disagree.

    Named by the origin workspace's own directory rather than its full path: a
    brief is model input, and someone's disk layout does not belong in it.
    """
    if not lessons:
        return ""
    lines = []
    for lesson in lessons:
        origin = lesson.origin_label(workspace)
        where = "learned here" if origin == "here" else f"learned in {origin}"
        lines.append(f"- **{lesson.statement}**  _({where}, seen {lesson.occurrences}x)_")
        if lesson.how_to_apply:
            lines.append(f"  - Apply it by: {lesson.how_to_apply}")
    return (
        "Earlier runs went wrong in these specific ways. Do not repeat them.\n\n"
        + "\n".join(lines)
        + "\n\nA lesson learned in another project is evidence, not a rule: where it "
        "conflicts with what you can see in this workspace, this workspace wins, and "
        "say so in your output."
    )


def _peers_block(peers: list[AgentSpec], self_id: str) -> str:
    others = [p for p in peers if p.id != self_id]
    if not others:
        return ""
    lines = [f"- `{p.id}` -- {p.title}: {', '.join(p.objectives[:2]) or p.role}" for p in others]
    return (
        "These agents are working at the same time as you:\n\n"
        + "\n".join(lines)
        + "\n\nUse the `messages` field to reach them. Send a message when you find "
        "something that changes another agent's work, when you need a fact only they "
        "have, or when you believe one of them is wrong. Address the supervisor as "
        "`supervisor` and broadcast with `*`. Messages are delivered by the supervisor "
        "before the recipient's next turn; do not wait for a reply within this turn."
    )


def _working_tree_line(rules_ref: str) -> str:
    """The shared-tree rule, or a pointer to where it is written out in full.

    The rule is a kilobyte, it is byte-identical in every brief of every run,
    and it is one of the few things in a brief that an agent must not skim. A
    pointer is the better shape for both facts -- read once, applied throughout
    -- but only when there is a file to point at, so the full text stays the
    default and nothing is lost on a host that cannot read one.
    """
    if not rules_ref:
        return f"Working tree: {SHARED_TREE_RULE}"
    return (
        f"Working tree: read **Shared working tree** in `{rules_ref}` before you "
        "touch anything. It forbids specific git commands, it is binding, and "
        "breaking it destroys another agent's unfinished work rather than yours."
    )


def _scope_block(agent: AgentSpec, role: Role | None, rules_ref: str = "") -> str:
    lines: list[str] = []
    if agent.scope.paths:
        lines.append("In scope (paths): " + ", ".join(f"`{p}`" for p in agent.scope.paths))
    if agent.scope.topics:
        lines.append("In scope (topics): " + ", ".join(agent.scope.topics))
    forbidden = list(agent.scope.forbidden_paths)
    if forbidden:
        lines.append("Never modify: " + ", ".join(f"`{p}`" for p in forbidden))
    out = list(agent.scope.out_of_scope) + list(role.out_of_scope if role else [])
    if out:
        lines.append("Out of scope -- do not pursue these, even if they look worthwhile:")
        lines.append(_bullets(sorted(set(out))))
    # Its own paragraph: a bullet list runs into the line after it otherwise.
    lines.append("")
    lines.append(_working_tree_line(rules_ref))
    return "\n".join(lines)


def _baseline_block(run: RunState, rules_ref: str = "") -> str:
    """What a whole-repository measurement is measured *against*.

    Every agent in a run writes into the same tree, so a criterion like "the
    existing test suite still passes" has no fixed answer: it returns whatever
    the tree happened to hold when it was asked. One run had that criterion on
    every task and got 87, 99 and 100 tests back from three verifiers, each of
    which then had to reason about why its own number differed. Naming the commit
    the run started from turns the question into a comparison an agent can
    actually make, and makes two agents' numbers mean the same thing.
    """
    baseline = run.facts.get(BASELINE_FACT, "")
    lead = (
        f"This run's baseline is commit {baseline}."
        if baseline
        else "This run has no recorded baseline commit: the workspace is not a git "
        "repository, or git could not be reached."
    )
    # The commit stays inline whichever way the brief is carried. It is one line,
    # it differs per run, and it is the value an agent has to quote back in a
    # measurement -- so a pointer would be indirection with nothing saved.
    if rules_ref:
        return lead + (
            f"\n\nHow to measure anything whole-repository against it: **Measuring "
            f"against the baseline** in `{rules_ref}`. Read it before you report a "
            "test count, a lint result or a build outcome."
        )
    body = (
        "The tree in front of you is not the baseline plus your own change. Other "
        "agents are writing into it while you work, so any whole-repository "
        "measurement -- the test suite passes, the linter is clean, the build "
        "succeeds -- moves while you take it, and will not match the same "
        "measurement taken by another agent.\n\n"
        "Measure such a criterion against the baseline plus your own diff:\n"
    )
    return lead + "\n\n" + body + _bullets(
        [
            "Report the figure you actually observed and name the baseline it is "
            "relative to. A bare count with nothing to compare it against settles "
            "nothing.",
            "A failure counts against this task only if it is in a file this task "
            "touched, or you can trace it to this task's change.",
            "A failure you cannot trace to this task belongs to a peer or to the "
            "baseline. Say so, send a message about it, and neither fix it nor "
            "fail this task for it.",
            "Do not try to get yourself a clean tree -- see the working-tree rule "
            "under Scope. Compare against the baseline instead.",
        ]
    )


def _budget_block(agent: AgentSpec) -> str:
    budget = agent.budget
    parts = [f"You have at most {budget.max_turns} turns."]
    if budget.max_seconds:
        parts.append(f"Wall-clock limit: {budget.max_seconds}s.")
    parts.append(
        "Report `status: done` as soon as every objective is addressed -- finishing "
        "early is a good outcome, and burning turns to look thorough is not."
    )
    return " ".join(parts)


def render_contract(
    schema: dict[str, Any], contract_ref: str = "", result_ref: str = ""
) -> str:
    """The answer's shape, inline or by reference.

    Pretty-printed, the analysis schema alone is a little over five thousand
    characters, and it is the same five thousand for every analysis agent in
    every run. Written once and pointed at, it costs a line.

    ``result_ref`` changes where the answer goes, not what it is: an agent that
    writes its JSON to a file keeps a large finding set out of the caller's
    context on the way back, the same way the brief keeps it out on the way in.
    """
    if not contract_ref:
        return (
            "Reply with exactly one JSON object matching this schema. No prose outside "
            "it, no code fence.\n\n```json\n"
            + json.dumps(schema, indent=2)
            + "\n```"
        )
    lines = [
        f"Your answer is one JSON object matching the schema in `{contract_ref}`. "
        "Read that file; do not guess the shape from the section headings above.",
    ]
    if result_ref:
        lines.append(
            f"\nWrite that object to `{result_ref}` -- the file, not your reply -- and "
            "then say in one line that you have written it. Nothing else you say is "
            "read as the answer."
        )
    else:
        lines.append("\nReply with the object and nothing else: no prose, no code fence.")
    return "\n".join(lines)


def build_rules_document(run: RunState) -> str:
    """The run-scoped rules every brief in the run points at.

    Two blocks that were repeated verbatim in every brief: the shared-tree rule
    and how to measure a whole-repository claim against the baseline. Neither
    varies by agent, both are long, and one of them is the rule an agent is most
    likely to skim past when it arrives buried in its own scope section.

    Written per run rather than per package because the baseline commit is a
    property of the run, and a rules document that named a different run's
    baseline would be worse than no rules document.
    """
    return "\n".join(
        [
            f"# Rules for run {run.id}",
            "",
            "Every brief in this run points here. Read it once; it applies to all of",
            "your turns.",
            "",
            "## Shared working tree",
            "",
            SHARED_TREE_RULE,
            "",
            "## Measuring against the baseline",
            "",
            _baseline_block(run),
            "",
        ]
    )


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------


def build_analysis_brief(
    run: RunState,
    agent: AgentSpec,
    role: Role | None,
    peers: list[AgentSpec],
    schema: dict[str, Any],
    shared_context: str = "",
    lessons: list[Lesson] | None = None,
    tools: str = "",
    refs: BriefRefs | None = None,
) -> str:
    """Brief for one analysis lens."""
    refs = refs or BriefRefs()
    focus = role.focus_questions if role else []
    parts = [
        f"# Analysis brief: {agent.title}\n\n"
        f"You are agent `{agent.id}` in a supervised run. You are one of several "
        f"agents examining the same task from different angles, in parallel.",
        _section("The task", run.prompt),
        _section("Shared context", shared_context),
        _section("Your lens", (role.charter if role else agent.brief)),
        _section(
            "Objectives",
            _numbered(agent.objectives)
            + "\n\nEvery objective must be addressed in your output, including any you "
            "conclude is not applicable -- say so and say why.",
        ),
        _section("Questions to answer", _bullets(focus)) if focus else "",
        _section("Scope", _scope_block(agent, role, refs.rules)),
        _section("Tools", tools),
        _section("Other agents", _peers_block(peers, agent.id)),
        _section("Lessons from previous runs", _lessons_block(lessons or [], run.workspace)),
        _section("Rules", _bullets(CORE_RULES)),
        _section("Budget", _budget_block(agent)),
        _section("Output contract", render_contract(schema, refs.contract, refs.result)),
    ]
    return "\n".join(p for p in parts if p).strip()


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def _dod_block(criteria: list[DoDCriterion]) -> str:
    lines = [
        "This task is not done until every mandatory criterion below is verified by "
        "an independent verifier. Your own claim that something works does not "
        "close a criterion.\n"
    ]
    for crit in criteria:
        flag = "MANDATORY" if crit.mandatory else "optional"
        lines.append(f"- `{crit.id}` [{flag}] **{crit.statement}**")
        lines.append(f"  - Verified by: {crit.method.value}")
        if crit.command:
            lines.append(f"  - Command: `{crit.command}`")
        if crit.expect:
            lines.append(f"  - Passes when: {crit.expect}")
        if crit.rubric:
            lines.append(f"  - Rubric: {crit.rubric}")
    return "\n".join(lines)


def build_execution_brief(
    run: RunState,
    agent: AgentSpec,
    task: ExecutionTask,
    role: Role | None,
    peers: list[AgentSpec],
    schema: dict[str, Any],
    shared_context: str = "",
    lessons: list[Lesson] | None = None,
    supporting_findings: list[str] | None = None,
    tools: str = "",
    refs: BriefRefs | None = None,
) -> str:
    """Brief for an approved execution task."""
    refs = refs or BriefRefs()
    parts = [
        f"# Execution brief: {task.title}\n\n"
        f"You are agent `{agent.id}`, assigned to task `{task.id}` in a supervised "
        f"run. The user approved this task; implement it as approved.",
        _section("Original request", run.prompt),
        _section("Shared context", shared_context),
        _section("What to do", task.action),
        _section("Why it matters", task.motivation),
        _section("Findings behind this task", _bullets(supporting_findings or [])),
        _section("Your speciality", (role.charter if role else agent.brief)),
        _section("Definition of done", _dod_block(task.dod)),
        _section("Baseline", _baseline_block(run, refs.rules)),
        _section("Scope", _scope_block(agent, role, refs.rules)),
        _section("Tools", tools),
        _section("Other agents", _peers_block(peers, agent.id)),
        _section("Lessons from previous runs", _lessons_block(lessons or [], run.workspace)),
        _section(
            "Rules",
            _bullets(
                [
                    "Implement exactly the approved action. If the right change turns "
                    "out to be different, stop and report it -- do not substitute your "
                    "own plan.",
                    *CORE_RULES[1:],
                ]
            ),
        ),
        _section("Budget", _budget_block(agent)),
        _section("Output contract", render_contract(schema, refs.contract, refs.result)),
    ]
    return "\n".join(p for p in parts if p).strip()


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


def build_verification_brief(
    run: RunState,
    agent: AgentSpec,
    task: ExecutionTask,
    schema: dict[str, Any],
    change_summary: str = "",
    tools: str = "",
    refs: BriefRefs | None = None,
) -> str:
    """Brief for proving (or disproving) a task's definition of done."""
    refs = refs or BriefRefs()
    parts = [
        f"# Verification brief: {task.title}\n\n"
        f"You are agent `{agent.id}`. An implementer reports this task complete. "
        f"Establish independently whether that is true.",
        _section("Original request", run.prompt),
        _section("What was supposed to happen", f"{task.action}\n\n{task.motivation}"),
        _section("What the implementer reports", change_summary),
        _section("Criteria to verify", _dod_block(task.dod)),
        _section("Baseline", _baseline_block(run, refs.rules)),
        _section(
            "How to verify",
            _bullets(
                [
                    "Run the stated command yourself and report its real output. Do not "
                    "predict what it would print.",
                    "Count what a test command actually selected. A filter that matched "
                    "no test still exits 0 -- 'no tests ran', '[no tests to run]', "
                    "'0 passed' -- and a criterion that ran nothing is `fail`, not a "
                    "pass, whatever the exit code said.",
                    "For inspection criteria, read the file and quote the lines that "
                    "settle the question.",
                    "For review criteria, judge against the rubric and cite the specific "
                    "code that meets or fails it.",
                    "Mark `blocked` only when the check itself cannot run, and say why.",
                    "A criterion that is partly met is `fail`. There is no partial credit.",
                    "Finding a genuine failure is a successful verification. Do not "
                    "soften a verdict to be agreeable.",
                ]
            ),
        ),
        _section(
            "Scope",
            "Verify only the criteria listed. Do not fix anything you find; report "
            "it. Report regressions separately.\n\n"
            + _working_tree_line(refs.rules),
        ),
        _section("Tools", tools),
        _section("Output contract", render_contract(schema, refs.contract, refs.result)),
    ]
    return "\n".join(p for p in parts if p).strip()


# --------------------------------------------------------------------------
# Continuation after a directive
# --------------------------------------------------------------------------


def render_inbox(messages: list[Message]) -> str:
    if not messages:
        return ""
    lines = []
    for msg in messages:
        head = f"- **from `{msg.sender}`** ({msg.kind.value})"
        if msg.subject:
            head += f" -- {msg.subject}"
        lines.append(head)
        lines.append(f"  {msg.content}")
        if msg.supervisor_note:
            lines.append(f"  - Supervisor: {msg.supervisor_note}")
    return "\n".join(lines)


def render_directive(directive: Directive, agent: AgentSpec) -> str:
    """The continuation prompt an agent receives for its next turn."""
    headline = {
        "continue": "Continue. You are on brief.",
        "refocus": "Stop what you are doing and return to your objectives.",
        "narrow": "You have gone outside your scope. Cut back to what was assigned.",
        "deepen": "Your last turn was too shallow for the objectives you were given.",
        "answer": "Answer to your blocking question, from the supervisor.",
        "escalate": "This needs input beyond your remit.",
        "accept": "Your work is accepted. Finish and report `status: done`.",
        "reject": "Your last turn is rejected. Redo it against the corrections below.",
        "stop": "Stop now and report what you have.",
    }.get(directive.kind.value, "Continue.")

    parts = [
        f"# Supervisor directive: {directive.kind.value}\n\n{headline}",
        _section("Why", directive.rationale),
        _section("Corrections", _bullets(directive.corrections)),
        _section("Focus on", _bullets(directive.focus)),
        _section("Do not", _bullets(directive.forbidden)),
        _section("Messages for you", render_inbox(directive.inbox)),
        _section(
            "Remaining budget",
            f"{directive.turns_remaining} turn(s) left. Report `status: done` as soon "
            f"as your objectives are addressed."
            if directive.turns_remaining
            else "This is your final turn. Report what you have.",
        ),
        _section(
            "Reminder",
            f"Your objectives were:\n{_numbered(agent.objectives)}"
            if agent.objectives else "",
        ),
        "Reply with the same JSON contract as before.",
    ]
    return "\n".join(p for p in parts if p).strip()
