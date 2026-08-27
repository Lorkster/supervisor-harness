"""SQLite projection of the event log.

Purely derived: the index can be deleted and rebuilt from the JSONL logs at any
time. It exists so the improvement loop and the CLI can ask cross-run questions
("which roles drift most", "which DoD methods fail verification") without
replaying every log.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..models import RunState
from .events import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, prompt TEXT, workspace TEXT, mode TEXT, backend TEXT,
    phase TEXT, host TEXT, created_at TEXT, updated_at TEXT, error TEXT,
    findings INTEGER DEFAULT 0, tasks INTEGER DEFAULT 0, tasks_verified INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY, run_id TEXT, role TEXT, kind TEXT, title TEXT,
    backend TEXT, binding TEXT, status TEXT, turns INTEGER DEFAULT 0,
    drift_score REAL DEFAULT 0, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY, run_id TEXT, agent_id TEXT, lens TEXT, severity TEXT,
    title TEXT, confidence REAL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, run_id TEXT, title TEXT, status TEXT, decision TEXT,
    risk TEXT, effort TEXT, attempts INTEGER, role TEXT,
    dod_total INTEGER, dod_passed INTEGER
);
CREATE TABLE IF NOT EXISTS criteria (
    id TEXT PRIMARY KEY, task_id TEXT, run_id TEXT, statement TEXT, method TEXT,
    mandatory INTEGER, status TEXT
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY, run_id TEXT, iteration INTEGER, quality REAL,
    scope_fidelity REAL, completeness REAL, passed INTEGER, summary TEXT
);
CREATE TABLE IF NOT EXISTS lessons (
    id TEXT PRIMARY KEY, run_id TEXT, category TEXT, trigger TEXT, statement TEXT,
    why TEXT, how_to_apply TEXT, target TEXT, confidence REAL, occurrences INTEGER,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, run_id TEXT, sender TEXT, recipient TEXT, kind TEXT,
    subject TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, run_id TEXT, seq INTEGER, type TEXT, actor TEXT, ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_agents_run ON agents(run_id);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks(run_id);
CREATE INDEX IF NOT EXISTS idx_criteria_task ON criteria(task_id);
CREATE INDEX IF NOT EXISTS idx_lessons_target ON lessons(target);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
"""

_RUN_TABLES = ("agents", "findings", "tasks", "criteria", "checkpoints", "lessons", "messages", "events")


class RunIndex:
    """Read/write access to the derived SQLite database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- projection --------------------------------------------------------

    def sync_run(self, state: RunState, events: list[Event] | None = None) -> None:
        """Rewrite every row belonging to this run. Idempotent by construction."""
        cur = self._conn.cursor()
        cur.execute("DELETE FROM runs WHERE id = ?", (state.id,))
        for table in _RUN_TABLES:
            cur.execute(f"DELETE FROM {table} WHERE run_id = ?", (state.id,))  # noqa: S608 - fixed table names

        verified = sum(1 for t in state.tasks.values() if t.dod_satisfied())
        cur.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                state.id, state.prompt, state.workspace, str(state.mode), str(state.backend),
                str(state.phase), state.host, state.created_at, state.updated_at, state.error,
                len(state.findings), len(state.tasks), verified,
            ),
        )

        for agent in state.agents.values():
            usage = state.usage.get(agent.id)
            drift = state.drift.get(agent.id)
            cur.execute(
                "INSERT INTO agents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    agent.id, state.id, agent.role, str(agent.kind), agent.title,
                    str(agent.backend), agent.binding.ref(), str(agent.status),
                    state.turn_counts.get(agent.id, 0), drift.score if drift else 0.0,
                    usage.input_tokens if usage else 0, usage.output_tokens if usage else 0,
                ),
            )

        for finding in state.findings:
            cur.execute(
                "INSERT OR REPLACE INTO findings VALUES (?,?,?,?,?,?,?)",
                (finding.id, state.id, finding.agent_id, finding.lens,
                 str(finding.severity), finding.title, finding.confidence),
            )

        for task in state.tasks.values():
            passed = sum(1 for c in task.dod if str(c.status) == "pass")
            cur.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task.id, state.id, task.title, str(task.status),
                    str(task.decision) if task.decision else "", str(task.risk), task.effort,
                    task.attempts, task.suggested_role, len(task.dod), passed,
                ),
            )
            for crit in task.dod:
                cur.execute(
                    "INSERT OR REPLACE INTO criteria VALUES (?,?,?,?,?,?,?)",
                    (crit.id, task.id, state.id, crit.statement, str(crit.method),
                     int(crit.mandatory), str(crit.status)),
                )

        for cp in state.checkpoints:
            cur.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?,?,?,?)",
                (cp.id, state.id, cp.iteration, cp.quality, cp.scope_fidelity,
                 cp.completeness, int(cp.passed), cp.summary),
            )

        for lesson in state.lessons:
            cur.execute(
                "INSERT OR REPLACE INTO lessons VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (lesson.id, state.id, str(lesson.category), lesson.trigger, lesson.statement,
                 lesson.why, lesson.how_to_apply, lesson.target, lesson.confidence,
                 lesson.occurrences, lesson.created_at),
            )

        for msg in state.messages:
            cur.execute(
                "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?)",
                (msg.id, state.id, msg.sender, msg.recipient, str(msg.kind), msg.subject, msg.ts),
            )

        for event in events or []:
            cur.execute(
                "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?)",
                (event.id, state.id, event.seq, str(event.type), event.actor, event.ts),
            )

        self._conn.commit()

    # -- queries -----------------------------------------------------------

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.query(
            "SELECT id, phase, mode, prompt, created_at, updated_at, findings, tasks, "
            "tasks_verified FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )

    def drift_by_role(self) -> list[dict[str, Any]]:
        """Roles ranked by mean drift, for the improvement loop."""
        return self.query(
            "SELECT role, COUNT(*) AS agents, ROUND(AVG(drift_score), 3) AS mean_drift, "
            "SUM(CASE WHEN status = 'stopped' THEN 1 ELSE 0 END) AS stopped "
            "FROM agents GROUP BY role HAVING agents > 0 ORDER BY mean_drift DESC"
        )

    def criteria_failure_rate(self) -> list[dict[str, Any]]:
        """Which verification methods actually prove things, and which stall."""
        return self.query(
            "SELECT method, COUNT(*) AS total, "
            "SUM(CASE WHEN status = 'pass' THEN 1 ELSE 0 END) AS passed, "
            "SUM(CASE WHEN status = 'fail' THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN status = 'unverified' THEN 1 ELSE 0 END) AS unverified "
            "FROM criteria GROUP BY method ORDER BY total DESC"
        )

    def recurring_lessons(self, min_occurrences: int = 1, limit: int = 50) -> list[dict[str, Any]]:
        return self.query(
            "SELECT target, category, statement, how_to_apply, SUM(occurrences) AS hits, "
            "MAX(confidence) AS confidence FROM lessons GROUP BY target, statement "
            "HAVING hits >= ? ORDER BY hits DESC, confidence DESC LIMIT ?",
            (min_occurrences, limit),
        )
