"""Phase logic: prompt construction and pure transformations.

Everything here is side-effect free. The supervisor owns I/O, event emission and
sequencing; this module owns *what a stage should ask* and *what its answer
means*. Keeping the split lets every phase be tested without a model or a store.

Each phase has a deterministic component that runs regardless of whether a model
is involved -- lens selection, definition-of-done arithmetic, checkpoint scoring
from verified criteria -- so a run still makes correct decisions when a model is
unavailable or answers badly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..agents.registry import AgentRegistry
from ..agents.roles import ROLES_BY_ID, Role, role_for_task, select_lenses
from ..config import HarnessConfig, Policy
from ..ids import now_iso
from ..models import (
    AgentKind,
    AgentSpec,
    AgentStatus,
    Budget,
    Checkpoint,
    CriterionStatus,
    ExecutionTask,
    Finding,
    Lesson,
    LessonCategory,
    ModelBinding,
    Report,
    RunMode,
    RunState,
    Scope,
    TaskStatus,
)
from .blackboard import (
    contested_keys,
    critical_findings,
    detect_contradictions,
    rank_findings,
)
from .dod import apply_quality_bars, summarise, validate_criteria

# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def required_lenses(config: HarnessConfig) -> list[str]:
    """Lenses policy insists on, whatever the prompt or the planner says."""
    return ["security"] if config.policy.require_security_review else []


def plan_lenses(state: RunState, config: HarnessConfig) -> list[Role]:
    """Choose analysis lenses deterministically, honouring policy requirements."""
    policy = config.policy
    require = required_lenses(config)
    return select_lenses(
        state.prompt,
        minimum=policy.min_analysis_lenses,
        maximum=policy.max_analysis_lenses,
        require=require,
    )


def build_analysis_agents(
    state: RunState,
    config: HarnessConfig,
    registry: AgentRegistry,
    lenses: list[Role],
) -> list[AgentSpec]:
    """Turn chosen lenses into briefed, model-bound, budgeted agents."""
    specs: list[AgentSpec] = []
    for role in lenses:
        match = registry.match(role)
        specs.append(
            AgentSpec(
                run_id=state.id,
                role=role.id,
                kind=AgentKind.ANALYSIS,
                title=role.title,
                brief=role.charter,
                objectives=list(role.objectives),
                scope=Scope(out_of_scope=list(role.out_of_scope)),
                binding=config.binding_for(role.stage),
                backend=state.backend,
                budget=Budget(max_turns=config.policy.default_max_turns),
                host_agent_type=match.name if match and match.spawnable else None,
            )
        )
    return specs


def planning_prompt(
    state: RunState, registry: AgentRegistry, lenses: list[Role]
) -> tuple[str, str]:
    """Ask a model to sharpen the deterministic plan for this specific task."""
    system = (
        "You are the planning stage of a supervised multi-agent run. A deterministic "
        "pre-selection has already chosen which analysis lenses to run. Your job is to "
        "sharpen them for this particular task, not to redesign the process.\n\n"
        "For each lens, replace its generic objectives with two to four objectives "
        "specific enough that an agent could fail to meet them. Draw the scope so the "
        "lenses do not duplicate each other's work. Decide whether the user is asking "
        "for analysis ('report') or for work to be done ('execute').\n\n"
        "You may drop a lens that genuinely does not apply, and you may add one from the "
        "available list if the pre-selection missed something material. Do not add "
        "lenses speculatively -- each one costs a parallel agent.\n\n"
        "Finally, draw the run's envelope: every path this whole run may modify. "
        "It bounds every agent and every task that follows, including work nobody "
        "has proposed yet, so draw it around the change the task implies rather "
        "than around any one lens -- and remember that a change usually has tests. "
        "Nothing later in the run can widen it, and the harness narrows it further "
        "to the workspace's own configured envelope if you exceed that."
    )
    available = ", ".join(sorted(ROLES_BY_ID))
    preselected = "\n".join(
        f"- {r.id} ({r.title}): {r.summary}\n  default objectives: "
        + "; ".join(r.objectives)
        for r in lenses
    )
    user = (
        f"# Task\n{state.prompt}\n\n"
        f"# Workspace\n{state.workspace}\n\n"
        f"# Pre-selected lenses\n{preselected}\n\n"
        f"# All available lenses\n{available}\n\n"
        "Produce the plan."
    )
    return system, user


def apply_plan(
    state: RunState,
    plan: dict,
    config: HarnessConfig,
    registry: AgentRegistry,
    fallback: list[AgentSpec],
) -> tuple[list[AgentSpec], str, RunMode]:
    """Merge a model's plan over the deterministic one.

    The model may sharpen objectives and scope, but the harness keeps ownership
    of budgets, model bindings and the maximum agent count, so a bad plan cannot
    make a run unbounded.
    """
    entries = plan.get("lenses") or []
    if not entries:
        return fallback, str(plan.get("shared_context", "")), RunMode.AUTO

    specs: list[AgentSpec] = []
    for entry in entries[: config.policy.max_analysis_lenses]:
        if not isinstance(entry, dict):
            continue
        role_id = str(entry.get("role", "")).strip().lower()
        role = ROLES_BY_ID.get(role_id)
        if role is None or role.kind is not AgentKind.ANALYSIS:
            continue
        objectives = [str(o).strip() for o in (entry.get("objectives") or []) if str(o).strip()]
        match = registry.match(role)
        specs.append(
            AgentSpec(
                run_id=state.id,
                role=role.id,
                kind=AgentKind.ANALYSIS,
                title=role.title,
                brief=role.charter,
                objectives=objectives or list(role.objectives),
                scope=Scope(
                    paths=[str(p) for p in (entry.get("scope_paths") or [])],
                    out_of_scope=[
                        *(str(o) for o in (entry.get("out_of_scope") or [])),
                        *role.out_of_scope,
                    ],
                ),
                binding=config.binding_for(role.stage),
                backend=state.backend,
                budget=Budget(max_turns=config.policy.default_max_turns),
                host_agent_type=match.name if match and match.spawnable else None,
            )
        )

    if not specs:
        return fallback, str(plan.get("shared_context", "")), RunMode.AUTO

    # A planning model may sharpen a lens but may not drop one that policy
    # requires. Silently removing the security pass is precisely the failure the
    # policy exists to prevent, and it would be invisible in the final report.
    chosen = {spec.role for spec in specs}
    for role_id in required_lenses(config):
        role = ROLES_BY_ID.get(role_id)
        if role_id in chosen or role is None:
            continue
        match = registry.match(role)
        specs.append(
            AgentSpec(
                run_id=state.id, role=role.id, kind=AgentKind.ANALYSIS, title=role.title,
                brief=role.charter, objectives=list(role.objectives),
                scope=Scope(out_of_scope=list(role.out_of_scope)),
                binding=config.binding_for(role.stage), backend=state.backend,
                budget=Budget(max_turns=config.policy.default_max_turns),
                host_agent_type=match.name if match and match.spawnable else None,
            )
        )

    mode = RunMode.EXECUTE if str(plan.get("mode", "")).lower() == "execute" else RunMode.REPORT
    return specs, str(plan.get("shared_context", "")), mode


# --------------------------------------------------------------------------
# Synthesis
# --------------------------------------------------------------------------


def _findings_digest(findings: list[Finding], limit: int = 40) -> str:
    if not findings:
        return "(no findings were produced)"
    lines = []
    for finding in rank_findings(findings)[:limit]:
        lines.append(
            f"- [{finding.severity.value}] ({finding.lens}, confidence {finding.confidence:.1f}) "
            f"`{finding.id}` {finding.title}"
        )
        if finding.detail:
            lines.append(f"    {finding.detail}")
        if finding.evidence:
            lines.append(f"    evidence: {'; '.join(finding.evidence[:3])}")
        if finding.recommendation:
            lines.append(f"    recommended: {finding.recommendation}")
    return "\n".join(lines)


def synthesis_prompt(state: RunState, mode_hint: RunMode) -> tuple[str, str]:
    """Ask for a merged view and, when work is called for, the tasks to do it."""
    system = (
        "You are the synthesis stage of a supervised multi-agent run. Several analysis "
        "agents examined one task from different angles. Merge their findings into a "
        "single coherent view.\n\n"
        "Your responsibilities, in order:\n"
        "1. Say what the analysis actually established, and how confident that is.\n"
        "2. Name every place two lenses disagree, and judge which is right. Do not "
        "paper over a disagreement by including both claims.\n"
        "3. Decide the deliverable: 'report' if the user wants to know something, "
        "'execute' if they want something changed.\n"
        "4. If 'execute', propose the tasks.\n\n"
        "Every proposed task must name, in rationale_refs, the ids of the findings it "
        "closes -- copied verbatim from the findings list below. A task that closes no "
        "finding is work nobody asked for; a finding no task closes stays open, and the "
        "run says so at the end.\n\n"
        "Every proposed task must carry a definition of done that someone else could "
        "verify without asking you what you meant. Each criterion must be a single "
        "condition, objectively checkable, and at least one per task must be provable "
        "by running a command, running tests, or inspecting a file for specific "
        "content. Criteria like 'the code is clean' are rejected by the harness.\n\n"
        "Three ways a criterion looks checkable and is not. The harness rejects or "
        "supplements all three, so write them correctly rather than have them added "
        "for you:\n"
        "1. A command that runs part of a suite must say which tests it means. A "
        "filter that selects nothing still exits 0 -- 'pytest -k thing', 'go test "
        "-run Thing' -- so name node ids "
        "(tests/test_lock.py::test_stale_lock_is_broken), or set expect to the "
        "minimum selection ('7 passed').\n"
        "2. If the task exists to make something refuse -- a scope fence, an "
        "allow-list, a lock, a quota -- one criterion must be a negative test that "
        "drives the concrete shape from the finding that motivated it (the actual "
        "traversal path, the actual destructive command, the actual exception) and "
        "asserts the refusal. Criteria the implementer can satisfy with their own "
        "happy-path tests prove nothing about a guard.\n"
        "3. If the task touches locking, retries, timeouts or I/O, one criterion must "
        "show it still terminates: a test that drives the contended or failing path "
        "to completion inside a stated wall-clock bound. Replacing a crash with an "
        "unbounded retry loop passes every other kind of criterion.\n\n"
        "Propose the smallest set of tasks that fully addresses the findings. Do not "
        "invent work the findings do not support."
    )

    contradictions = detect_contradictions(state.findings)
    agent_notes = "\n".join(
        f"- `{a.id}` {a.title} ({a.role}): {a.status.value}"
        + (f", drift {state.drift[a.id].score}" if a.id in state.drift else "")
        for a in state.agents.values()
    )
    conflict_block = (
        "\n\n# Possible contradictions detected mechanically\n"
        + "\n".join(f"- {c}" for c in contradictions)
        if contradictions
        else ""
    )
    unresolved = [
        m for m in state.messages if m.kind.value in ("question", "contradiction", "warning")
    ]
    message_block = (
        "\n\n# Cross-agent traffic worth reading\n"
        + "\n".join(f"- `{m.sender}` -> `{m.recipient}` ({m.kind.value}): {m.content[:220]}"
                    for m in unresolved[:15])
        if unresolved
        else ""
    )

    user = (
        f"# Original request\n{state.prompt}\n\n"
        f"# Agents\n{agent_notes}\n\n"
        f"# Findings\n{_findings_digest(state.findings)}"
        f"{conflict_block}{message_block}\n\n"
        f"# Hint\nThe planning stage judged this run to be '{mode_hint.value}'. "
        "Override that if the findings say otherwise.\n\n"
        "Produce the synthesis."
    )
    return system, user


def build_report(state: RunState, data: dict) -> Report:
    conflicts = [str(c) for c in (data.get("conflicts") or [])]
    if not conflicts:
        conflicts = detect_contradictions(state.findings)
    # A key two agents claimed differently is a disagreement of the same kind,
    # reached from the other direction: `detect_contradictions` infers one from
    # findings that clash, and this one the agents stated outright by keying
    # their claims the same way. Appended rather than merged into that list, and
    # never suppressed by the synthesis model having offered its own conflicts:
    # a model that did not notice the disagreement is exactly when it matters.
    for key, claims in contested_keys(state.established).items():
        conflicts.append(
            f"agents disagree about {key}: "
            + "; ".join(f"{c.role or c.agent_id} says {c.statement}" for c in claims)
        )
    return Report(
        run_id=state.id,
        title=state.prompt[:120],
        summary=str(data.get("summary", "")).strip(),
        findings=rank_findings(state.findings),
        conflicts=conflicts,
        open_questions=[str(q) for q in (data.get("open_questions") or [])],
        recommended_mode=(
            RunMode.EXECUTE
            if str(data.get("recommended_mode", "")).lower() == "execute"
            else RunMode.REPORT
        ),
    )


def resolve_dependencies(tasks: list[ExecutionTask]) -> dict[str, list[str]]:
    """Rewrite each ``depends_on`` entry to the id of the task it names.

    The synthesis model writes dependencies as titles, because the ids do not
    exist until the harness parses its answer. :func:`runnable_tasks` compares
    them against a set of ids, so an unresolved title never matches and the
    task is silently unrunnable for the life of the run -- a user can approve
    work that can never be dispatched. Resolving here, once, is what makes the
    comparison downstream meaningful.

    An entry naming no task is dropped rather than left to block forever, and
    reported per task id so the user sees it before approving.
    """
    by_title = {t.title.strip().casefold(): t.id for t in tasks if t.title.strip()}
    known_ids = {t.id for t in tasks}
    notes: dict[str, list[str]] = {}
    for task in tasks:
        resolved: list[str] = []
        for dep in task.depends_on:
            entry = str(dep).strip()
            if entry in known_ids:
                resolved.append(entry)
                continue
            match = by_title.get(entry.casefold())
            if match is None:
                notes.setdefault(task.id, []).append(
                    f"dropped dependency naming no task in this plan: {entry!r}"
                )
            elif match == task.id:
                notes.setdefault(task.id, []).append(
                    f"dropped self-dependency: {entry!r}"
                )
            elif match not in resolved:
                resolved.append(match)
        task.depends_on = resolved
    return notes


def prepare_tasks(
    tasks: list[ExecutionTask], policy: Policy, workspace: Path | None = None
) -> tuple[list[ExecutionTask], dict[str, list[str]]]:
    """Apply mandatory quality bars and record what needed fixing.

    Returns the tasks plus, per task id, the notes the user should see: which
    criteria the harness added, and which of the proposed ones are too weak to
    verify.
    """
    notes: dict[str, list[str]] = {}
    for task in tasks:
        entries: list[str] = []
        added = apply_quality_bars(task, policy, workspace)
        for crit in added:
            entries.append(f"harness added mandatory criterion: {crit.statement}")
        for issue in validate_criteria(task.dod, policy):
            entries.append(f"weak criterion ({issue.severity.value}): {issue.problem}")
        if entries:
            notes[task.id] = entries
    return tasks, notes


def resolve_rationale_refs(
    tasks: list[ExecutionTask], findings: list[Finding]
) -> dict[str, list[str]]:
    """Rewrite each task's ``rationale_refs`` to the ids of the findings it closes.

    A task is derived from findings, and until this runs nothing records which
    ones. Without that mapping the end of a run cannot separate "fixed here"
    from "still open", and someone reconstructs it by hand from the report --
    which is most of a follow-up prompt, every time.

    The synthesis model is asked for finding ids and frequently answers with
    titles instead, so a title is resolved too. A reference naming no finding is
    dropped rather than kept as a dangling id, and reported per task so the user
    sees it before approving; so is a task that cites nothing at all, which is
    either invented work or a mapping the model declined to make.
    """
    by_id = {f.id: f.id for f in findings}
    by_title = {f.title.strip().casefold(): f.id for f in findings if f.title.strip()}
    notes: dict[str, list[str]] = {}

    for task in tasks:
        resolved: list[str] = []
        for raw in task.rationale_refs:
            entry = str(raw).strip()
            match = by_id.get(entry) or by_title.get(entry.casefold())
            if match is None:
                notes.setdefault(task.id, []).append(
                    f"dropped a reference naming no finding in this run: {entry!r}"
                )
            elif match not in resolved:
                resolved.append(match)
        task.rationale_refs = resolved
        if not resolved:
            notes.setdefault(task.id, []).append(
                "closes no finding: this task is not traceable to anything the "
                "analysis established, and nothing at the end of the run will be "
                "able to say what it fixed"
            )
    return notes


# How a finding stood when the run ended. Ordered worst first: what is still
# open is the part a reader has to act on.
FINDING_OPEN = "open"
FINDING_ATTEMPTED = "attempted"
FINDING_PENDING = "pending"
FINDING_FIXED = "fixed"

_RECONCILED_ORDER = (FINDING_OPEN, FINDING_ATTEMPTED, FINDING_PENDING, FINDING_FIXED)

_RECONCILED_HEADING = {
    FINDING_OPEN: "Still open",
    FINDING_ATTEMPTED: "Attempted, not proven",
    FINDING_PENDING: "Approved, not finished",
    FINDING_FIXED: "Fixed and proven in this run",
}

_UNSTARTED = (TaskStatus.PROPOSED, TaskStatus.REJECTED, TaskStatus.DEFERRED)


@dataclass
class ReconciledFinding:
    """One finding, and what this run actually did about it."""

    finding: Finding
    state: str
    reason: str
    task_ids: list[str] = field(default_factory=list)


def reconcile_findings(state: RunState) -> list[ReconciledFinding]:
    """Every finding, mapped to the tasks that claimed it and how they ended.

    A finding is only ``fixed`` when a task that named it was verified with its
    definition of done met. Anything else is still the reader's problem, and
    says which kind of problem it is.
    """
    out: list[ReconciledFinding] = []
    for finding in rank_findings(state.findings):
        tasks = [t for t in state.tasks.values() if finding.id in t.rationale_refs]
        if not tasks:
            out.append(
                ReconciledFinding(finding, FINDING_OPEN, "no task claimed this finding")
            )
            continue

        ids = [t.id for t in tasks]
        proven = [t for t in tasks if t.status is TaskStatus.VERIFIED and t.dod_satisfied()]
        proven_ids = {t.id for t in proven}
        running = [
            t for t in tasks
            if t.status not in _UNSTARTED and t.id not in proven_ids
        ]
        if proven:
            titles = ", ".join(f"{t.title!r}" for t in proven)
            out.append(ReconciledFinding(finding, FINDING_FIXED, f"proven by {titles}", ids))
        elif any(t.status is TaskStatus.FAILED for t in running):
            unmet = [c.statement for t in running for c in t.unmet_criteria()]
            out.append(
                ReconciledFinding(
                    finding,
                    FINDING_ATTEMPTED,
                    "the task failed its definition of done"
                    + (f": {unmet[0]}" if unmet else ""),
                    ids,
                )
            )
        elif running:
            out.append(
                ReconciledFinding(
                    finding,
                    FINDING_PENDING,
                    "approved but not verified when the run ended",
                    ids,
                )
            )
        else:
            decided = ", ".join(sorted({t.status.value for t in tasks}))
            out.append(
                ReconciledFinding(
                    finding, FINDING_OPEN, f"every task claiming it was {decided}", ids
                )
            )
    return out


def _reconciliation_counts(rows: list[ReconciledFinding]) -> str:
    counts = {state: sum(1 for r in rows if r.state == state) for state in _RECONCILED_ORDER}
    return (
        f"{len(rows)} finding(s): {counts[FINDING_FIXED]} fixed, "
        f"{counts[FINDING_ATTEMPTED]} attempted, {counts[FINDING_PENDING]} pending, "
        f"{counts[FINDING_OPEN]} still open."
    )


def reconciliation_markdown(state: RunState) -> str:
    """The run's findings, each mapped to what was done about it.

    Written as an artifact of every run, because the question it answers -- what
    did this run actually close? -- is asked after the run is over, by someone
    who no longer has the event log in front of them.
    """
    rows = reconcile_findings(state)
    lines = [f"# Findings reconciliation: {state.prompt}", ""]
    lines.append(f"- Run `{state.id}` -- {state.phase.value}")
    lines.append("")
    if not rows:
        lines += ["This run produced no findings.", ""]
        return "\n".join(lines)

    lines += [_reconciliation_counts(rows), ""]
    if not state.approved_tasks():
        lines += [
            "No execution task ran in this run, so every finding below is still open.",
            "",
        ]

    for group in _RECONCILED_ORDER:
        entries = [r for r in rows if r.state == group]
        if not entries:
            continue
        lines += [f"## {_RECONCILED_HEADING[group]}", ""]
        for row in entries:
            finding = row.finding
            lines.append(
                f"- **[{finding.severity.value}]** `{finding.id}` {finding.title}"
            )
            claimed = ", ".join(f"`{t}`" for t in row.task_ids)
            lines.append(f"    - {row.reason}" + (f" ({claimed})" if claimed else ""))
            if group != FINDING_FIXED and finding.recommendation:
                lines.append(f"    - recommended: {_display(finding.recommendation)}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


# --------------------------------------------------------------------------
# Execution assignment
# --------------------------------------------------------------------------


def runnable_tasks(state: RunState) -> list[ExecutionTask]:
    """Approved tasks whose dependencies are already verified."""
    done = {t.id for t in state.tasks.values() if t.status is TaskStatus.VERIFIED}
    out = []
    for task in state.tasks.values():
        if task.status not in (TaskStatus.APPROVED, TaskStatus.FAILED):
            continue
        if task.attempts >= 1 and task.status is TaskStatus.FAILED:
            continue
        if all(dep in done for dep in task.depends_on):
            out.append(task)
    return out


def build_execution_agent(
    state: RunState,
    task: ExecutionTask,
    config: HarnessConfig,
    registry: AgentRegistry,
) -> AgentSpec:
    role = role_for_task(task.title, task.action, task.suggested_role)
    match = registry.match(role)
    binding: ModelBinding = task.suggested_binding or config.binding_for(role.stage)
    return AgentSpec(
        run_id=state.id,
        role=role.id,
        kind=AgentKind.EXECUTION,
        title=f"{role.title}: {task.title}",
        brief=role.charter,
        objectives=[task.action, *[c.statement for c in task.mandatory_criteria]],
        scope=Scope(
            paths=list(task.scope.paths),
            forbidden_paths=list(task.scope.forbidden_paths),
            out_of_scope=[*task.scope.out_of_scope, *role.out_of_scope],
        ),
        binding=binding,
        backend=state.backend,
        budget=Budget(max_turns=config.policy.execution_max_turns),
        host_agent_type=match.name if match and match.spawnable else None,
        task_id=task.id,
        attempt=task.attempts,
    )


def build_verification_agent(
    state: RunState,
    task: ExecutionTask,
    config: HarnessConfig,
    registry: AgentRegistry,
) -> AgentSpec:
    role = ROLES_BY_ID["verifier"]
    match = registry.match(role)
    return AgentSpec(
        run_id=state.id,
        role=role.id,
        kind=AgentKind.VERIFICATION,
        title=f"Verify: {task.title}",
        brief=role.charter,
        objectives=[f"Prove or disprove: {c.statement}" for c in task.mandatory_criteria],
        # The task's own paths, not an empty scope. An empty one reads as "the
        # whole workspace" to the toolbox, which made the verifier the least
        # fenced agent in the run -- free to write anywhere outside the floor,
        # and free of the executable allow-list a scoped agent is held to, while
        # judging an agent that was fenced. Attenuation at spawn would narrow
        # this anyway; it is written here so the brief the verifier reads says
        # the same thing the fence enforces.
        scope=Scope(
            paths=list(task.scope.paths),
            forbidden_paths=list(task.scope.forbidden_paths),
            out_of_scope=list(role.out_of_scope),
        ),
        binding=config.binding_for("verification"),
        backend=state.backend,
        # One turn, because one is what the code allows: `_report_verification`
        # settles the task and ends the agent on its first report, so the 3 this
        # used to declare was never reachable. Verification is a single judgement
        # by design -- a verifier given more turns would be negotiating with
        # itself over a verdict it has already reached -- so the budget is
        # corrected to match rather than the behaviour being loosened to match
        # the budget. Now that verification turns are recorded, this is enforced:
        # a second report is refused by the turn-budget bound in `report`.
        budget=Budget(max_turns=1),
        host_agent_type=match.name if match and match.spawnable else None,
        task_id=task.id,
        attempt=task.attempts,
    )


# --------------------------------------------------------------------------
# Checkpoint
# --------------------------------------------------------------------------


def deterministic_checkpoint(state: RunState, policy: Policy, iteration: int) -> Checkpoint:
    """Score the run from facts already recorded, before any model opinion.

    Completeness comes from verified criteria, scope fidelity from measured
    drift, and quality from how much of the work was proven rather than
    asserted. A model can adjust these, but it cannot invent a pass over
    criteria that plainly did not verify.
    """
    tasks = [t for t in state.tasks.values() if t.status not in
             (TaskStatus.PROPOSED, TaskStatus.REJECTED, TaskStatus.DEFERRED)]

    mandatory = [c for t in tasks for c in t.mandatory_criteria]
    proven = [c for c in mandatory if c.status is CriterionStatus.PASS]
    waived = [c for c in mandatory if c.status is CriterionStatus.WAIVED]
    failed = [c for c in mandatory if c.status is CriterionStatus.FAIL]

    completeness = (
        (len(proven) + 0.5 * len(waived)) / len(mandatory) if mandatory else 0.0
    )

    drifts = [d.score for d in state.drift.values()] or [0.0]
    scope_fidelity = max(0.0, 1.0 - sum(drifts) / len(drifts))

    # Quality: proportion of proven criteria that were proven mechanically
    # rather than by review, penalised for unverified and failed criteria.
    machine_proven = [c for c in proven if c.machine_checkable]
    quality = (
        (0.5 + 0.5 * (len(machine_proven) / len(proven))) if proven else 0.0
    )
    if failed:
        quality *= max(0.2, 1.0 - 0.25 * len(failed))

    gaps: list[str] = []
    for task in tasks:
        unmet = task.unmet_criteria()
        if unmet:
            gaps.append(
                f"`{task.id}` {task.title}: {summarise(task)}; unmet -- "
                + "; ".join(c.statement for c in unmet[:3])
            )
    for agent in state.agents.values():
        if agent.status is AgentStatus.STOPPED:
            gaps.append(f"agent `{agent.id}` ({agent.role}) was stopped before finishing")
        elif agent.status is AgentStatus.BLOCKED:
            gaps.append(f"agent `{agent.id}` ({agent.role}) remained blocked")
        elif agent.status is AgentStatus.FAILED:
            # Abandoned, or blown up mid-run. Either way its share of the work
            # was never done, and a checkpoint that cannot see that scores the
            # run as though nothing were missing.
            gaps.append(f"agent `{agent.id}` ({agent.role}) ended without reporting")

    passed = (
        not failed
        and completeness >= policy.checkpoint_threshold
        and scope_fidelity >= policy.checkpoint_threshold
        and not any(t.unmet_criteria() for t in tasks)
    )

    return Checkpoint(
        run_id=state.id,
        iteration=iteration,
        quality=round(quality, 3),
        scope_fidelity=round(scope_fidelity, 3),
        completeness=round(completeness, 3),
        passed=passed,
        gaps=gaps,
        summary=(
            f"{len(proven)}/{len(mandatory)} mandatory criteria proven across "
            f"{len(tasks)} task(s)"
        ),
    )


def checkpoint_prompt(state: RunState, deterministic: Checkpoint) -> tuple[str, str]:
    system = (
        "You are the quality checkpoint of a supervised run. Mechanical scoring has "
        "already been computed from verified criteria and measured drift; you are "
        "judging what the numbers cannot see.\n\n"
        "Assess three things:\n"
        "- quality: is the delivered work actually good, or merely present?\n"
        "- scope_fidelity: did the work stay within what the user approved?\n"
        "- completeness: does this fully answer the original request, including the "
        "parts nobody wrote a criterion for?\n\n"
        "You may lower the mechanical scores freely. Raise them only with a stated "
        "reason. If the run falls short, give remediation instructions specific enough "
        "for an agent to act on directly.\n\n"
        "Separately, list 'avoidable_causes': the shortfalls that better briefing, "
        "scoping or definitions of done would have prevented. These become lessons for "
        "future runs, so name the process failure, not the agent."
    )

    task_block = "\n".join(
        f"- `{t.id}` {t.title} [{t.status.value}] -- {summarise(t)}\n"
        f"    action: {t.action}\n"
        + "".join(
            f"    - [{c.status.value}] {c.statement}"
            + (f"\n        evidence: {c.evidence[:200]}" if c.evidence else "")
            + "\n"
            for c in t.dod
        )
        for t in state.tasks.values()
        if t.status not in (TaskStatus.PROPOSED, TaskStatus.REJECTED, TaskStatus.DEFERRED)
    ) or "(no tasks were executed)"

    user = (
        f"# Original request\n{state.prompt}\n\n"
        f"# Mechanical scoring\n"
        f"quality={deterministic.quality} scope_fidelity={deterministic.scope_fidelity} "
        f"completeness={deterministic.completeness} passed={deterministic.passed}\n"
        f"{deterministic.summary}\n\n"
        f"# Gaps found mechanically\n"
        + ("\n".join(f"- {g}" for g in deterministic.gaps) or "(none)")
        + f"\n\n# Tasks and their criteria\n{task_block}\n\n"
        f"# Key findings from analysis\n"
        f"{_findings_digest(critical_findings(state.findings), limit=15)}\n\n"
        "Produce the checkpoint judgement."
    )
    return system, user


def merge_checkpoint(deterministic: Checkpoint, model: Checkpoint, policy: Policy) -> Checkpoint:
    """Combine mechanical scoring with the model's judgement.

    The model may always lower a score. It may raise one only slightly, because
    an unverified criterion is unverified no matter how convincing the prose
    around it is, and a pass is never granted over a failing criterion.
    """
    def blend(mech: float, judged: float) -> float:
        return round(min(judged, mech + 0.1) if judged > mech else judged, 3)

    quality = blend(deterministic.quality, model.quality)
    scope = blend(deterministic.scope_fidelity, model.scope_fidelity)
    completeness = blend(deterministic.completeness, model.completeness)

    passed = (
        deterministic.passed
        and model.passed
        and min(quality, scope, completeness) >= policy.checkpoint_threshold
    )

    return Checkpoint(
        id=deterministic.id,
        run_id=deterministic.run_id,
        iteration=deterministic.iteration,
        quality=quality,
        scope_fidelity=scope,
        completeness=completeness,
        passed=passed,
        gaps=[*deterministic.gaps, *model.gaps],
        remediation=model.remediation,
        avoidable_causes=model.avoidable_causes,
        summary=model.summary or deterministic.summary,
        ts=now_iso(),
    )


# --------------------------------------------------------------------------
# Improvement loop
# --------------------------------------------------------------------------


def mechanical_lessons(state: RunState) -> list[Lesson]:
    """Lessons derivable from the run's own record, with no model involved."""
    lessons: list[Lesson] = []

    for agent_id, assessment in state.drift.items():
        agent = state.agents.get(agent_id)
        if agent is None or assessment.score < 0.6:
            continue
        kinds = {s.kind for s in assessment.signals}
        if "scope_paths" in kinds or "forbidden_paths" in kinds:
            lessons.append(
                Lesson(
                    run_id=state.id,
                    workspace=state.workspace,
                    category=LessonCategory.SCOPE,
                    trigger=f"agent `{agent_id}` ({agent.role}) worked outside its scope: "
                            f"{assessment.summary}",
                    statement=f"The {agent.role} brief did not fence its file scope tightly enough",
                    why="An unfenced scope lets an agent expand into work another agent owns, "
                        "which wastes budget and produces conflicting changes.",
                    how_to_apply=f"When briefing `{agent.role}`, list explicit scope paths and "
                                 f"forbidden paths before dispatch.",
                    target=agent.role,
                    confidence=0.7,
                )
            )
        if "brief_echo" in kinds:
            lessons.append(
                Lesson(
                    run_id=state.id,
                    workspace=state.workspace,
                    category=LessonCategory.BRIEFING,
                    trigger=f"agent `{agent_id}` ({agent.role}) restated its brief instead of "
                            f"answering it",
                    statement=f"The {agent.role} brief invited restatement rather than evidence",
                    why="Objectives phrased as topics get echoed back; objectives phrased as "
                        "questions with required evidence do not.",
                    how_to_apply=f"Phrase each `{agent.role}` objective as a question that "
                                 f"requires a file:line citation to answer.",
                    target=agent.role,
                    confidence=0.6,
                )
            )

    for task in state.tasks.values():
        for crit in task.dod:
            if crit.status is CriterionStatus.BLOCKED:
                lessons.append(
                    Lesson(
                        run_id=state.id,
                    workspace=state.workspace,
                        category=LessonCategory.DOD,
                        trigger=(
                f"criterion {crit.id!r} could not be checked: {crit.evidence[:160]}"
            ),
                        statement="A definition-of-done criterion was written in a form that "
                                  "could not be verified",
                        why="A criterion that cannot be checked cannot close a task, so the "
                            "task stalls at verification.",
                        how_to_apply=(
                            f"For {crit.method.value} criteria, require the exact command and "
                            "expected result at proposal time, and reject the task otherwise."
                        ),
                        target="dod",
                        confidence=0.65,
                    )
                )
        if task.attempts > 1:
            lessons.append(
                Lesson(
                    run_id=state.id,
                    workspace=state.workspace,
                    category=LessonCategory.BRIEFING,
                    trigger=f"task `{task.id}` needed {task.attempts} attempts",
                    statement=f"Tasks of this shape ({task.suggested_role or 'implementation'}) "
                              "need a sharper brief on the first attempt",
                    why="Repeated attempts usually mean the definition of done was clear but "
                        "the action was not.",
                    how_to_apply="State the concrete first change in the action field, not the "
                                 "outcome; the outcome belongs in the criteria.",
                    target=task.suggested_role or "implementer",
                    confidence=0.55,
                )
            )
    return lessons


def lessons_prompt(state: RunState, checkpoint: Checkpoint | None) -> tuple[str, str]:
    system = (
        "You are the improvement stage of a supervised run. Extract lessons that would "
        "make the *next* run better.\n\n"
        "A lesson is only useful if it is reusable and actionable. 'The agent should "
        "have been more careful' is neither. 'Security briefs must name the trust "
        "boundary explicitly, because agents otherwise audit the whole file' is both.\n\n"
        "Only record a lesson when this run gives real evidence for it. Prefer few, "
        "well-evidenced lessons over many plausible ones. Target each lesson at the "
        "thing that should change: a role id, 'supervisor', 'dod', or '*'."
    )

    drift_block = "\n".join(
        f"- `{aid}` ({state.agents[aid].role if aid in state.agents else '?'}): "
        f"score {d.score}, {d.summary[:200]}"
        for aid, d in state.drift.items()
        if d.score > 0.3
    ) or "(no significant drift)"

    checkpoint_block = (
        f"passed={checkpoint.passed} quality={checkpoint.quality} "
        f"scope={checkpoint.scope_fidelity} completeness={checkpoint.completeness}\n"
        + "\n".join(f"- gap: {g}" for g in checkpoint.gaps)
        + "\n"
        + "\n".join(f"- avoidable: {c}" for c in checkpoint.avoidable_causes)
        if checkpoint
        else "(no checkpoint was recorded)"
    )

    user = (
        f"# Original request\n{state.prompt}\n\n"
        f"# Outcome\nphase={state.phase.value}, {len(state.findings)} findings, "
        f"{len(state.tasks)} tasks\n\n"
        f"# Checkpoint\n{checkpoint_block}\n\n"
        f"# Drift observed\n{drift_block}\n\n"
        f"# Tasks\n"
        + ("\n".join(f"- `{t.id}` {t.title} [{t.status.value}] {summarise(t)}"
                     for t in state.tasks.values()) or "(none)")
        + "\n\nExtract the lessons."
    )
    return system, user


# --------------------------------------------------------------------------
# Final report
# --------------------------------------------------------------------------


def _display(text: str) -> str:
    """Tidy model-escaped text for reading.

    Models frequently emit ``\\"`` and ``\\n`` inside JSON string values that are
    already escaped once, so the literal sequences survive parsing and end up in
    the report. Only these two are unescaped, and only for display.
    """
    return text.replace('\\"', '"').replace("\\n", " ").strip()


def final_report_markdown(state: RunState) -> str:
    """The document handed back to the user at the end of a run."""
    lines: list[str] = [f"# Supervised run: {state.prompt}", ""]
    lines.append(f"- Run `{state.id}` -- {state.phase.value}")
    lines.append(f"- Mode: {state.mode.value}, backend: {state.backend.value}, host: {state.host}")
    usage = state.total_usage()
    if usage.total_tokens:
        lines.append(f"- Tokens: {usage.input_tokens} in / {usage.output_tokens} out")
    lines.append("")

    if state.report and state.report.summary:
        lines += ["## Summary", "", state.report.summary, ""]

    tasks = [t for t in state.tasks.values() if t.status not in
             (TaskStatus.PROPOSED, TaskStatus.REJECTED, TaskStatus.DEFERRED)]
    if tasks:
        lines += ["## Definition-of-done verification", ""]
        for task in tasks:
            mark = "PASS" if task.dod_satisfied() else "NOT MET"
            lines.append(f"### {task.title} -- **{mark}**")
            lines.append("")
            lines.append(f"{task.action}")
            lines.append("")
            for crit in task.dod:
                flag = {"pass": "x", "fail": " ", "unverified": "?",
                        "blocked": "!", "waived": "~"}.get(crit.status.value, " ")
                required = "" if crit.mandatory else " _(optional)_"
                lines.append(f"- [{flag}] {crit.statement}{required}")
                lines.append(
                f"    - method: `{crit.method.value}`, status: **{crit.status.value}**"
            )
                if crit.evidence:
                    evidence = _display(crit.evidence).splitlines()
                    lines.append(f"    - evidence: `{evidence[0][:160]}`")
                    if len(evidence) > 1:
                        lines.append(f"      ({len(evidence) - 1} more line(s) in the event log)")
            lines.append("")

    if state.findings:
        lines += ["## Findings", ""]
        for finding in rank_findings(state.findings)[:25]:
            lines.append(f"- **[{finding.severity.value}]** ({finding.lens}) {finding.title}")
            if finding.detail:
                lines.append(f"    - {_display(finding.detail)}")
            if finding.evidence:
                lines.append(
                    "    - evidence: "
                    + "; ".join(_display(e) for e in finding.evidence[:2])
                )
            if finding.recommendation:
                lines.append(f"    - recommended: {_display(finding.recommendation)}")
        lines.append("")

    rows = reconcile_findings(state)
    if rows:
        lines += ["## Findings reconciliation", ""]
        lines.append(_reconciliation_counts(rows))
        lines.append("")
        outstanding = [r for r in rows if r.state != FINDING_FIXED]
        if outstanding:
            lines.append("Not closed by this run:")
            for row in outstanding[:25]:
                lines.append(
                    f"- **[{row.finding.severity.value}]** ({row.state}) "
                    f"{row.finding.title} -- {row.reason}"
                )
            if len(outstanding) > 25:
                lines.append(f"- ...and {len(outstanding) - 25} more")
        else:
            lines.append("Every finding this run produced was closed and proven.")
        lines.append("")
        lines.append("Full mapping, finding by finding: `artifacts/reconciliation.md`.")
        lines.append("")

    if state.report and state.report.conflicts:
        lines += ["## Disagreements between lenses", ""]
        lines += [f"- {c}" for c in state.report.conflicts]
        lines.append("")

    if state.report and state.report.open_questions:
        lines += ["## Open questions", ""]
        lines += [f"- {q}" for q in state.report.open_questions]
        lines.append("")

    if state.checkpoints:
        cp = state.checkpoints[-1]
        lines += [
            "## Quality checkpoint",
            "",
            f"- Verdict: **{'passed' if cp.passed else 'not passed'}** "
            f"(iteration {cp.iteration})",
            f"- Quality {cp.quality} / scope fidelity {cp.scope_fidelity} / "
            f"completeness {cp.completeness}",
        ]
        if cp.summary:
            lines.append(f"- {cp.summary}")
        for gap in cp.gaps:
            lines.append(f"- Gap: {gap}")
        lines.append("")

    if state.lessons:
        lines += ["## Lessons recorded for future runs", ""]
        for lesson in state.lessons:
            lines.append(f"- **{lesson.statement}** (target: `{lesson.target}`)")
            if lesson.how_to_apply:
                lines.append(f"    - {lesson.how_to_apply}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"
