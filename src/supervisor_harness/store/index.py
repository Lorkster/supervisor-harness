"""SQLite projection of the event log.

Purely derived: the index can be deleted and rebuilt from the JSONL logs at any
time. It exists so the improvement loop and the CLI can ask cross-run questions
("which roles drift most", "which DoD methods fail verification") without
replaying every log.

The file carries its schema version in ``PRAGMA user_version``. Opening an index
written by an older release drops every derived table and rebuilds it; opening
one written by a newer release raises :class:`IndexSchemaError` rather than
writing rows the newer schema would not recognise.

Recovery, in either direction and whenever the index looks wrong: delete
``index.sqlite3`` together with its ``index.sqlite3-wal`` and
``index.sqlite3-shm`` sidecars, then run ``supervisor reindex`` to rebuild the
projection from the authoritative logs. Nothing is lost -- the logs are the
source of truth and the index holds no state of its own.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..models import RunState
from .events import Event

SCHEMA_VERSION = 1

SCHEMA = f"""
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
PRAGMA user_version = {SCHEMA_VERSION};
"""

_RUN_TABLES = ("agents", "findings", "tasks", "criteria", "checkpoints", "lessons", "messages", "events")
_ALL_TABLES = ("runs", *_RUN_TABLES)


class IndexSchemaError(RuntimeError):
    """The index file was written by a schema version this release cannot use."""


class RunIndex:
    """Read/write access to the derived SQLite database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        """Bring the file to :data:`SCHEMA_VERSION`, or refuse to open it."""
        found = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if found > SCHEMA_VERSION:
            self._conn.close()
            raise IndexSchemaError(
                f"{self.path} was written by schema version {found}; this release "
                f"understands {SCHEMA_VERSION}. Upgrade, or delete the file with its "
                "-wal and -shm sidecars and run `supervisor reindex`."
            )
        if found < SCHEMA_VERSION:
            # The index is derived, so an older file is dropped and rebuilt rather
            # than migrated; `CREATE TABLE IF NOT EXISTS` would leave stale columns.
            with self._conn:
                for table in _ALL_TABLES:
                    self._conn.execute(f"DROP TABLE IF EXISTS {table}")  # noqa: S608 - fixed table names
        with self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # -- projection --------------------------------------------------------

    def sync_run(self, state: RunState, events: list[Event]) -> None:
        """Rewrite every row belonging to this run. Idempotent by construction.

        The whole projection is one transaction: a failure part-way rolls the
        deletions back instead of leaving them pending for the next caller to
        commit.

        ``events`` is required, and that is the fix rather than an aesthetic
        preference. It used to default to ``None`` while the deletion below ran
        unconditionally over every table including ``events`` -- so
        ``sync_run(state)`` silently wiped the run's projected event rows.
        Measured before this changed: two rows after ``sync_run(state, events)``,
        zero after ``sync_run(state)``. Both callers passed events, so it never
        fired; a signature that cannot express the mistake is worth more than a
        comment asking callers not to make it.
        """
        with self._conn:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM runs WHERE id = ?", (state.id,))
            for table in _RUN_TABLES:
                cur.execute(f"DELETE FROM {table} WHERE run_id = ?", (state.id,))  # noqa: S608 - fixed table names

            verified = sum(1 for t in state.tasks.values() if t.dod_satisfied())
            cur.execute(
                "INSERT INTO runs (id, prompt, workspace, mode, backend, phase, host, "
                "created_at, updated_at, error, findings, tasks, tasks_verified) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    "INSERT INTO agents (id, run_id, role, kind, title, backend, binding, "
                    "status, turns, drift_score, input_tokens, output_tokens) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        agent.id, state.id, agent.role, str(agent.kind), agent.title,
                        str(agent.backend), agent.binding.ref(), str(agent.status),
                        state.turn_counts.get(agent.id, 0), drift.score if drift else 0.0,
                        usage.input_tokens if usage else 0, usage.output_tokens if usage else 0,
                    ),
                )

            for finding in state.findings:
                cur.execute(
                    "INSERT OR REPLACE INTO findings (id, run_id, agent_id, lens, severity, "
                    "title, confidence) VALUES (?,?,?,?,?,?,?)",
                    (finding.id, state.id, finding.agent_id, finding.lens,
                     str(finding.severity), finding.title, finding.confidence),
                )

            for task in state.tasks.values():
                passed = sum(1 for c in task.dod if str(c.status) == "pass")
                cur.execute(
                    "INSERT INTO tasks (id, run_id, title, status, decision, risk, effort, "
                    "attempts, role, dod_total, dod_passed) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        task.id, state.id, task.title, str(task.status),
                        str(task.decision) if task.decision else "", str(task.risk), task.effort,
                        task.attempts, task.suggested_role, len(task.dod), passed,
                    ),
                )
                for crit in task.dod:
                    cur.execute(
                        "INSERT OR REPLACE INTO criteria (id, task_id, run_id, statement, "
                        "method, mandatory, status) VALUES (?,?,?,?,?,?,?)",
                        (crit.id, task.id, state.id, crit.statement, str(crit.method),
                         int(crit.mandatory), str(crit.status)),
                    )

            for cp in state.checkpoints:
                cur.execute(
                    "INSERT OR REPLACE INTO checkpoints (id, run_id, iteration, quality, "
                    "scope_fidelity, completeness, passed, summary) VALUES (?,?,?,?,?,?,?,?)",
                    (cp.id, state.id, cp.iteration, cp.quality, cp.scope_fidelity,
                     cp.completeness, int(cp.passed), cp.summary),
                )

            for lesson in state.lessons:
                cur.execute(
                    "INSERT OR REPLACE INTO lessons (id, run_id, category, trigger, statement, "
                    "why, how_to_apply, target, confidence, occurrences, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    # The run that *learned* it, not the run being projected.
                    # Stamping state.id made a lesson change owner every time
                    # another run synced it -- `add_lesson` returns the earlier
                    # object unchanged when a lesson repeats, so the same row was
                    # rewritten under whichever run happened to sync last, and
                    # `reindex` produced a different answer depending on order.
                    (lesson.id, lesson.run_id or state.id, str(lesson.category),
                     lesson.trigger, lesson.statement,
                     lesson.why, lesson.how_to_apply, lesson.target, lesson.confidence,
                     lesson.occurrences, lesson.created_at),
                )

            for msg in state.messages:
                cur.execute(
                    "INSERT OR REPLACE INTO messages (id, run_id, sender, recipient, kind, "
                    "subject, ts) VALUES (?,?,?,?,?,?,?)",
                    (msg.id, state.id, msg.sender, msg.recipient, str(msg.kind), msg.subject, msg.ts),
                )

            for event in events or []:
                cur.execute(
                    "INSERT OR REPLACE INTO events (id, run_id, seq, type, actor, ts) "
                    "VALUES (?,?,?,?,?,?)",
                    (event.id, state.id, event.seq, str(event.type), event.actor, event.ts),
                )

    # -- retention ---------------------------------------------------------

    def delete_run(self, run_id: str) -> None:
        """Remove every row belonging to one run."""
        with self._conn:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            for table in _RUN_TABLES:
                cur.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))

    def prune(self, live_run_ids: list[str]) -> list[str]:
        """Drop rows for runs that no longer exist. Returns the ids removed.

        Without this ``reindex`` did not converge: it iterated the runs that are
        still there and rewrote them, and nothing ever looked at the rows for
        runs that are not. A deleted run's prompt and the user's absolute
        workspace path stayed in the index for the life of the file.
        """
        keep = set(live_run_ids)
        rows = self._conn.execute("SELECT id FROM runs").fetchall()
        stale = [str(r["id"]) for r in rows if str(r["id"]) not in keep]
        for run_id in stale:
            self.delete_run(run_id)
        return stale

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
