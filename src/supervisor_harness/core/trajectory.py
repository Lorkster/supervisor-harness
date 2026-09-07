"""A run as a trajectory: the supervisor's steps, and each agent's beneath it.

A third projection over the event log, beside `core/journal.py` and
`core/timing.py`. No new events, no model call, nothing written at run time --
the log already holds all of it, and this is a reader.

What it is *for* is different from the other two. The journal answers "why was
this directive issued to this agent" for a person. This produces a portable
document: something an evaluation harness, a dashboard or a fine-tuning pipeline
can consume without knowing anything about this codebase.

## Where the shape comes from

Adapted from NVIDIA's ATIF trajectory format, whose nesting is the same shape a
supervised run already has -- a root trajectory for the supervisor, and an
embedded child per agent, each resolvable by an id from inside the document.

Two of its conventions are taken as invariants here rather than as a schema,
because they are the two that make a trajectory honest:

**A deterministic step says so, and carries no model metrics.** ATIF spells this
``llm_call_count=0`` and forbids ``metrics`` on such a step. It matters more here
than it does there: the central economic claim of this harness is that watching
for drift is cheap because the watching is mostly heuristic, and until now a
reader of the log could not cheaply separate what the harness decided for free
from what a model was asked. :func:`validate` enforces it.

**A step that repeats earlier work says which step it repeats.** ATIF flags a
step retained across a context-compaction boundary so that consumers filter it.
The harness does not compact context, but it does re-issue: a packet handed out
again to an agent that has not answered carries the same brief, and a consumer
counting work would count it twice. ``repeats`` names the earlier step.

What is deliberately *not* taken is ATIF itself. Pinning to another project's
evolving version buys compatibility with consumers this harness does not have,
and the names here are this harness's own. If a consumer ever appears, an
adapter over this is a small thing to write; a wrong abstraction inherited early
is not.

## What is a step, and what is not

Steps are the things the supervisor *did* or was *told*: a brief rendered, a
packet dispatched, a turn reported, a directive issued, drift assessed, a
criterion verified, a task decided. Findings and messages are not steps, because
they arrive inside a turn and already appear in it -- emitting them again would
double-count exactly what ``repeats`` exists to prevent.

## The one place this is approximate

A recorded checkpoint is a merge of deterministic scoring with a model's
judgement, and the event does not say whether a model actually contributed --
so checkpoint and lesson steps are marked ``mixed`` rather than resolved.
Making that exact needs a field on ``Checkpoint``, which is a change to what the
run records; this module is a reader and does not get to make it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import RunState
from ..store.events import Event, EventType
from .timing import measure

#: The document's own version. Ours, not ATIF's: see the module docstring.
SCHEMA = "supervisor-trajectory/1"

#: Who settled a step.
POLICY = "policy"    #: deterministic -- the harness decided it, at no model cost
MODEL = "model"      #: a model produced it
MIXED = "mixed"      #: a deterministic result a model was allowed to adjust
PERSON = "person"    #: the user decided it

#: Steps whose ``decided_by`` is :data:`POLICY` must carry no metrics and no
#: reasoning. Stated as data so :func:`validate` and the builder cannot drift
#: apart on what the rule is.
METRIC_BEARING = frozenset({MODEL, MIXED})


@dataclass
class Step:
    """One thing that happened, in the trajectory it happened in."""

    index: int = 0
    ts: str = ""
    source: str = "supervisor"   # system | supervisor | agent | person
    kind: str = ""
    decided_by: str = POLICY
    content: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    #: Present only on a step a model produced. Absent -- not zeroed -- on a
    #: deterministic one, so "no model was asked" and "a model was asked and
    #: reported nothing" stay different facts.
    metrics: dict[str, Any] | None = None
    #: The index of the earlier step in this same trajectory whose work this
    #: repeats. A consumer that must not count work twice filters on it.
    repeats: int | None = None


@dataclass
class AgentTrajectory:
    """One agent's steps, embedded in the run's."""

    agent_id: str = ""
    role: str = ""
    kind: str = ""
    title: str = ""
    model: str = ""
    host_agent_type: str = ""
    status: str = ""
    steps: list[Step] = field(default_factory=list)


@dataclass
class Trajectory:
    """A whole run, portable."""

    schema: str = SCHEMA
    #: Who produced this document. The version is stamped by the caller rather
    #: than read here: it belongs to the distribution, and `core` reaching up to
    #: the package root for it makes `core` and the root mutually dependent --
    #: which `tests/test_architecture.py` catches, and did.
    harness: dict[str, str] = field(default_factory=lambda: {"name": "supervisor-harness"})
    run_id: str = ""
    prompt: str = ""
    mode: str = ""
    backend: str = ""
    workspace: str = ""
    created_at: str = ""
    ended_at: str = ""
    outcome: dict[str, Any] = field(default_factory=dict)
    totals: dict[str, Any] = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)
    agents: list[AgentTrajectory] = field(default_factory=list)


def _metrics(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """A turn's metrics, or ``None`` when nothing was measured.

    An all-zero usage becomes ``None`` rather than a dict of zeroes. In
    host-delegated mode the harness never sees a token, and a metrics block full
    of zeroes reads as "the model used nothing" rather than "nobody was
    counting" -- which is the same overstatement this batch declined to make by
    adding cost fields that only one backend can populate.
    """
    if not isinstance(usage, dict):
        return None
    kept = {k: v for k, v in usage.items() if v}
    return kept or None


class _Builder:
    """Assembles a trajectory from a log, one event at a time.

    A class for the reason `_JournalBuilder` is one: the handlers share state --
    the per-agent step lists, the open dispatch per agent, the step counters --
    and as free functions they would each take all of it.
    """

    def __init__(self, state: RunState) -> None:
        self.state = state
        self.trajectory = Trajectory(
            run_id=state.id,
            prompt=state.prompt,
            mode=str(state.mode),
            backend=str(state.backend),
            workspace=state.workspace,
            created_at=state.created_at,
        )
        self.agents: dict[str, AgentTrajectory] = {}
        # The index of an agent's last unanswered dispatch step, so a re-issue
        # can name the step it repeats.
        self.open_dispatch: dict[str, int] = {}

    # -- placement ---------------------------------------------------------

    def agent(self, agent_id: str) -> AgentTrajectory | None:
        """The child trajectory for this agent, created on first sight."""
        if not agent_id:
            return None
        existing = self.agents.get(agent_id)
        if existing is not None:
            return existing
        spec = self.state.agents.get(agent_id)
        child = AgentTrajectory(
            agent_id=agent_id,
            role=spec.role if spec else "",
            kind=str(spec.kind) if spec else "",
            title=spec.title if spec else "",
            model=spec.binding.ref() if spec else "",
            host_agent_type=spec.host_agent_type or "" if spec else "",
            status=str(spec.status) if spec else "",
        )
        self.agents[agent_id] = child
        self.trajectory.agents.append(child)
        return child

    def add(
        self,
        event: Event,
        kind: str,
        *,
        agent_id: str = "",
        source: str = "supervisor",
        decided_by: str = POLICY,
        content: str = "",
        detail: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
        repeats: int | None = None,
    ) -> Step:
        """Append a step to the run's trajectory, or to an agent's."""
        target = self.agent(agent_id) if agent_id else None
        steps = target.steps if target is not None else self.trajectory.steps
        step = Step(
            index=len(steps) + 1,
            ts=event.ts,
            source=source,
            kind=kind,
            decided_by=decided_by,
            content=content,
            detail=detail or {},
            # The invariant, enforced where steps are made rather than only
            # where they are checked: a deterministic step cannot carry metrics
            # even if a caller passes some.
            metrics=metrics if decided_by in METRIC_BEARING else None,
            repeats=repeats,
        )
        steps.append(step)
        return step

    # -- handlers ----------------------------------------------------------

    def feed(self, event: Event) -> None:
        handler = _HANDLERS.get(event.type)
        if handler is not None:
            handler(self, event, event.payload)

    def on_run_created(self, event: Event, p: dict[str, Any]) -> None:
        self.add(event, "run_created", source="system", content=self.state.prompt)

    def on_phase_changed(self, event: Event, p: dict[str, Any]) -> None:
        self.add(event, "phase", content=str(p.get("phase", "")))

    def on_envelope_set(self, event: Event, p: dict[str, Any]) -> None:
        self.add(event, "envelope", detail={"envelope": p.get("envelope", {})})

    def on_agent_spawned(self, event: Event, p: dict[str, Any]) -> None:
        agent = p.get("agent") or {}
        agent_id = str(agent.get("id", ""))
        self.add(
            event, "spawned", agent_id=agent_id,
            content=str(agent.get("title", "")),
            detail={"role": agent.get("role", ""), "objectives": agent.get("objectives", [])},
        )

    def on_brief_rendered(self, event: Event, p: dict[str, Any]) -> None:
        self.add(
            event, "brief", agent_id=str(p.get("agent_id", "")),
            content=str(p.get("brief", "")),
        )

    def on_agent_dispatched(self, event: Event, p: dict[str, Any]) -> None:
        agent_id = str(p.get("agent_id", ""))
        # A packet handed out again to an agent that never answered repeats the
        # earlier one: same brief, same work asked for. Naming the step it
        # repeats is what lets a consumer count the work once.
        step = self.add(
            event, "dispatched", agent_id=agent_id,
            detail={"kind": p.get("kind", "")},
            repeats=self.open_dispatch.get(agent_id),
        )
        self.open_dispatch[agent_id] = step.index

    def on_turn_recorded(self, event: Event, p: dict[str, Any]) -> None:
        turn = p.get("turn") or {}
        agent_id = str(turn.get("agent_id", ""))
        self.open_dispatch.pop(agent_id, None)
        self.add(
            event, "turn", agent_id=agent_id, source="agent", decided_by=MODEL,
            content=str(turn.get("output", "")),
            detail={
                "reasoning": turn.get("reasoning", ""),
                "claimed_status": turn.get("claimed_status", ""),
                "files_touched": turn.get("files_touched", []),
                "findings": len(turn.get("findings") or []),
                "open_questions": turn.get("open_questions", []),
            },
            metrics=_metrics(turn.get("usage")),
        )

    def on_directive_issued(self, event: Event, p: dict[str, Any]) -> None:
        directive = p.get("directive") or {}
        self.add(
            event, "directive", agent_id=str(directive.get("agent_id", "")),
            content=str(directive.get("rationale", "")),
            detail={
                "kind": directive.get("kind", ""),
                "corrections": directive.get("corrections", []),
                "forbidden": directive.get("forbidden", []),
            },
        )

    def on_drift_assessed(self, event: Event, p: dict[str, Any]) -> None:
        assessment = p.get("assessment") or {}
        checked_by = str(assessment.get("checked_by", "heuristics"))
        self.add(
            event, "drift", agent_id=str(p.get("agent_id", "")),
            # The distinction this whole convention exists for: a heuristic
            # assessment cost nothing, and a run's drift bill is the count of
            # the ones that escalated, not the count of all of them.
            decided_by=MODEL if "model" in checked_by else POLICY,
            content=str(assessment.get("summary", "")),
            detail={
                "score": assessment.get("score", 0.0),
                "on_task": assessment.get("on_task", True),
                "checked_by": checked_by,
                "signals": [s.get("kind", "") for s in (assessment.get("signals") or [])],
            },
        )

    def on_assists_recorded(self, event: Event, p: dict[str, Any]) -> None:
        self.add(
            event, "harness_repaired", agent_id=str(p.get("agent_id", "")),
            content=f"{p.get('total', 0)} repair(s)",
            detail={"counts": p.get("counts", {})},
        )

    def on_task_proposed(self, event: Event, p: dict[str, Any]) -> None:
        task = p.get("task") or {}
        self.add(
            event, "task_proposed", decided_by=MODEL,
            content=str(task.get("title", "")),
            detail={"id": task.get("id", ""), "action": task.get("action", "")},
        )

    def on_task_decided(self, event: Event, p: dict[str, Any]) -> None:
        self.add(
            event, "task_decided", source="person", decided_by=PERSON,
            content=str(p.get("decision", "")),
            detail={"task_id": p.get("task_id", ""), "note": p.get("note", "")},
        )

    def on_criterion_verified(self, event: Event, p: dict[str, Any]) -> None:
        by = event.actor
        self.add(
            event, "criterion", agent_id="" if by == "harness" else by,
            source="supervisor" if by == "harness" else "agent",
            # The harness running a check itself is the strongest evidence in
            # the run and the cheapest; an agent's account of running one is
            # neither. They should not read the same in an exported record.
            decided_by=POLICY if by == "harness" else MODEL,
            content=str(p.get("status", "")),
            detail={
                "task_id": p.get("task_id", ""),
                "criterion_id": p.get("criterion_id", ""),
                "evidence": str(p.get("evidence", ""))[:2000],
                "verified_by": by,
            },
        )

    def on_checkpoint_recorded(self, event: Event, p: dict[str, Any]) -> None:
        checkpoint = p.get("checkpoint") or {}
        self.add(
            event, "checkpoint", decided_by=MIXED,
            content=str(checkpoint.get("summary", "")),
            detail={
                "iteration": checkpoint.get("iteration", 0),
                "passed": checkpoint.get("passed", False),
                "quality": checkpoint.get("quality", 0.0),
                "scope_fidelity": checkpoint.get("scope_fidelity", 0.0),
                "completeness": checkpoint.get("completeness", 0.0),
            },
        )

    def on_lesson_learned(self, event: Event, p: dict[str, Any]) -> None:
        lesson = p.get("lesson") or {}
        self.add(
            event, "lesson", decided_by=MIXED,
            content=str(lesson.get("statement", "")),
            detail={"category": lesson.get("category", ""), "target": lesson.get("target", "")},
        )

    def on_run_ended(self, event: Event, p: dict[str, Any]) -> None:
        self.trajectory.ended_at = event.ts
        self.add(event, "run_ended", source="system", content=str(p.get("phase", "")))

    def finish(self, events: list[Event]) -> Trajectory:
        state = self.state
        timing = measure(events)
        self.trajectory.ended_at = self.trajectory.ended_at or (events[-1].ts if events else "")
        self.trajectory.outcome = {
            "phase": str(state.phase),
            "findings": len(state.findings),
            "tasks": {
                "total": len(state.tasks),
                "verified": sum(1 for t in state.tasks.values() if str(t.status) == "verified"),
                "failed": sum(1 for t in state.tasks.values() if str(t.status) == "failed"),
            },
            "error": state.error,
        }
        self.trajectory.totals = {
            "events": len(events),
            "steps": len(self.trajectory.steps)
            + sum(len(a.steps) for a in self.trajectory.agents),
            "agents": len(self.trajectory.agents),
            "usage": {k: v for k, v in _as_dict(state.total_usage()).items() if v},
            "timing": timing.to_payload(),
            # Cheap to compute here and the number this format exists to make
            # legible: how much of the run a model was asked for at all.
            "model_steps": self._count(MODEL),
            "policy_steps": self._count(POLICY),
            "assists": dict(state.assists),
        }
        return self.trajectory

    def _count(self, decided_by: str) -> int:
        return sum(
            1 for step in self._all_steps() if step.decided_by == decided_by
        )

    def _all_steps(self) -> list[Step]:
        return [
            *self.trajectory.steps,
            *(step for agent in self.trajectory.agents for step in agent.steps),
        ]


def _as_dict(obj: Any) -> dict[str, Any]:
    from ..serde import to_jsonable

    value = to_jsonable(obj)
    return value if isinstance(value, dict) else {}


_HANDLERS = {
    EventType.RUN_CREATED: _Builder.on_run_created,
    EventType.PHASE_CHANGED: _Builder.on_phase_changed,
    EventType.ENVELOPE_SET: _Builder.on_envelope_set,
    EventType.AGENT_SPAWNED: _Builder.on_agent_spawned,
    EventType.BRIEF_RENDERED: _Builder.on_brief_rendered,
    EventType.AGENT_DISPATCHED: _Builder.on_agent_dispatched,
    EventType.TURN_RECORDED: _Builder.on_turn_recorded,
    EventType.DIRECTIVE_ISSUED: _Builder.on_directive_issued,
    EventType.DRIFT_ASSESSED: _Builder.on_drift_assessed,
    EventType.ASSISTS_RECORDED: _Builder.on_assists_recorded,
    EventType.TASK_PROPOSED: _Builder.on_task_proposed,
    EventType.TASK_DECIDED: _Builder.on_task_decided,
    EventType.CRITERION_VERIFIED: _Builder.on_criterion_verified,
    EventType.CHECKPOINT_RECORDED: _Builder.on_checkpoint_recorded,
    EventType.LESSON_LEARNED: _Builder.on_lesson_learned,
    EventType.RUN_ENDED: _Builder.on_run_ended,
}


def build_trajectory(
    state: RunState, events: list[Event], *, version: str = ""
) -> Trajectory:
    """Project a run's log into a portable trajectory.

    ``version`` is the harness version to stamp, supplied by whoever is
    exporting. See :class:`Trajectory.harness` for why it is not read here.
    """
    builder = _Builder(state)
    for event in events:
        builder.feed(event)
    trajectory = builder.finish(events)
    if version:
        trajectory.harness["version"] = version
    return trajectory


def validate(trajectory: Trajectory) -> list[str]:
    """Every way this document could be wrong, as a list of reasons.

    A list rather than an exception, and a function rather than a comment,
    because the two invariants this format carries are the whole point of it:
    documented and unchecked, they would be true until the day a new step kind
    forgot one. This is the equivalent of the model validators ATIF's schema
    carries, in a codebase that has no schema library and does not want one.
    """
    problems: list[str] = []

    def check_steps(label: str, steps: list[Step]) -> None:
        for position, step in enumerate(steps, start=1):
            where = f"{label} step {step.index}"
            if step.index != position:
                problems.append(f"{where}: indices must run 1..n in order, expected {position}")
            if step.decided_by not in (POLICY, MODEL, MIXED, PERSON):
                problems.append(f"{where}: unknown decided_by {step.decided_by!r}")
            if step.decided_by not in METRIC_BEARING and step.metrics is not None:
                problems.append(
                    f"{where}: decided_by={step.decided_by!r} carries metrics; a step no "
                    "model was asked for cannot have model metrics"
                )
            if step.repeats is not None and not 1 <= step.repeats < step.index:
                problems.append(
                    f"{where}: repeats={step.repeats} must name an earlier step in the "
                    "same trajectory"
                )

    if trajectory.schema != SCHEMA:
        problems.append(f"unknown schema {trajectory.schema!r}")

    check_steps("run", trajectory.steps)
    seen: set[str] = set()
    for agent in trajectory.agents:
        if not agent.agent_id:
            problems.append("an embedded agent trajectory has no agent_id")
        elif agent.agent_id in seen:
            problems.append(f"duplicate agent trajectory {agent.agent_id!r}")
        else:
            seen.add(agent.agent_id)
        check_steps(f"agent {agent.agent_id}", agent.steps)

    return problems


__all__ = [
    "MIXED",
    "MODEL",
    "PERSON",
    "POLICY",
    "SCHEMA",
    "AgentTrajectory",
    "Step",
    "Trajectory",
    "build_trajectory",
    "validate",
]
