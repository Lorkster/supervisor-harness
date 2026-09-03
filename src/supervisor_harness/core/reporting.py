"""Turning a run's recorded state into what a caller reads.

The lowest of the layers `core/supervisor.py` was split into, and the one with
the clearest boundary: **nothing here calls back into the supervisor.** It reads
``RunState`` and the store, and returns responses, views and artifacts. That was
measured before the split rather than hoped for -- these methods made zero calls
to anything outside this set, which is what made them safe to lift out whole.

The bodies are the ones that were on ``Supervisor``, moved verbatim. A refactor
whose bodies change is not a refactor; keeping them identical is what lets "no
behaviour change" be checked by diffing rather than asserted.

`status` and `explain` remain on ``Supervisor`` as one-line delegations, because
they are public API that the CLI, the MCP server and the tests all call.
"""

from __future__ import annotations

from typing import Any

from ..config import HarnessConfig
from ..models import (
    CriterionStatus,
    ExecutionTask,
    Phase,
    Usage,
)
from ..serde import to_jsonable
from ..store.events import EventType
from ..store.runstore import RunSession, RunStore
from . import phases
from .blackboard import contested_keys
from .envelope import stale_reason
from .journal import RunJournal, build_journal
from .responses import SupervisorResponse

#: How many of a run's notes ``status`` returns. The whole list is in the state
#: and all of it is in the log; this is what fits in an answer meant to be read.
STATUS_NOTE_LIMIT = 20


def _dod_summary(task: ExecutionTask) -> str:
    """`passed/total` over a task's mandatory criteria, for `status`.

    Lifted out of the dict literal it was written in only because the line no
    longer fits: the expression is unchanged.
    """
    passed = sum(1 for c in task.mandatory_criteria if c.status is CriterionStatus.PASS)
    return f"{passed}/{len(task.mandatory_criteria)}"


class Reporting:
    """Reads a run; writes nothing back to it except artifacts."""

    def __init__(self, config: HarnessConfig, store: RunStore) -> None:
        self.config = config
        self.store = store

    def status(self, run_id: str) -> dict[str, Any]:
        state = self.store.load_state(run_id)
        return {
            "run_id": state.id,
            "phase": str(state.phase),
            "mode": str(state.mode),
            "backend": str(state.backend),
            "prompt": state.prompt,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
            "agents": [
                {
                    "id": a.id,
                    "role": a.role,
                    "kind": str(a.kind),
                    "title": a.title,
                    "status": str(a.status),
                    "turns": state.turn_counts.get(a.id, 0),
                    # How silent it has been: packets handed out since it last
                    # answered. The host can see one of its agents is overdue
                    # before the supervisor's own bound abandons it.
                    "unreported_dispatches": a.unreported_dispatches,
                    "drift": state.drift[a.id].score if a.id in state.drift else None,
                    "model": a.binding.ref(),
                }
                for a in state.agents.values()
            ],
            # What the run itself was entitled to touch. ``None`` where no
            # envelope was ever established, which is a different fact from an
            # envelope naming the whole workspace and is reported as one.
            "envelope": to_jsonable(state.envelope) if state.envelope else None,
            # Why a resume will pause before execution, or None. Derived rather
            # than stored: the grant carries its date and the cap is policy, so
            # a run does not need an event to become stale.
            "envelope_stale": stale_reason(
                state.envelope, state.created_at,
                self.config.policy.envelope_max_age_days,
            ),
            # What the run's agents established for each other, and where two of
            # them keyed a claim the same way and said different things.
            "established": [
                {"key": f.key, "statement": f.statement, "evidence": f.evidence,
                 "by": f.role or f.agent_id}
                for f in state.established
            ],
            "contested_facts": sorted(contested_keys(state.established)),
            "open_questions": sorted({
                q for turn in state.turns for q in turn.open_questions
            }),
            "findings": len(state.findings),
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": str(t.status),
                    "decision": str(t.decision) if t.decision else None,
                    "dod": _dod_summary(t),
                    "satisfied": t.dod_satisfied(),
                }
                for t in state.tasks.values()
            ],
            "checkpoints": [
                {"iteration": c.iteration, "passed": c.passed, "quality": c.quality,
                 "scope_fidelity": c.scope_fidelity, "completeness": c.completeness}
                for c in state.checkpoints
            ],
            "lessons": len(state.lessons),
            "artifacts": [
                {"path": a.path, "kind": a.kind, "actor": a.actor, "ts": a.ts}
                for a in state.artifacts
            ],
            # Types the fold has no branch for. Reported rather than kept to
            # itself: a run replayed by an older build, or one written with a
            # misspelled type, is projecting less than the log holds, and the
            # state is the only place that is visible.
            "unhandled_events": list(state.unhandled_events),
            # The three ways a run can be projecting less than its log holds, or
            # its log less than was written to it. All three used to be silent,
            # and a run missing records read back as a complete, plausible one.
            "orphaned_events": list(state.orphaned_events),
            "rejected_events": list(state.rejected_events),
            "damaged_lines": state.damaged_lines,
            # Why things went the way they did: an agent abandoned, a stage
            # fallen back, an index projection failed. The fold used to drop
            # these, so a failed run reported its failure without the sentence
            # explaining it, and only `supervisor events --type note` could say.
            # Newest last, and capped -- a long run's early notes are audit
            # material for the log, not context for a status read.
            "notes": [
                {"text": n.text, "actor": n.actor, "ts": n.ts, **n.context}
                for n in state.notes[-STATUS_NOTE_LIMIT:]
            ],
            "note_count": len(state.notes),
            "usage": to_jsonable(state.total_usage()),
            "error": state.error,
        }
    def explain(self, run_id: str, agent_id: str = "") -> RunJournal:
        """Why each directive was issued to each agent, assembled from the log.

        Deliberately not from the snapshot. ``RunState.drift`` is keyed by agent
        id, so the fold keeps only each agent's newest assessment and overwrites
        the rest -- and an assessment that has been overwritten cannot explain
        the directive it produced. ``status`` answers where a run is now from
        the snapshot; this answers how it got there, from the record.
        """
        state = self.store.load_state(run_id)
        return build_journal(state, self.store.log(run_id).read_all(), agent_id)
    @staticmethod
    def _task_view(task: ExecutionTask) -> dict[str, Any]:
        return {
            "id": task.id,
            "title": task.title,
            "action": task.action,
            "motivation": task.motivation,
            "closes_findings": task.rationale_refs,
            "risk": str(task.risk),
            "effort": task.effort,
            "suggested_role": task.suggested_role,
            "depends_on": task.depends_on,
            "scope": to_jsonable(task.scope),
            "definition_of_done": [
                {
                    "id": c.id,
                    "statement": c.statement,
                    "method": str(c.method),
                    "command": c.command,
                    "expect": c.expect,
                    "rubric": c.rubric,
                    "mandatory": c.mandatory,
                }
                for c in task.dod
            ],
        }
    def _final_response(self, session: RunSession) -> SupervisorResponse:
        state = session.state
        markdown = phases.final_report_markdown(state)
        satisfied = [t for t in state.tasks.values() if t.dod_satisfied()]
        executed = state.approved_tasks()
        return SupervisorResponse(
            run_id=state.id,
            phase=str(state.phase),
            action="failed" if state.phase is Phase.FAILED else "complete",
            message=(
                f"Run {state.phase.value}. "
                f"{len(satisfied)}/{len(executed)} task(s) meet their definition of done."
                if executed
                else f"Run {state.phase.value} with an analysis report."
            ),
            report_markdown=markdown,
            checkpoint=to_jsonable(state.checkpoints[-1]) if state.checkpoints else None,
            detail={
                "artifact": str(self.store.run_dir(state.id) / "artifacts" / "report.md"),
                "reconciliation": str(
                    self.store.run_dir(state.id) / "artifacts" / "reconciliation.md"
                ),
                "findings": len(state.findings),
                "findings_open": [
                    r.finding.id
                    for r in phases.reconcile_findings(state)
                    if r.state != phases.FINDING_FIXED
                ],
                "lessons": len(state.lessons),
                "dod_satisfied": [t.id for t in satisfied],
                "dod_unmet": {
                    t.id: [c.statement for c in t.unmet_criteria()]
                    for t in executed if not t.dod_satisfied()
                },
            },
        )
    def _write_run_artifacts(self, session: RunSession) -> None:
        """Write the run's two documents: what happened, and what it closed.

        The reconciliation is written alongside the report rather than derived
        on request, because the question it answers -- which findings did this
        run actually fix, and which are still open? -- is asked after the run is
        over, and was previously reconstructed by hand from the report.
        """
        state = session.state
        path = self.store.write_artifact(
            state.id, "report.md", phases.final_report_markdown(state)
        )
        session.emit(EventType.ARTIFACT_WRITTEN, {"path": str(path), "kind": "report"})

        reconciliation = self.store.write_artifact(
            state.id, "reconciliation.md", phases.reconciliation_markdown(state)
        )
        session.emit(
            EventType.ARTIFACT_WRITTEN,
            {"path": str(reconciliation), "kind": "reconciliation"},
        )
    def _complete(self, session: RunSession) -> SupervisorResponse:
        self._write_run_artifacts(session)
        session.emit(EventType.RUN_ENDED, {"phase": str(Phase.COMPLETE)})
        session.sync_index()
        return self._final_response(session)
    def _error(self, session: RunSession, message: str) -> SupervisorResponse:
        session.emit(EventType.RUN_ENDED, {"phase": str(Phase.FAILED), "error": message})
        session.sync_index()
        return SupervisorResponse(
            run_id=session.state.id, phase=str(Phase.FAILED), action="failed", message=message
        )
    @staticmethod
    def _usage_from(payload: dict[str, Any]) -> Usage:
        raw = payload.get("usage")
        if isinstance(raw, dict):
            return Usage(
                input_tokens=int(raw.get("input_tokens", 0) or 0),
                output_tokens=int(raw.get("output_tokens", 0) or 0),
                seconds=float(raw.get("seconds", 0) or 0),
                tool_calls=int(raw.get("tool_calls", 0) or 0),
            )
        return Usage()
