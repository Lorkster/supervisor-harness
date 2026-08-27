"""Run storage: directories, snapshots, the lessons library and resumption.

Layout under the harness home (``.supervisor/`` in the workspace by default)::

    runs/<run_id>/events.jsonl    append-only, authoritative
    runs/<run_id>/state.json      derived snapshot, for fast status reads
    runs/<run_id>/artifacts/      reports and anything an agent produced
    lessons.jsonl                 cross-run lessons library
    index.sqlite3                 derived query index
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..ids import now_iso
from ..models import Lesson, RunState
from ..serde import from_jsonable, to_jsonable
from .events import Event, EventType, _apply, fold
from .eventlog import EventLog
from .index import RunIndex

HOME_ENV = "SUPERVISOR_HOME"
DEFAULT_DIRNAME = ".supervisor"


class RunStore:
    """Filesystem-backed store for every run the harness has executed."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.runs_dir = self.root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.lessons_path = self.root / "lessons.jsonl"
        self._index: RunIndex | None = None

    @classmethod
    def discover(cls, workspace: Path | str | None = None) -> "RunStore":
        """Resolve the harness home from ``SUPERVISOR_HOME`` or the workspace."""
        env = os.environ.get(HOME_ENV)
        if env:
            return cls(Path(env).expanduser())
        base = Path(workspace) if workspace else Path.cwd()
        return cls(base / DEFAULT_DIRNAME)

    # -- run plumbing ------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        path = self.runs_dir / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def log(self, run_id: str) -> EventLog:
        return EventLog(self.run_dir(run_id) / "events.jsonl")

    def exists(self, run_id: str) -> bool:
        return (self.runs_dir / run_id / "events.jsonl").exists()

    def create(self, state: RunState) -> "RunSession":
        """Start a new run and write its genesis event."""
        session = RunSession(self, state)
        session.emit(EventType.RUN_CREATED, {"run": to_jsonable(state)})
        return session

    def open(self, run_id: str) -> "RunSession":
        """Resume an existing run by replaying its log."""
        if not self.exists(run_id):
            raise FileNotFoundError(f"no such run: {run_id}")
        state = fold(self.log(run_id).read_all())
        state.id = run_id
        return RunSession(self, state)

    def list_run_ids(self) -> list[str]:
        if not self.runs_dir.exists():
            return []
        return sorted(
            (p.name for p in self.runs_dir.iterdir() if (p / "events.jsonl").exists()),
            reverse=True,
        )

    def latest_run_id(self) -> str | None:
        ids = self.list_run_ids()
        return ids[0] if ids else None

    def load_state(self, run_id: str) -> RunState:
        """Read the snapshot if present, else rebuild from the log."""
        snapshot = self.runs_dir / run_id / "state.json"
        if snapshot.exists():
            try:
                return from_jsonable(json.loads(snapshot.read_text(encoding="utf-8")), RunState)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        return fold(self.log(run_id).read_all())

    def save_snapshot(self, state: RunState) -> None:
        """Write the derived snapshot atomically so a crash cannot half-write it."""
        path = self.run_dir(state.id) / "state.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(to_jsonable(state), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    # -- artifacts ---------------------------------------------------------

    def write_artifact(self, run_id: str, name: str, content: str) -> Path:
        artifacts = self.run_dir(run_id) / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        path = artifacts / name
        path.write_text(content, encoding="utf-8")
        return path

    # -- lessons library ---------------------------------------------------

    def lessons(self) -> list[Lesson]:
        if not self.lessons_path.exists():
            return []
        out: list[Lesson] = []
        for line in self.lessons_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(from_jsonable(json.loads(line), Lesson))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return out

    def add_lesson(self, lesson: Lesson) -> Lesson:
        """Append a lesson, merging into an identical earlier one when it repeats."""
        existing = self.lessons()
        for prior in existing:
            if prior.statement.strip().lower() == lesson.statement.strip().lower() and prior.target == lesson.target:
                prior.occurrences += 1
                prior.confidence = min(1.0, max(prior.confidence, lesson.confidence) + 0.05)
                prior.updated_at = now_iso()
                self._rewrite_lessons(existing)
                return prior
        with self.lessons_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(to_jsonable(lesson), ensure_ascii=False) + "\n")
        return lesson

    def _rewrite_lessons(self, lessons: list[Lesson]) -> None:
        tmp = self.lessons_path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "".join(json.dumps(to_jsonable(le), ensure_ascii=False) + "\n" for le in lessons),
            encoding="utf-8",
        )
        tmp.replace(self.lessons_path)

    def lessons_for(self, targets: list[str], limit: int = 8) -> list[Lesson]:
        """Highest-signal lessons applicable to the given roles or stages."""
        wanted = {t.lower() for t in targets} | {"*", "supervisor"}
        hits = [le for le in self.lessons() if le.target.lower() in wanted]
        hits.sort(key=lambda le: (le.occurrences, le.confidence), reverse=True)
        return hits[:limit]

    # -- index -------------------------------------------------------------

    def index(self) -> RunIndex:
        if self._index is None:
            self._index = RunIndex(self.root / "index.sqlite3")
        return self._index

    def reindex(self, run_ids: list[str] | None = None) -> int:
        """Rebuild the SQLite projection from the authoritative logs."""
        idx = self.index()
        count = 0
        for run_id in run_ids or self.list_run_ids():
            events = self.log(run_id).read_all()
            state = fold(events)
            state.id = run_id
            idx.sync_run(state, events)
            count += 1
        return count


class RunSession:
    """A live handle on one run: emit events, keep state in step, persist."""

    def __init__(self, store: RunStore, state: RunState) -> None:
        self.store = store
        self.state = state
        self._log = store.log(state.id)
        self._pending_index = False

    # -- emission ----------------------------------------------------------

    def emit(
        self,
        type: EventType,
        payload: dict[str, Any] | None = None,
        actor: str = "supervisor",
    ) -> Event:
        """Append an event, apply it to in-memory state, and snapshot."""
        event = Event(run_id=self.state.id, type=type, actor=actor, payload=payload or {})
        self._log.append(event)
        self.state = _apply(self.state, event)
        self._pending_index = True
        self.store.save_snapshot(self.state)
        return event

    def emit_many(self, events: list[tuple[EventType, dict[str, Any], str]]) -> list[Event]:
        """Append a batch under a single lock acquisition."""
        built = [
            Event(run_id=self.state.id, type=t, actor=actor, payload=payload or {})
            for t, payload, actor in events
        ]
        self._log.append_many(built)
        for event in built:
            self.state = _apply(self.state, event)
        self._pending_index = True
        self.store.save_snapshot(self.state)
        return built

    def note(self, text: str, actor: str = "supervisor", **fields: Any) -> Event:
        return self.emit(EventType.NOTE, {"text": text, **fields}, actor=actor)

    # -- persistence -------------------------------------------------------

    def events(self) -> list[Event]:
        return self._log.read_all()

    def sync_index(self) -> None:
        """Project this run into SQLite. Cheap enough to call at phase edges."""
        self.store.index().sync_run(self.state, self.events())
        self._pending_index = False

    def reload(self) -> RunState:
        """Re-fold from the log; use when another process may have written."""
        self.state = fold(self._log.read_all())
        return self.state
