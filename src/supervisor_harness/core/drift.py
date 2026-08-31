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
    AgentSpec,
    AgentStatus,
    AgentTurn,
    Directive,
    DirectiveKind,
    DriftAssessment,
    DriftSignal,
    Severity,
    Usage,
)
from .paths import matches_any, scope_relative

_TOKEN = re.compile(r"[a-z0-9_]{3,}")

_STOPWORDS = frozenset(
    """the and for that this with from into your you are was were will would should
    have has had not but they them their there then than when where which what who
    how why all any can could may might must our out its it's about above after
    again against because been before being below between both during each few
    more most other over same some such only own too very just also use used
    using need needs make makes made get gets got does did done"""
    .split()
)


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
    forbidden = [
        f for f in touched
        if matches_any(f, scope.forbidden_paths)
    ]
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
    return DriftSignal(
        kind="scope_paths",
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
    """The agent is circling: this turn says what the last one said."""
    if not ctx.previous_turns:
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
    """Output is mostly the brief handed back, with nothing new in it."""
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


def _check_no_progress(ctx: TurnContext) -> DriftSignal | None:
    """A turn that produced nothing: no findings, no files, no verdict."""
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
    if "topic_divergence" in kinds:
        out.append("You are answering a different question. Re-read the task and your objectives.")
    return out


def decide_directive(
    assessment: DriftAssessment,
    agent: AgentSpec,
    turn: AgentTurn,
    policy: Policy,
    turns_used: int,
    inbox: list | None = None,
    prior_corrections: int = 0,
    usage: Usage | None = None,
) -> Directive:
    """Choose what to tell the agent next.

    Precedence: hard stops beat corrections, corrections beat acceptance, and an
    agent claiming completion is accepted only if it is not simultaneously adrift.

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
        scope_signals = {"scope_paths", "forbidden_paths", "out_of_scope_topic"}
        kind = (
            DirectiveKind.NARROW
            if any(s.kind in scope_signals for s in assessment.signals)
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

    return Directive(
        agent_id=agent.id,
        kind=DirectiveKind.CONTINUE,
        rationale=assessment.summary or "on brief",
        focus=agent.objectives if remaining <= 1 else [],
        inbox=inbox,
        turns_remaining=remaining,
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
    "TurnContext",
    "assess_heuristically",
    "decide_directive",
    "merge_assessments",
    "should_escalate",
    "status_after",
    "tokens",
]
