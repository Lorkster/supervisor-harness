"""Drift detection and course correction.

Two layers, deliberately in this order:

1. **Heuristics** -- deterministic, free, and run after every turn. They catch
   the drift patterns that actually occur: touching files outside scope,
   restating the brief instead of answering it, looping on the same output, and
   leaving objectives unaddressed while the budget burns.
2. **A model check** -- only when the heuristics fire, or periodically for
   expensive agents. Routed to the ``drift`` stage, which is normally a small
   local model, so watching is cheap enough to do constantly.

Escalating only on suspicion is what makes continuous supervision affordable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import Policy
from ..models import (
    ACTIVE_AGENT_STATUSES,
    AgentKind,
    AgentSpec,
    AgentStatus,
    AgentTurn,
    Directive,
    DirectiveKind,
    DriftAssessment,
    DriftSignal,
    Message,
    Severity,
    Usage,
)
from .paths import matches_any, scope_relative

_TOKEN = re.compile(r"[a-z0-9_]{3,}")

# Written as prose because that is how it is read and edited. Ruff's SIM905
# prefers a list literal, and applying that fix produced a single 900-character
# line -- the rule is right in general and wrong for a block this size, so the
# text is named first and split once, which satisfies both.
_STOPWORD_TEXT = """the and for that this with from into your you are was were will would should
    have has had not but they them their there then than when where which what who
    how why all any can could may might must our out its it's about above after
    again against because been before being below between both during each few
    more most other over same some such only own too very just also use used
    using need needs make makes made get gets got does did done"""

_STOPWORDS = frozenset(_STOPWORD_TEXT.split())


def tokens(text: str) -> set[str]:
    """Content words only; stopwords carry no signal about what an agent did."""
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS}


def containment(inner: set[str], outer: set[str]) -> float:
    """Fraction of ``inner`` present in ``outer`` (asymmetric on purpose)."""
    return len(inner & outer) / len(inner) if inner else 0.0


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


#: How a coverage directive's rationale begins. The supervisor sends an agent
#: back for coverage once; this is how it recognises that it already has.
COVERAGE_RATIONALE = "claimed done having read"

#: How many unread files a coverage directive names before it summarises.
COVERAGE_LIST_LIMIT = 15


@dataclass
class ScopeCoverage:
    """Which of the files in an agent's scope it has read so far."""

    in_scope: list[str]
    read: list[str]          # the in-scope files read, a subset of ``in_scope``

    @property
    def fraction(self) -> float:
        return len(self.read) / len(self.in_scope) if self.in_scope else 1.0

    @property
    def unread(self) -> list[str]:
        seen = set(self.read)
        return [f for f in self.in_scope if f not in seen]

    def describe(self) -> str:
        return f"{len(self.read)} of {len(self.in_scope)} files in its scope"


def scope_coverage(in_scope: list[str], read: set[str]) -> ScopeCoverage:
    """Count ``read`` (workspace-relative paths) against the files in scope."""
    return ScopeCoverage(in_scope=list(in_scope), read=[f for f in in_scope if f in read])


@dataclass
class TurnContext:
    """Everything the heuristics need about an agent's history."""

    agent: AgentSpec
    turn: AgentTurn
    previous_turns: list[AgentTurn]
    brief: str
    task_prompt: str
    turn_index: int
    workspace: str = ""


# --------------------------------------------------------------------------
# Heuristics
# --------------------------------------------------------------------------


def _check_scope_paths(ctx: TurnContext) -> DriftSignal | None:
    """Files touched outside the declared scope, or inside a forbidden path."""
    # ``scope_relative`` returns None for a rooted path this workspace cannot
    # place. Judging such a path against workspace-relative globs would call it
    # a violation every time, so it is dropped rather than counted: the
    # supervisor's workspace need not be the one the agent's tools reported
    # against, and a mismatch is not evidence about the agent.
    touched = [
        p for p in (scope_relative(f, ctx.workspace) for f in (ctx.turn.files_touched or [])) if p
    ]
    if not touched:
        return None

    scope = ctx.agent.scope
    # A forbidden path is one no agent may *modify*, and only an execution agent
    # can. For any other kind, what arrives here is what it read: a host agent
    # reports the files it examined in the same field. Counted as a violation,
    # it was "uncorrectable" and stopped the agent outright -- four of five
    # analysis lenses in one real run, on their first turn, for reading the
    # very documents the task told them to read, which the plan had marked as
    # not to be modified.
    forbidden = [
        f for f in touched
        if matches_any(f, scope.forbidden_paths)
    ] if ctx.agent.kind is AgentKind.EXECUTION else []
    if forbidden:
        return DriftSignal(
            kind="forbidden_paths",
            severity=Severity.CRITICAL,
            detail=f"touched forbidden path(s): {', '.join(sorted(set(forbidden))[:5])}",
            score=1.0,
        )

    if not scope.paths:
        return None
    outside = [f for f in touched if not matches_any(f, scope.paths)]
    if not outside:
        return None
    ratio = len(outside) / len(touched)
    # The same for the scope itself. Outside it, an execution agent has written
    # where it may not: a violation no second opinion can talk down. Any other
    # agent has read there -- which nothing fences, and which may be exactly
    # what its task said to do. A real run stopped a UX lens for reading the
    # two documents the request named, with the drift model saying it was
    # "correctly performing the required analysis": the scope signal is a floor
    # the model cannot lower. Read outside its scope, it is a signal like any
    # other, and the model's judgement counts.
    wrote = ctx.agent.kind is AgentKind.EXECUTION
    return DriftSignal(
        kind="scope_paths" if wrote else "reads_outside_scope",
        severity=Severity.HIGH if ratio > 0.5 else Severity.MEDIUM,
        detail=(
            f"{len(outside)} of {len(touched)} files are outside the declared scope: "
            f"{', '.join(sorted(set(outside))[:5])}"
        ),
        score=min(1.0, 0.35 + 0.5 * ratio),
    )


def _check_out_of_scope_topics(ctx: TurnContext) -> DriftSignal | None:
    """The agent is working on something its brief explicitly excluded."""
    excluded = [t for t in ctx.agent.scope.out_of_scope if t.strip()]
    if not excluded:
        return None
    body = ctx.turn.output.lower()
    if not body:
        return None
    hits = [t for t in excluded if t.lower() in body]
    if not hits:
        return None
    # A mention is weak evidence; the same topic dominating the output is not.
    weight = sum(body.count(t.lower()) for t in hits)
    density = weight / max(1, len(body.split()) / 100)
    return DriftSignal(
        kind="out_of_scope_topic",
        severity=Severity.MEDIUM if density < 2 else Severity.HIGH,
        detail=f"output dwells on excluded topic(s): {', '.join(hits[:3])}",
        score=min(0.65, 0.2 + 0.15 * density),
    )


def _check_objective_coverage(ctx: TurnContext) -> DriftSignal | None:
    """Objectives still unaddressed while the budget runs down."""
    objectives = [o for o in ctx.agent.objectives if o.strip()]
    if not objectives:
        return None
    body = tokens(f"{ctx.turn.output} {ctx.turn.self_assessment} " + " ".join(
        f.title + " " + f.detail for f in ctx.turn.findings
    ))
    if not body:
        return None

    covered = sum(1 for obj in objectives if containment(tokens(obj), body) >= 0.5)
    coverage = covered / len(objectives)
    budget_used = (ctx.turn_index + 1) / max(1, ctx.agent.budget.max_turns)

    # Only a concern once the agent has had a fair share of its budget.
    if budget_used < 0.5 or coverage >= 0.6:
        return None
    return DriftSignal(
        kind="objective_coverage",
        severity=Severity.MEDIUM if coverage > 0.3 else Severity.HIGH,
        detail=(
            f"{covered}/{len(objectives)} objectives addressed with "
            f"{int(budget_used * 100)}% of the turn budget used"
        ),
        score=min(0.85, (0.6 - coverage) + 0.3 * budget_used),
    )


def _check_repetition(ctx: TurnContext) -> DriftSignal | None:
    """The agent is circling: this turn says what the last one said.

    Not when it says so as "done". Saying the same thing again and calling it
    finished is standing by an answer, and it is what this signal's own
    correction asks for ("or report `status: done`"). Counted as circling, an
    agent sent back once -- to deepen, or to read more of its scope -- that
    stood by its answer was refocused for it on every turn and could never be
    accepted.
    """
    if not ctx.previous_turns or ctx.turn.claimed_status is AgentStatus.DONE:
        return None
    current = tokens(ctx.turn.output)
    if len(current) < 15:
        return None
    previous = tokens(ctx.previous_turns[-1].output)
    similarity = jaccard(current, previous)
    if similarity < 0.72:
        return None
    return DriftSignal(
        kind="repetition",
        severity=Severity.MEDIUM,
        detail=f"this turn is {int(similarity * 100)}% the same as the previous one",
        score=min(0.75, similarity),
    )


def _check_brief_echo(ctx: TurnContext) -> DriftSignal | None:
    """Output is mostly the brief handed back, with nothing new in it.

    Not for an implementer reporting work: its work is the change, and whether
    the change is done is settled by its definition of done, not by how novel
    its account sounds. A short, accurate report of a one-line change uses the
    brief's words because the brief names the change -- measured in a go-live
    run, where it read as "only 10% new information", earned the implementer a
    "deepen", and helped stop it with its task already done.
    """
    if ctx.agent.kind is AgentKind.EXECUTION and (
        ctx.turn.files_touched or ctx.turn.claimed_status is AgentStatus.DONE
    ):
        return None
    body = tokens(ctx.turn.output)
    if len(body) < 12:
        return None
    brief_terms = tokens(ctx.brief)
    if not brief_terms:
        return None
    echoed = containment(body, brief_terms)
    novel = len(body - brief_terms) / len(body)
    if echoed < 0.8 or novel > 0.25:
        return None
    return DriftSignal(
        kind="brief_echo",
        severity=Severity.HIGH,
        detail=f"only {int(novel * 100)}% of the output is new information",
        score=0.7,
    )


#: The signals that are about *where* an agent worked rather than how well.
#: A second opinion may lower a drift score, but never below what these alone
#: say: the heuristics cannot be talked out of a scope violation.
SCOPE_SIGNALS = frozenset({"scope_paths", "forbidden_paths", "out_of_scope_topic"})

#: The signals a "narrow" answers: the scope signals, and a lens reading
#: outside its scope -- which is about where it worked too, but is not a
#: violation, so it sets the directive without setting a floor.
NARROWING_SIGNALS = SCOPE_SIGNALS | {"reads_outside_scope"}


def _check_no_progress(ctx: TurnContext) -> DriftSignal | None:
    """A turn that produced nothing: no findings, no files, no verdict.

    A turn spent reading the workspace is not nothing. It used to score the
    same as an idle one, so a lens working through a repository before
    reporting was refocused on every turn -- told to "read a specific file",
    which is what it had just done -- and in one observed run stopped with
    most of its budget unspent. That case is its own, weaker signal: enough to
    corroborate real drift and to ask for a report as the budget runs out, not
    enough on its own to correct an agent that is working.
    """
    produced = (
        len(ctx.turn.findings)
        + len(ctx.turn.files_touched)
        + len(ctx.turn.artifacts)
        + (1 if ctx.turn.claimed_status is AgentStatus.DONE else 0)
    )
    if produced or ctx.turn_index == 0:
        return None
    if ctx.turn.claimed_status is AgentStatus.BLOCKED and ctx.turn.blocked_on:
        return None  # Blocked with a stated reason is a legitimate outcome.
    if ctx.turn.usage.tool_calls:
        return DriftSignal(
            kind="unreported_exploration",
            severity=Severity.LOW,
            detail=(f"read the workspace ({ctx.turn.usage.tool_calls} tool call(s)) "
                    "but reported nothing from it"),
            score=0.3,
        )
    return DriftSignal(
        kind="no_progress",
        severity=Severity.MEDIUM,
        detail="turn produced no findings, no file changes and no completion claim",
        score=0.5,
    )


def _check_topic_divergence(ctx: TurnContext) -> DriftSignal | None:
    """The output has drifted away from the task's own vocabulary.

    Measured in both directions and the better one taken. A deep dive into one
    objective legitimately uses few of the task's words, but its own words are
    still drawn from the task's subject matter; genuinely divergent work fails
    both directions at once.
    """
    body = tokens(ctx.turn.output)
    if len(body) < 25:
        return None
    anchor = tokens(ctx.task_prompt) | tokens(" ".join(ctx.agent.objectives))
    if len(anchor) < 5:
        return None
    overlap = max(containment(anchor, body), containment(body, anchor))
    if overlap >= 0.12:
        return None
    return DriftSignal(
        kind="topic_divergence",
        severity=Severity.HIGH,
        detail=f"only {int(overlap * 100)}% of the task's own terms appear in the output",
        score=0.65,
    )


HEURISTICS = (
    _check_scope_paths,
    _check_out_of_scope_topics,
    _check_objective_coverage,
    _check_repetition,
    _check_brief_echo,
    _check_no_progress,
    _check_topic_divergence,
)


def assess_heuristically(ctx: TurnContext) -> DriftAssessment:
    """Run every heuristic and combine the signals into one score.

    The strongest signal sets the level and the others corroborate it at a
    quarter weight. A plain probabilistic OR was tried first and rejected: it
    let two moderate signals (a quiet turn plus thin coverage) add up to a
    hard stop, which is not a proportionate response to one weak turn.
    """
    signals = [signal for check in HEURISTICS if (signal := check(ctx)) is not None]

    if signals:
        scores = sorted((s.score for s in signals), reverse=True)
        score = round(min(1.0, scores[0] + 0.25 * sum(scores[1:])), 3)
    else:
        score = 0.0

    return DriftAssessment(
        on_task=score < 0.45,
        score=score,
        signals=signals,
        summary=(
            "; ".join(s.detail for s in signals) if signals else "no drift signals"
        ),
        checked_by="heuristics",
    )


def merge_assessments(heuristic: DriftAssessment, model: DriftAssessment) -> DriftAssessment:
    """Combine the two layers, weighting the model's judgement slightly higher.

    The heuristics cannot be talked out of a scope violation, and the model
    catches drift that is semantically obvious but lexically invisible, so
    neither is allowed to fully override the other.
    """
    score = round(0.4 * heuristic.score + 0.6 * model.score, 3)
    if any(s.kind in SCOPE_SIGNALS for s in heuristic.signals):
        # What the docstring above always claimed and the arithmetic did not
        # enforce: a scope violation at 0.85 and a model saying 0.0 averaged
        # to 0.34, below the threshold -- accepted.
        score = max(score, heuristic.score)
    return DriftAssessment(
        on_task=score < 0.45 and model.on_task,
        score=score,
        signals=[*heuristic.signals, *model.signals],
        summary=model.summary or heuristic.summary,
        checked_by="heuristics+model",
    )


def should_escalate(assessment: DriftAssessment, policy: Policy, turn_index: int) -> bool:
    """Whether to spend a model call confirming what the heuristics suspect."""
    if not policy.model_drift_check:
        return False
    if assessment.score >= policy.drift_threshold * 0.7:
        return True
    # Periodic spot-check, so quiet drift does not accumulate unseen.
    return policy.drift_check_every > 0 and (turn_index + 1) % (policy.drift_check_every * 3) == 0


# --------------------------------------------------------------------------
# Course correction
# --------------------------------------------------------------------------


def _corrections_from(signals: list[DriftSignal], agent: AgentSpec) -> list[str]:
    """Turn signals into instructions an agent can actually act on."""
    out: list[str] = []
    kinds = {s.kind for s in signals}

    if "forbidden_paths" in kinds:
        out.append(
            "Revert any change you made to a forbidden path. Those files are owned by "
            "another agent or are off-limits for this run."
        )
    if "scope_paths" in kinds:
        allowed = ", ".join(f"`{p}`" for p in agent.scope.paths) or "your assigned files"
        out.append(f"Work only within {allowed}. Report anything outside it as a message instead.")
    if "reads_outside_scope" in kinds:
        allowed = ", ".join(f"`{p}`" for p in agent.scope.paths) or "your assigned files"
        out.append(f"Your scope is {allowed}. Read outside it only what your objectives "
                   "need, and say what each such file told you.")
    if "out_of_scope_topic" in kinds:
        excluded = ", ".join(agent.scope.out_of_scope[:3])
        out.append(f"Drop the excluded topics ({excluded}) entirely and return to your objectives.")
    if "objective_coverage" in kinds:
        out.append(
            "Address the objectives you have not covered yet, in order, one paragraph "
            "each with concrete evidence."
        )
    if "repetition" in kinds:
        out.append(
            "Your last turn repeated the previous one. Either produce new evidence or "
            "report `status: done` with what you have."
        )
    if "brief_echo" in kinds:
        out.append(
            "Stop restating the brief. Report what you found by examining the actual "
            "code, with file:line evidence."
        )
    if "no_progress" in kinds:
        out.append(
            "That turn produced nothing. Take one concrete action -- read a specific "
            "file, run a specific command -- and report its result."
        )
    if "unreported_exploration" in kinds:
        out.append(
            "You have read the workspace but reported nothing from it. Report what you "
            "found as findings with file:line evidence. If you found nothing, say so and "
            "report `status: done`."
        )
    if "topic_divergence" in kinds:
        out.append("You are answering a different question. Re-read the task and your objectives.")
    return out


def decide_directive(
    assessment: DriftAssessment,
    agent: AgentSpec,
    turn: AgentTurn,
    policy: Policy,
    turns_used: int,
    inbox: list[Message] | None = None,
    prior_corrections: int = 0,
    usage: Usage | None = None,
    coverage: ScopeCoverage | None = None,
) -> Directive:
    """Choose what to tell the agent next.

    Precedence: hard stops beat corrections, corrections beat acceptance, and an
    agent claiming completion is accepted only if it is not simultaneously adrift.

    ``coverage`` is passed only when the caller wants it judged: an analysis
    agent, with the check on, not already sent back for it. A "done" with turns
    left and less than ``policy.min_scope_coverage`` of the scope read is sent
    back to read the rest.

    Stopping is reserved for cases where correction has already been tried and
    failed, or where the violation is not correctable after the fact (a
    forbidden path has already been written to). Killing an agent for one bad
    turn throws away the work it has done and the context it has built.
    """
    remaining = max(0, agent.budget.max_turns - turns_used)
    inbox = inbox or []

    # All four ceilings, not just the turn count. The other three defaulted to
    # zero here, so `Budget.max_tokens`, `max_seconds` and `max_tool_calls` were
    # declared, documented and unenforceable.
    spent = usage or Usage()
    exhausted = agent.budget.exhausted(
        turns_used, spent.total_tokens, spent.seconds, spent.tool_calls
    )
    if exhausted:
        return Directive(
            agent_id=agent.id,
            kind=DirectiveKind.STOP,
            rationale=exhausted,
            corrections=["Report your findings so far, including what remains unresolved."],
            inbox=inbox,
            turns_remaining=0,
        )

    uncorrectable = any(s.kind == "forbidden_paths" for s in assessment.signals)
    recidivist = prior_corrections >= 1 and assessment.score >= policy.drift_hard_threshold
    if uncorrectable or recidivist:
        return Directive(
            agent_id=agent.id,
            kind=DirectiveKind.STOP,
            rationale=(
                f"drift score {assessment.score} after {prior_corrections} correction(s); "
                f"{assessment.summary}"
                if recidivist
                else f"uncorrectable scope violation; {assessment.summary}"
            ),
            corrections=_corrections_from(assessment.signals, agent),
            inbox=inbox,
            turns_remaining=0,
        )

    if assessment.score >= policy.drift_threshold:
        kind = (
            DirectiveKind.NARROW
            if any(s.kind in NARROWING_SIGNALS for s in assessment.signals)
            else DirectiveKind.REFOCUS
        )
        return Directive(
            agent_id=agent.id,
            kind=kind,
            rationale=assessment.summary,
            corrections=_corrections_from(assessment.signals, agent),
            focus=agent.objectives,
            forbidden=agent.scope.out_of_scope,
            inbox=inbox,
            turns_remaining=remaining,
        )

    if turn.claimed_status is AgentStatus.DONE:
        shallow = any(s.kind in ("brief_echo", "objective_coverage") for s in assessment.signals)
        if shallow:
            return Directive(
                agent_id=agent.id,
                kind=DirectiveKind.DEEPEN,
                rationale="claimed done, but objectives are not adequately covered",
                corrections=_corrections_from(assessment.signals, agent),
                focus=agent.objectives,
                inbox=inbox,
                turns_remaining=remaining,
            )
        if (
            coverage is not None
            and remaining > 0
            and coverage.fraction < policy.min_scope_coverage
        ):
            return Directive(
                agent_id=agent.id,
                kind=DirectiveKind.DEEPEN,
                rationale=f"{COVERAGE_RATIONALE} {coverage.describe()}",
                corrections=[_coverage_correction(coverage)],
                focus=agent.objectives,
                inbox=inbox,
                turns_remaining=remaining,
            )
        return Directive(
            agent_id=agent.id,
            kind=DirectiveKind.ACCEPT,
            rationale="objectives addressed and no drift detected",
            inbox=inbox,
            turns_remaining=remaining,
        )

    if turn.claimed_status is AgentStatus.BLOCKED:
        return Directive(
            agent_id=agent.id,
            kind=DirectiveKind.ESCALATE,
            rationale=turn.blocked_on or "agent reported itself blocked",
            inbox=inbox,
            turns_remaining=remaining,
        )

    # Still exploring and nothing reported, with the budget nearly gone: say
    # so while there is a turn left to answer in. Budget exhaustion stops an
    # agent *after* its last turn, so an agent that only ever read would
    # otherwise end with nothing to show for it.
    exploring = any(s.kind == "unreported_exploration" for s in assessment.signals)
    return Directive(
        agent_id=agent.id,
        kind=DirectiveKind.CONTINUE,
        rationale=assessment.summary or "on brief",
        corrections=(_corrections_from(assessment.signals, agent)
                     if exploring and remaining <= 2 else []),
        focus=agent.objectives if remaining <= 1 else [],
        inbox=inbox,
        turns_remaining=remaining,
    )


def _coverage_correction(coverage: ScopeCoverage) -> str:
    """What an agent sent back for coverage is told, naming what it has not read."""
    unread = coverage.unread
    named = ", ".join(f"`{f}`" for f in unread[:COVERAGE_LIST_LIMIT])
    more = (f", and {len(unread) - COVERAGE_LIST_LIMIT} more"
            if len(unread) > COVERAGE_LIST_LIMIT else "")
    return (
        f"You have read {coverage.describe()}. Not yet read: {named}{more}. "
        "Read the ones that bear on your objectives and report what they change. "
        "For any you leave unread, say in self_assessment why it does not need "
        "reading. List every file you read in files_examined."
    )


def status_after(directive: Directive) -> AgentStatus:
    """The agent status implied by a directive."""
    return {
        DirectiveKind.ACCEPT: AgentStatus.DONE,
        DirectiveKind.STOP: AgentStatus.STOPPED,
        DirectiveKind.ESCALATE: AgentStatus.BLOCKED,
    }.get(directive.kind, AgentStatus.RUNNING)


__all__ = [
    "ACTIVE_AGENT_STATUSES",
    "COVERAGE_RATIONALE",
    "SCOPE_SIGNALS",
    "ScopeCoverage",
    "TurnContext",
    "assess_heuristically",
    "decide_directive",
    "merge_assessments",
    "scope_coverage",
    "should_escalate",
    "status_after",
    "tokens",
]
