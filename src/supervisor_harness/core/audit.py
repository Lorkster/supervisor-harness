"""What a finished run actually did, as against what it was allowed to do.

The envelope and the fence answer one question, at issue time: what may this
agent touch. `core/envelope.py` narrows a scope to every ceiling above it and
`core/tools.py` refuses a write outside the result. Both are checks *before*
the fact, and both are complete for the backend the harness drives itself.

Nothing answered the other question. A finished run could not be asked what its
agents *did* -- and in host-delegated mode, which is the default, the fence is
not even the enforcement: the host runs the work under the user's own permission
model, and the harness sees only what comes back. So the run with the least
enforcement is the run with the most need of an audit, and it had none.

## The method, and the trap it avoids

Taken from NVIDIA's red-team scanners, whose one methodological rule is worth
more than any individual check they run: classify **the action and its result**,
never the surface that advertised the capability -- so that advertising cannot
be mistaken for use.

The trap here is sharper than it was there. Every brief this harness writes
*contains the prohibited list*: the shared-tree rule names `git stash`,
`git checkout` and six others, in full, in the text handed to every agent. A
scanner that searched briefs for prohibited commands would report every agent in
every run, at a hundred per cent confidence, for reading its instructions.

So the rule here is: **a brief is never evidence.** Neither is a task's action,
a scope, or a finding's text. Those are things the harness said. What an agent
reported it did, and what a criterion recorded as having been run, are the only
things this module reads.

## What that leaves it able to say, honestly

In host mode the harness sees agents' own accounts, so most of what follows
audits *the record*. That is not nothing -- an agent that reports touching files
outside its scope has said so on the log, and nobody was reading it -- but it is
not the filesystem, and this module does not pretend otherwise.

One scanner does reach ground truth: with a baseline commit and a git
repository, the tree itself says which files changed, and comparing that against
what agents claimed catches the case self-reporting cannot -- a change nobody
owned up to. It is skipped, loudly, where there is no repository to ask.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..models import AgentKind, CriterionStatus, RunState, VerifyMethod
from .dod import VERIFY_EXECUTABLES
from .paths import matches_any, scope_relative
from .tools import GIT_TREE_SUBCOMMANDS, STORE_DIRS, VCS_DIRS

#: How long the tree is given to answer. A scanner that hangs on a repository
#: turns an audit into something nobody runs.
GIT_TIMEOUT_SECONDS = 20


@dataclass
class Observation:
    """One thing the record shows, with what it was read from."""

    scanner: str
    severity: str          # "high" | "medium" | "low"
    summary: str
    evidence: str = ""
    agent_id: str = ""
    task_id: str = ""

    def line(self) -> str:
        who = f" [{self.agent_id or self.task_id}]" if (self.agent_id or self.task_id) else ""
        return f"- **{self.scanner}** ({self.severity}){who}: {self.summary}"


@dataclass
class AuditReport:
    """Everything the scanners found, and what they could not look at."""

    run_id: str = ""
    observations: list[Observation] = field(default_factory=list)
    #: Scanners that could not run, and why. Reported rather than omitted: a
    #: silent scanner and a clean one look identical, and only one of them is
    #: reassuring.
    skipped: dict[str, str] = field(default_factory=dict)
    scanned: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.observations)

    @property
    def by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for observation in self.observations:
            counts[observation.severity] = counts.get(observation.severity, 0) + 1
        return counts

    def summary(self) -> str:
        if not self.observations:
            ran = len(self.scanned)
            skipped = f", {len(self.skipped)} skipped" if self.skipped else ""
            return f"nothing to report from {ran} scanner(s){skipped}"
        counts = self.by_severity
        return ", ".join(f"{counts[s]} {s}" for s in ("high", "medium", "low") if s in counts)

    def to_payload(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "scanned": list(self.scanned),
            "skipped": dict(self.skipped),
            "observations": [
                {
                    "scanner": o.scanner, "severity": o.severity, "summary": o.summary,
                    "evidence": o.evidence, "agent_id": o.agent_id, "task_id": o.task_id,
                }
                for o in self.observations
            ],
        }

    def render(self) -> str:
        lines = [
            f"# Audit of run {self.run_id}",
            "",
            f"**{self.summary()}**",
            "",
            "What an agent *reported* is what this reads. A brief, a scope or a task's",
            "action is something the harness said, and none of it is evidence here --",
            "every brief contains the list of prohibited git commands, so a scanner that",
            "read briefs would report every agent in every run for having been told.",
            "",
        ]
        if self.observations:
            lines.append("## What the record shows")
            lines.append("")
            for observation in self.observations:
                lines.append(observation.line())
                if observation.evidence:
                    lines.append(f"    - evidence: {observation.evidence}")
            lines.append("")
        lines.append("## Scanners")
        lines.append("")
        lines.extend(f"- {name}: ran" for name in self.scanned)
        lines.extend(f"- {name}: **skipped** -- {why}" for name, why in self.skipped.items())
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Scanners
# --------------------------------------------------------------------------


def _floor_violation(path: str) -> bool:
    parts = {p for p in path.replace("\\", "/").split("/") if p}
    return bool(parts & (VCS_DIRS | STORE_DIRS))


def scan_writes_outside_scope(state: RunState) -> list[Observation]:
    """Files an agent reported touching that its own fence did not cover.

    Reads ``files_touched`` from recorded turns, which is the agent's own
    account. In host-delegated mode that account is all there is, and it was
    never being checked against the scope the same agent was handed.
    """
    out: list[Observation] = []
    for turn in state.turns:
        agent = state.agents.get(turn.agent_id)
        if agent is None or not turn.files_touched:
            continue
        scope = agent.scope
        for raw in turn.files_touched:
            path = scope_relative(raw, state.workspace)
            if path is None:
                continue
            if _floor_violation(path):
                out.append(Observation(
                    scanner="writes_outside_scope", severity="high",
                    agent_id=agent.id,
                    summary=f"reported touching {path!r}, which no scope may cover",
                    evidence=f"turn {turn.seq}",
                ))
                continue
            if scope.forbidden_paths and matches_any(path, scope.forbidden_paths):
                out.append(Observation(
                    scanner="writes_outside_scope", severity="high",
                    agent_id=agent.id,
                    summary=f"reported touching {path!r}, explicitly forbidden to it",
                    evidence=f"turn {turn.seq}",
                ))
            elif scope.paths and not matches_any(path, scope.paths):
                out.append(Observation(
                    scanner="writes_outside_scope",
                    # An analysis agent reporting a file it read outside its
                    # paths is ordinary; an execution agent reporting one it
                    # changed is not. The field is the same and the meaning is
                    # not, so the severity carries the difference rather than
                    # the scanner pretending it cannot tell.
                    severity="high" if agent.kind is AgentKind.EXECUTION else "low",
                    agent_id=agent.id,
                    summary=f"reported touching {path!r}, outside its scope "
                            f"({', '.join(scope.paths)})",
                    evidence=f"turn {turn.seq}",
                ))
    return out


def scan_writes_outside_envelope(state: RunState) -> list[Observation]:
    """The same files against the run's own grant, which no scope may exceed.

    Separate from the scope scan because a hit means something different: an
    agent inside its own fence but outside the run's means attenuation did not
    hold, which is a harness failure rather than an agent one.
    """
    envelope = state.envelope
    if envelope is None or not envelope.paths:
        return []
    out: list[Observation] = []
    for turn in state.turns:
        for raw in turn.files_touched:
            path = scope_relative(raw, state.workspace)
            if path is None or matches_any(path, envelope.paths):
                continue
            out.append(Observation(
                scanner="writes_outside_envelope", severity="high",
                agent_id=turn.agent_id,
                summary=f"reported touching {path!r}, outside the run's envelope",
                evidence=f"turn {turn.seq}; envelope from {envelope.source or 'unknown'}",
            ))
    return out


def scan_forbidden_commands(state: RunState) -> list[Observation]:
    """Criteria whose recorded command is one no agent is allowed to run.

    The command field is a *recorded action*: it is what a criterion says was
    run to prove it. That is why this scanner reads criteria and not briefs --
    a brief names every prohibited git subcommand by design.
    """
    out: list[Observation] = []
    for task in state.tasks.values():
        for criterion in task.dod:
            command = criterion.command.strip()
            if not command:
                continue
            try:
                tokens = shlex.split(command)
            except ValueError:
                tokens = command.split()
            if not tokens:
                continue
            executable = Path(tokens[0]).name.lower().removesuffix(".exe")
            if executable == "git":
                subcommand = next(
                    (t for t in tokens[1:] if not t.startswith("-")), ""
                ).lower()
                if subcommand in GIT_TREE_SUBCOMMANDS:
                    out.append(Observation(
                        scanner="forbidden_commands", severity="high", task_id=task.id,
                        summary=f"criterion {criterion.id} runs `git {subcommand}`, which "
                                "acts on the whole shared tree",
                        evidence=command[:200],
                    ))
                continue
            if executable not in VERIFY_EXECUTABLES:
                out.append(Observation(
                    scanner="forbidden_commands", severity="medium", task_id=task.id,
                    summary=f"criterion {criterion.id} runs {executable!r}, which is not a "
                            "project check runner",
                    evidence=command[:200],
                ))
    return out


def scan_unproven_passes(state: RunState) -> list[Observation]:
    """Criteria marked passed whose evidence does not look like what was run.

    A criterion passed with *no* evidence is already recorded as failed, which
    is the strong version of this rule and predates it. This is the weaker
    case the strong one lets through: evidence that exists and does not mention
    the command it is supposed to be the output of -- a prediction of what the
    command would have printed, rather than what it printed.
    """
    out: list[Observation] = []
    for task in state.tasks.values():
        for criterion in task.dod:
            if criterion.status is not CriterionStatus.PASS:
                continue
            if criterion.method not in (VerifyMethod.COMMAND, VerifyMethod.TEST):
                continue
            command, evidence = criterion.command.strip(), criterion.evidence.strip()
            if not command or not evidence:
                continue
            executable = Path(command.split()[0]).name.lower().removesuffix(".exe")
            if executable and executable not in evidence.lower():
                out.append(Observation(
                    scanner="unproven_passes", severity="medium", task_id=task.id,
                    summary=f"criterion {criterion.id} passed, but its evidence never "
                            f"mentions {executable!r}",
                    evidence=f"{command[:80]!r} -> {evidence[:160]!r}",
                ))
    return out


def scan_untraceable_closures(state: RunState) -> list[Observation]:
    """Tasks that closed a finding without touching anything the finding cited.

    A finding's evidence names where the problem is. A task closing it whose
    agents reported touching none of those files has not been shown to have
    fixed it -- the reconciliation would still record the finding as fixed, on
    the strength of the task having been assigned to it.
    """
    from .facts import anchor_from_evidence

    findings = {f.id: f for f in state.findings}
    touched_by_task: dict[str, set[str]] = {}
    for turn in state.turns:
        agent = state.agents.get(turn.agent_id)
        if agent is None or not agent.task_id:
            continue
        bucket = touched_by_task.setdefault(agent.task_id, set())
        for raw in turn.files_touched:
            path = scope_relative(raw, state.workspace)
            if path:
                bucket.add(path)

    out: list[Observation] = []
    for task in state.tasks.values():
        if str(task.status) != "verified" or not task.rationale_refs:
            continue
        touched = touched_by_task.get(task.id, set())
        for finding_id in task.rationale_refs:
            finding = findings.get(finding_id)
            if finding is None:
                continue
            # A finding cites its evidence as several strings, each of which may
            # or may not name a file. Any one of them being touched is enough:
            # a fix that lands in one of the three places a problem was cited
            # has still been shown to have gone somewhere the finding pointed.
            cited = {
                path for path in (anchor_from_evidence(e) for e in finding.evidence) if path
            }
            if not cited:
                continue
            if not cited & touched:
                out.append(Observation(
                    scanner="untraceable_closures", severity="medium", task_id=task.id,
                    summary=f"closed finding {finding_id}, which cites "
                            f"{', '.join(sorted(cited))}, and no turn of this task "
                            "reported touching any of them",
                    evidence=f"touched: {', '.join(sorted(touched)) or '(nothing reported)'}",
                ))
    return out


def scan_unclaimed_changes(
    state: RunState, workspace: Path
) -> tuple[list[Observation], str]:
    """Files the tree shows changed that no agent said it changed.

    The one scanner that reaches past the record. Everything else here audits
    what agents reported, and shares the blind spot that reporting has: an agent
    that changed a file and did not mention it is invisible to all of them.

    Returns its observations and a reason for skipping, one of which is always
    empty. It needs a baseline commit and a git repository, and says so plainly
    when it has neither -- a scanner that quietly does nothing is worse than one
    that is missing, because it reads as a clean result.
    """
    from ..models import BASELINE_FACT
    from .baseline import commit_from_fact

    # The recorded fact is a sentence written for an agent to read; git needs
    # the commit out of it.
    baseline = commit_from_fact(state.facts.get(BASELINE_FACT, ""))
    if not baseline:
        return [], "the run recorded no baseline commit"
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell, no model input
            ["git", "diff", "--name-only", baseline],  # noqa: S607
            cwd=workspace, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"git could not be run: {exc}"
    if result.returncode != 0:
        return [], f"git refused: {result.stderr.strip()[:160] or 'unknown error'}"

    changed = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    claimed: set[str] = set()
    for turn in state.turns:
        for raw in turn.files_touched:
            path = scope_relative(raw, state.workspace)
            if path:
                claimed.add(path)

    unclaimed = sorted(
        path for path in changed
        if path not in claimed and not _floor_violation(path)
    )
    if not unclaimed:
        return [], ""
    return [Observation(
        scanner="unclaimed_changes", severity="high",
        summary=f"{len(unclaimed)} file(s) changed since {baseline[:8]} that no agent "
                "reported touching",
        evidence=", ".join(unclaimed[:20]) + (" ..." if len(unclaimed) > 20 else ""),
    )], ""


# --------------------------------------------------------------------------
# The pass
# --------------------------------------------------------------------------

#: Every scanner that reads only the record, in the order they are reported.
LOG_SCANNERS = (
    scan_writes_outside_scope,
    scan_writes_outside_envelope,
    scan_forbidden_commands,
    scan_unproven_passes,
    scan_untraceable_closures,
)


def audit(state: RunState, workspace: Path | str = "") -> AuditReport:
    """Run every scanner over a finished run and collect what they found.

    Deterministic and read-only. No model is asked anything, and nothing is
    written -- the caller decides where an evidence directory goes.
    """
    report = AuditReport(run_id=state.id)
    for scanner in LOG_SCANNERS:
        name = scanner.__name__.removeprefix("scan_")
        report.scanned.append(name)
        report.observations.extend(scanner(state))

    root = Path(workspace) if workspace else Path(state.workspace or ".")
    observations, skipped = scan_unclaimed_changes(state, root)
    if skipped:
        report.skipped["unclaimed_changes"] = skipped
    else:
        report.scanned.append("unclaimed_changes")
        report.observations.extend(observations)

    severity_order = {"high": 0, "medium": 1, "low": 2}
    report.observations.sort(key=lambda o: (severity_order.get(o.severity, 3), o.scanner))
    return report


__all__ = [
    "GIT_TIMEOUT_SECONDS",
    "LOG_SCANNERS",
    "AuditReport",
    "Observation",
    "audit",
]
