"""Domain model for the supervisor harness.

Every persisted structure lives here. The event log is the source of truth;
:class:`RunState` is the projection produced by folding events, so nothing in
this module should carry behaviour beyond trivial derivations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .ids import new_id, now_iso

# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class Phase(StrEnum):
    """Lifecycle of a supervised run."""

    CREATED = "created"
    ANALYZING = "analyzing"
    SYNTHESIZING = "synthesizing"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    CHECKPOINT = "checkpoint"
    IMPROVING = "improving"
    COMPLETE = "complete"
    FAILED = "failed"
    ABORTED = "aborted"


TERMINAL_PHASES = {Phase.COMPLETE, Phase.FAILED, Phase.ABORTED}


class RunMode(StrEnum):
    """What the run is expected to produce."""

    REPORT = "report"      # analysis only; end with a written report
    EXECUTE = "execute"    # analysis, then approved execution tasks
    AUTO = "auto"          # let synthesis decide which of the two applies


class Backend(StrEnum):
    """Who actually runs an agent's turns."""

    HOST = "host"              # delegated to Claude Code / Cursor
    AUTONOMOUS = "autonomous"  # driven by the harness against a model provider


class AgentKind(StrEnum):
    ANALYSIS = "analysis"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    SYNTHESIS = "synthesis"


class AgentStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    AWAITING_DIRECTIVE = "awaiting_directive"
    DONE = "done"
    STOPPED = "stopped"
    FAILED = "failed"


# The statuses an agent can still be driven from. BLOCKED is deliberately not
# one of them: an escalation ends the agent (``status_after`` maps ESCALATE to
# BLOCKED and neither backend drives it further), so counting it as active made
# every phase re-dispatch an agent that had already finished, until the run was
# failed for not settling.
ACTIVE_AGENT_STATUSES = {
    AgentStatus.PENDING,
    AgentStatus.RUNNING,
    AgentStatus.AWAITING_DIRECTIVE,
}


class DirectiveKind(StrEnum):
    """Supervisor's response to an agent turn."""

    CONTINUE = "continue"    # on track, keep going
    REFOCUS = "refocus"      # drifted; return to the stated objective
    NARROW = "narrow"        # scope crept; cut back to the listed scope
    DEEPEN = "deepen"        # too shallow for the objective
    ANSWER = "answer"        # supervisor answers a blocking question
    ESCALATE = "escalate"    # needs the user or another agent
    ACCEPT = "accept"        # work accepted, agent may finish
    REJECT = "reject"        # output rejected, redo against the corrections
    STOP = "stop"            # halt (budget, futility, or supersession)


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_ORDER = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class VerifyMethod(StrEnum):
    """How a definition-of-done criterion is proven."""

    COMMAND = "command"        # run a command; exit code / output must match
    TEST = "test"              # named tests must exist and pass
    INSPECTION = "inspection"  # deterministic check of file content
    REVIEW = "review"          # judged against a rubric by a model or a human


class CriterionStatus(StrEnum):
    UNVERIFIED = "unverified"
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    WAIVED = "waived"


class TaskStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEFERRED = "deferred"
    IN_PROGRESS = "in_progress"
    AWAITING_VERIFICATION = "awaiting_verification"
    VERIFIED = "verified"
    FAILED = "failed"
    BLOCKED = "blocked"


class Decision(StrEnum):
    APPROVE = "approve"
    MODIFY = "modify"
    REJECT = "reject"
    DEFER = "defer"


class MessageKind(StrEnum):
    """Agent-to-agent and agent-to-supervisor traffic."""

    QUESTION = "question"
    ANSWER = "answer"
    FINDING = "finding"
    WARNING = "warning"
    HANDOFF = "handoff"
    REVIEW_REQUEST = "review_request"
    CONTRADICTION = "contradiction"
    NOTE = "note"


class LessonCategory(StrEnum):
    BRIEFING = "briefing"          # the brief was ambiguous or incomplete
    SCOPE = "scope"                # scope was drawn wrongly
    ROUTING = "routing"            # wrong role or model for the work
    DOD = "dod"                    # definition of done was unverifiable or weak
    DRIFT = "drift"                # a recurring drift pattern
    VERIFICATION = "verification"  # verification missed something
    PROCESS = "process"            # harness workflow itself
    TOOLING = "tooling"            # environment or tool failure


BROADCAST = "*"
SUPERVISOR = "supervisor"


# --------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------


@dataclass
class ModelBinding:
    """Which model answers for a given stage, and what to fall back to."""

    provider: str = "host"
    model: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    fallbacks: list[str] = field(default_factory=list)

    def ref(self) -> str:
        return f"{self.provider}:{self.model}" if self.model else self.provider


@dataclass
class Budget:
    """Hard ceilings; the supervisor stops an agent that exceeds them."""

    max_turns: int = 6
    max_tokens: int = 0        # 0 = unlimited
    max_seconds: int = 0       # 0 = unlimited
    max_tool_calls: int = 0    # 0 = unlimited

    def exhausted(
        self, turns: int, tokens: int = 0, seconds: float = 0.0, tool_calls: int = 0
    ) -> str | None:
        """The ceiling this agent has reached, or ``None``.

        Every argument but ``turns`` used to default its way to zero at the only
        call site, so three of the four declared ceilings were documentation. The
        caller now passes the agent's accumulated usage, and a figure the backend
        cannot measure arrives as zero -- which reads as "no evidence it was
        exceeded" rather than as "it was not".
        """
        if self.max_turns and turns >= self.max_turns:
            return f"turn budget exhausted ({turns}/{self.max_turns})"
        if self.max_tokens and tokens >= self.max_tokens:
            return f"token budget exhausted ({tokens}/{self.max_tokens})"
        if self.max_seconds and seconds >= self.max_seconds:
            return f"time budget exhausted ({seconds:.0f}s/{self.max_seconds}s)"
        if self.max_tool_calls and tool_calls >= self.max_tool_calls:
            return f"tool-call budget exhausted ({tool_calls}/{self.max_tool_calls})"
        return None


@dataclass
class Scope:
    """The fence an agent is expected to stay inside.

    ``paths``/``forbidden_paths`` are glob patterns; ``topics``/``out_of_scope``
    are short phrases used both in the brief and by the drift heuristics.
    """

    paths: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)


@dataclass
class ScopeEnvelope:
    """The ceiling on every scope in one run: what the run itself may touch.

    A :class:`Scope` is a fence around one agent, and the harness enforces one
    immaculately once it exists. Nothing used to constrain where one came from:
    each was supplied independently by a model, per lens and per task, and no
    two were ever compared. The envelope is the run's own grant, fixed before
    any task is proposed, and no agent in the run can be given authority it
    does not contain.

    ``paths`` empty means the whole workspace, the meaning the toolbox already
    gives an empty scope -- minus the unconditional floor, which no envelope
    can widen either. ``source`` records how it came to say what it says, so a
    finished run can be asked what it was entitled to touch and why.
    """

    paths: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)
    source: str = ""
    # When this grant was made. A run resumed long after it was planned is
    # exercising authority the user gave in a context that may no longer hold --
    # the working tree has moved on, and so may their intent. Empty on an
    # envelope recorded before this field existed, where the run's own
    # ``created_at`` stands in for it.
    granted_at: str = field(default_factory=now_iso)


@dataclass
class Fact:
    """Something an agent established for the run, and what backs it.

    Distinct from ``RunState.facts``, which holds what the *harness* knows -- the
    baseline commit, the planner's restatement. Those have no author, need no
    evidence and cannot be contested. This has all three, because two lenses
    looking at the same code can reach the same key from different directions
    and say different things about it, and that disagreement is worth more than
    either claim on its own.

    ``key`` is normalised (see ``blackboard.normalise_fact_key``); it is what
    makes two agents' claims comparable, which is the whole point of keying them
    at all.
    """

    key: str = ""
    statement: str = ""
    evidence: str = ""
    agent_id: str = ""
    role: str = ""
    ts: str = field(default_factory=now_iso)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    tool_calls: int = 0

    def add(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            seconds=self.seconds + other.seconds,
            tool_calls=self.tool_calls + other.tool_calls,
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# --------------------------------------------------------------------------
# Agents, turns, supervision
# --------------------------------------------------------------------------


@dataclass
class AgentSpec:
    """A briefed agent. Immutable apart from ``status``."""

    id: str = field(default_factory=lambda: new_id("agt"))
    run_id: str = ""
    role: str = ""                 # role id from the role registry
    kind: AgentKind = AgentKind.ANALYSIS
    title: str = ""
    brief: str = ""
    objectives: list[str] = field(default_factory=list)
    scope: Scope = field(default_factory=Scope)
    binding: ModelBinding = field(default_factory=ModelBinding)
    backend: Backend = Backend.HOST
    budget: Budget = field(default_factory=Budget)
    host_agent_type: str | None = None   # e.g. a Claude Code subagent type
    task_id: str | None = None           # set for execution agents
    # Which attempt of ``task_id`` this agent was built for. A remediated task
    # needs a verifier of its own; without this the first attempt's verifier
    # matches forever and the second attempt is never independently checked.
    attempt: int = 0
    # The agent whose authority this one is drawn from, where there is one. A
    # verifier is the case that exists today: it judges an execution agent's
    # work, and may not be handed a wider fence than the agent it is judging.
    # Read only as a ceiling -- an agent is never dispatched because of it.
    parent_agent_id: str = ""
    depends_on: list[str] = field(default_factory=list)
    status: AgentStatus = AgentStatus.PENDING
    created_at: str = field(default_factory=now_iso)
    # How long this agent has been silent. A host-run agent reports through the
    # caller, so a subagent that crashed or was cancelled looks exactly like one
    # still working: these two are the only signals the supervisor has. Both
    # count packets handed out since the agent last answered, and both reset the
    # moment it does.
    unreported_dispatches: int = 0
    unreported_since: str = ""


@dataclass
class Finding:
    """An observation produced during analysis."""

    id: str = field(default_factory=lambda: new_id("fnd"))
    agent_id: str = ""
    lens: str = ""
    severity: Severity = Severity.MEDIUM
    title: str = ""
    detail: str = ""
    evidence: list[str] = field(default_factory=list)
    recommendation: str = ""
    confidence: float = 0.6
    tags: list[str] = field(default_factory=list)


@dataclass
class Message:
    """One agent-to-agent (or agent-to-supervisor) message."""

    id: str = field(default_factory=lambda: new_id("msg"))
    run_id: str = ""
    sender: str = ""
    recipient: str = BROADCAST
    kind: MessageKind = MessageKind.NOTE
    subject: str = ""
    content: str = ""
    refs: list[str] = field(default_factory=list)
    ts: str = field(default_factory=now_iso)
    # Delivery is per recipient: a broadcast is not consumed by whoever happens
    # to be supervised first.
    delivered_to: list[str] = field(default_factory=list)
    supervisor_note: str = ""

    def delivered_for(self, agent_id: str) -> bool:
        return agent_id in self.delivered_to


@dataclass
class AgentTurn:
    """One unit of agent work, as reported back to the supervisor."""

    id: str = field(default_factory=lambda: new_id("trn"))
    run_id: str = ""
    agent_id: str = ""
    seq: int = 0
    reasoning: str = ""
    output: str = ""
    findings: list[Finding] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    claimed_status: AgentStatus = AgentStatus.RUNNING
    self_assessment: str = ""
    blocked_on: str = ""
    # What this turn could not settle. Every analysis brief has asked for these
    # since the contract was written; nothing read them, `AgentTurn` had no
    # field for them, and they were dropped on arrival. They are the companion
    # to an established fact -- what the run knows, and what it does not.
    open_questions: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    ts: str = field(default_factory=now_iso)


@dataclass
class Note:
    """An audit line: why something was skipped, abandoned or fell back.

    Notes carry the reasons nothing else records -- why an agent was given up on,
    why planning fell back to the derived lenses, why an index projection failed.
    They were emitted and then dropped by the fold, so the explanation for a
    failed run existed only in the raw log, and every reader that works from
    ``RunState`` -- ``status``, ``runs``, the MCP tools -- reported the failure
    without it.
    """

    # The id of the event that carried it, so a replayed log folds to the same
    # notes rather than a longer list of them.
    id: str = ""
    text: str = ""
    actor: str = "supervisor"
    ts: str = field(default_factory=now_iso)
    # Whatever the caller attached alongside the text: agent_id, task_id,
    # iteration. Kept because a note naming its subject is worth more than one
    # that does not, and the caller already knew which it was.
    context: dict[str, str] = field(default_factory=dict)


@dataclass
class Directive:
    """The supervisor's instruction back to an agent after a turn."""

    id: str = field(default_factory=lambda: new_id("dir"))
    agent_id: str = ""
    # The turn this directive answers. Empty on a log written before the
    # journal existed, where the association is recoverable only from the order
    # events were appended in -- which is why `core/journal.py` still has that
    # fallback and says so.
    turn_id: str = ""
    kind: DirectiveKind = DirectiveKind.CONTINUE
    rationale: str = ""
    corrections: list[str] = field(default_factory=list)
    focus: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    inbox: list[Message] = field(default_factory=list)
    turns_remaining: int = 0
    issued_by: str = SUPERVISOR
    ts: str = field(default_factory=now_iso)


@dataclass
class DriftSignal:
    """One reason to suspect an agent has left its lane."""

    kind: str = ""          # e.g. "scope_paths", "repetition", "no_progress"
    severity: Severity = Severity.LOW
    detail: str = ""
    score: float = 0.0      # contribution to the overall drift score, 0..1


@dataclass
class DriftAssessment:
    on_task: bool = True
    score: float = 0.0      # 0 = perfectly on brief, 1 = fully adrift
    signals: list[DriftSignal] = field(default_factory=list)
    summary: str = ""
    checked_by: str = "heuristics"
    # The turn that was assessed. ``RunState.drift`` is keyed by agent and so
    # keeps only the newest assessment per agent; every one of them is on the
    # log, and this is what ties one back to the turn it judged.
    turn_id: str = ""
    ts: str = field(default_factory=now_iso)


# --------------------------------------------------------------------------
# Execution tasks and definitions of done
# --------------------------------------------------------------------------


@dataclass
class DoDCriterion:
    """A single, individually verifiable completion condition."""

    id: str = field(default_factory=lambda: new_id("dod"))
    statement: str = ""
    method: VerifyMethod = VerifyMethod.INSPECTION
    command: str = ""          # for COMMAND / TEST
    expect: str = ""           # expected exit code, substring, or file state
    rubric: str = ""           # for REVIEW
    mandatory: bool = True
    status: CriterionStatus = CriterionStatus.UNVERIFIED
    evidence: str = ""
    verified_at: str = ""
    verified_by: str = ""

    @property
    def machine_checkable(self) -> bool:
        return self.method in (VerifyMethod.COMMAND, VerifyMethod.TEST, VerifyMethod.INSPECTION)


@dataclass
class ExecutionTask:
    """Work the user may approve, with a verifiable finish line."""

    id: str = field(default_factory=lambda: new_id("tsk"))
    run_id: str = ""
    title: str = ""
    action: str = ""        # what will concretely be done
    motivation: str = ""    # why it is worth doing
    rationale_refs: list[str] = field(default_factory=list)  # finding ids
    dod: list[DoDCriterion] = field(default_factory=list)
    scope: Scope = field(default_factory=Scope)
    suggested_role: str = ""
    suggested_binding: ModelBinding | None = None
    depends_on: list[str] = field(default_factory=list)
    risk: Severity = Severity.LOW
    effort: str = "medium"   # small | medium | large
    status: TaskStatus = TaskStatus.PROPOSED
    decision: Decision | None = None
    decision_note: str = ""
    assigned_agent_id: str | None = None
    attempts: int = 0
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    @property
    def mandatory_criteria(self) -> list[DoDCriterion]:
        return [c for c in self.dod if c.mandatory]

    def dod_satisfied(self) -> bool:
        """True only when every mandatory criterion is proven or explicitly waived."""
        if not self.mandatory_criteria:
            return False
        return all(
            c.status in (CriterionStatus.PASS, CriterionStatus.WAIVED)
            for c in self.mandatory_criteria
        )

    def unmet_criteria(self) -> list[DoDCriterion]:
        return [
            c for c in self.mandatory_criteria
            if c.status not in (CriterionStatus.PASS, CriterionStatus.WAIVED)
        ]


@dataclass
class TaskDecision:
    """The user's call on one proposed task."""

    task_id: str = ""
    decision: Decision = Decision.APPROVE
    note: str = ""
    modifications: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Checkpoint, lessons, reporting
# --------------------------------------------------------------------------


@dataclass
class Checkpoint:
    """Post-execution gate over quality, scope and completeness."""

    id: str = field(default_factory=lambda: new_id("chk"))
    run_id: str = ""
    iteration: int = 1
    quality: float = 0.0        # 0..1
    scope_fidelity: float = 0.0
    completeness: float = 0.0
    passed: bool = False
    gaps: list[str] = field(default_factory=list)
    remediation: list[str] = field(default_factory=list)
    avoidable_causes: list[str] = field(default_factory=list)
    summary: str = ""
    ts: str = field(default_factory=now_iso)


@dataclass
class Lesson:
    """A durable, reusable correction fed back into future runs."""

    id: str = field(default_factory=lambda: new_id("lsn"))
    run_id: str = ""
    # Where the lesson was learned. The library is shared across every workspace
    # the harness has run in, and a lesson drawn from one project is applied to
    # the next -- which is the point of it, and also a path by which one
    # repository's run shapes the prompts used on another. Recording the origin
    # is what makes that auditable and lets the reader weight local experience
    # above borrowed experience.
    workspace: str = ""
    # The other workspaces the same lesson was learned in. ``workspace`` records
    # where it was learned *first*; without this, a lesson that recurs in three
    # projects still reads as belonging to whichever one got there first, and a
    # reader weighting local experience above borrowed experience weights it
    # wrongly in the other two.
    also_seen_in: list[str] = field(default_factory=list)
    category: LessonCategory = LessonCategory.PROCESS
    trigger: str = ""        # what was observed
    statement: str = ""      # the lesson itself
    why: str = ""
    how_to_apply: str = ""
    target: str = ""         # role id, "supervisor", "dod", or a skill name
    confidence: float = 0.5
    occurrences: int = 1
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def learned_in(self, workspace: str) -> bool:
        """Whether this lesson was learned in ``workspace``, first or since."""
        if not workspace:
            return False
        return workspace == self.workspace or workspace in self.also_seen_in

    def origin_label(self, workspace: str = "") -> str:
        """Where this came from, in the words a brief should use.

        Named by the workspace's own directory, not its full path: a brief is
        model input, and the absolute layout of someone's disk is not something
        to put there.
        """
        if self.learned_in(workspace):
            return "here"
        if not self.workspace:
            return "an earlier run"
        name = self.workspace.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        return name or "another project"


@dataclass
class Artifact:
    """A file a run produced, recorded so a replay can find it again."""

    path: str = ""
    kind: str = ""
    actor: str = "supervisor"
    ts: str = field(default_factory=now_iso)


@dataclass
class Report:
    """The analysis deliverable when a run produces findings rather than work."""

    id: str = field(default_factory=lambda: new_id("rpt"))
    run_id: str = ""
    title: str = ""
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    recommended_mode: RunMode = RunMode.REPORT
    ts: str = field(default_factory=now_iso)


@dataclass
class RunState:
    """Folded projection of the event log: everything needed to resume."""

    id: str = field(default_factory=lambda: new_id("run"))
    prompt: str = ""
    workspace: str = ""
    mode: RunMode = RunMode.AUTO
    backend: Backend = Backend.HOST
    phase: Phase = Phase.CREATED
    host: str = "unknown"
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    # Subagent types the host said it can spawn, captured at start so later
    # phases can still match roles to them without the host re-declaring.
    host_agents: list[dict[str, Any]] = field(default_factory=list)

    # What this run may touch at all. ``None`` means no envelope was ever
    # established -- a run recorded before this existed -- which is not the
    # same fact as an envelope that names the whole workspace, and is kept
    # distinguishable so a resumed old run is not reported as having been
    # granted something nobody granted it.
    envelope: ScopeEnvelope | None = None

    agents: dict[str, AgentSpec] = field(default_factory=dict)
    # The brief each agent was actually given. Persisted because drift is
    # measured against it, and a generic charter scores quite differently.
    briefs: dict[str, str] = field(default_factory=dict)
    shared_context: str = ""
    facts: dict[str, str] = field(default_factory=dict)
    # What the run's agents established, in the order they established it.
    # A list rather than a dict keyed by ``key``: two agents may claim the same
    # key and disagree, and the fold that used to write ``facts[key] = value``
    # would have resolved that by discarding one of them silently.
    established: list[Fact] = field(default_factory=list)
    turn_counts: dict[str, int] = field(default_factory=dict)
    usage: dict[str, Usage] = field(default_factory=dict)
    # The turns themselves, not just how many there were. The fold used to keep
    # the count and discard the body, so everything that needed what an agent
    # actually said -- drift's comparison against the previous turn, the change
    # summary a verifier is given -- re-read the entire log to get it back, once
    # per supervised turn. That made supervision quadratic in the length of the
    # run it was supervising.
    turns: list[AgentTurn] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    tasks: dict[str, ExecutionTask] = field(default_factory=dict)
    # What the harness had to correct or drop when a task was proposed, by task
    # id. Written onto the proposal event and read back only by the approval
    # response, which had to rescan the log to find it.
    task_notes: dict[str, list[str]] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)
    directives: list[Directive] = field(default_factory=list)
    drift: dict[str, DriftAssessment] = field(default_factory=dict)
    checkpoints: list[Checkpoint] = field(default_factory=list)
    lessons: list[Lesson] = field(default_factory=list)
    # Files the run wrote, latest write per path, so replay can recover them
    # without walking the run directory.
    artifacts: list[Artifact] = field(default_factory=list)
    # Event types the fold has no branch for. Kept so a type added later, or
    # misspelled, is visible in the state instead of vanishing.
    unhandled_events: list[str] = field(default_factory=list)
    # Events whose branch exists but whose target does not: an AGENT_STATUS for
    # an agent the log never spawned, a CRITERION_VERIFIED for a task that is
    # not there. Each used to be a bare no-op, so a log that disagreed with
    # itself folded to a state that looked complete.
    orphaned_events: list[str] = field(default_factory=list)
    # Events whose application raised. The fold contains the failure rather than
    # abandoning the replay, so one malformed payload costs its own event
    # instead of the whole run's resumability.
    rejected_events: list[str] = field(default_factory=list)
    # Lines the log reader could not parse at all. Not foldable -- a line that
    # is not an event cannot describe itself -- so this is stamped by the store
    # after reading rather than accumulated by a branch below.
    damaged_lines: int = 0
    # The highest event sequence this state has seen. It is what makes the
    # snapshot answerable to the log: a reader compares it against the log's own
    # tail and can tell a current snapshot from one another process wrote before
    # the events it is missing. Zero on a snapshot written before this existed,
    # which reads as stale exactly once and is then rewritten.
    last_seq: int = 0
    report: Report | None = None
    checkpoint_iteration: int = 0
    error: str = ""

    # -- derivations -------------------------------------------------------

    def agents_of(self, kind: AgentKind) -> list[AgentSpec]:
        return [a for a in self.agents.values() if a.kind == kind]

    def active_agents(self) -> list[AgentSpec]:
        return [a for a in self.agents.values() if a.status in ACTIVE_AGENT_STATUSES]

    def approved_tasks(self) -> list[ExecutionTask]:
        return [
            t for t in self.tasks.values()
            if t.status not in (TaskStatus.PROPOSED, TaskStatus.REJECTED, TaskStatus.DEFERRED)
        ]

    def pending_messages(self, agent_id: str) -> list[Message]:
        return [
            m for m in self.messages
            if not m.delivered_for(agent_id)
            and m.recipient in (agent_id, BROADCAST)
            and m.sender != agent_id
        ]

    def total_usage(self) -> Usage:
        total = Usage()
        for u in self.usage.values():
            total = total.add(u)
        return total
