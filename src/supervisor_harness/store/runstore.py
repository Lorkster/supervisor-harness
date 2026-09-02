"""Run storage: directories, snapshots, the lessons library and resumption.

Layout under the harness home (``.supervisor/`` in the workspace by default)::

    runs/<run_id>/events.jsonl    append-only, authoritative
    runs/<run_id>/state.json      derived snapshot, for fast status reads
    runs/<run_id>/artifacts/      reports and anything an agent produced
    lessons.jsonl                 cross-run lessons library
    index.sqlite3                 derived query index
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..ids import now_iso
from ..ids import older_than as _older_than
from ..models import Lesson, RunState
from ..serde import from_jsonable, to_jsonable
from .eventlog import EventLog, FileLock
from .events import Event, EventType, _apply_contained, fold
from .index import RunIndex
from .redaction import redact

HOME_ENV = "SUPERVISOR_HOME"
DEFAULT_DIRNAME = ".supervisor"

#: How many times a snapshot rename waits out a reader before giving up, and
#: how long it pauses between attempts. Windows refuses to replace a file another
#: handle has open, and a reader's hold is measured in milliseconds.
SNAPSHOT_REPLACE_RETRIES = 10
SNAPSHOT_REPLACE_BACKOFF = 0.02

#: Bounds on the shared lessons library, used when no policy is passed in. The
#: library is deliberately cross-workspace, so it needs an edge somewhere.
DEFAULT_LESSON_MAX_AGE_DAYS = 180
DEFAULT_LESSON_MAX_OCCURRENCES = 20


def _artifact_name(name: str) -> str:
    """One path component, safe to join onto the artifacts directory.

    Everything but the final component is discarded rather than rejected: a
    caller asking for ``reports/report.md`` means ``report.md``, and a caller
    asking for ``../../escaped.md`` means nothing this method should honour.
    Separators of both kinds are cut, because a Windows path arriving on POSIX
    is one component to ``PurePosixPath`` and two to the filesystem underneath.
    """
    cleaned = str(name).replace("\\", "/").strip().rstrip("/")
    component = cleaned.rsplit("/", 1)[-1].strip()
    if not component or component in (".", ".."):
        raise ValueError(f"not a usable artifact name: {name!r}")
    return component


class RunStore:
    """Filesystem-backed store for every run the harness has executed."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.runs_dir = self.root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._contain()
        self.lessons_path = self.root / "lessons.jsonl"
        self._index: RunIndex | None = None
        #: Why the last snapshot write did not land, or ``None``. Recorded
        #: rather than raised: the snapshot is derived, and the log answers
        #: every question it does.
        self.snapshot_error: str | None = None

    def _contain(self) -> None:
        """Keep the store out of the repository it is sitting inside.

        The default root is ``.supervisor/`` *in the workspace*, and the
        workspace is frequently a repository someone else wrote. The store holds
        the run's prompt, the user's absolute paths and every agent's full
        output, so committing it by accident publishes all three -- and nothing
        stopped that: this repository's own ``.gitignore`` lists ``.supervisor/``,
        but that is this repository, not shipped behaviour.

        A ``.gitignore`` of ``*`` inside the store excludes it from whatever
        repository contains it, without editing a file the user owns. Written
        once and never overwritten, so a user who edits it keeps their version.

        Permissions are narrowed on POSIX for the same reason. Windows has no
        equivalent that ``os.chmod`` can express, so this is one of the places
        the harness is less protective there; saying so is better than implying
        otherwise.
        """
        marker = self.root / ".gitignore"
        try:
            if not marker.exists():
                marker.write_text(
                    "# Written by supervisor-harness. This directory holds run\n"
                    "# prompts, absolute paths and full agent output.\n"
                    "*\n",
                    encoding="utf-8",
                )
            if os.name == "posix":
                os.chmod(self.root, 0o700)
        except OSError:
            # A read-only or unusual store root is the caller's business; the
            # run must not fail over the containment of its own scratch space.
            pass

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
        state = self._fold_log(run_id)
        return RunSession(self, state)

    def _fold_log(self, run_id: str) -> RunState:
        """Replay one run's log, keeping what the read itself could not recover.

        The count of unreadable lines belongs on the state rather than on the
        log object: it is a fact about this run that every reader needs, and the
        log is constructed and discarded per call.
        """
        log = self.log(run_id)
        state = fold(log.read_all())
        state.id = run_id
        state.damaged_lines = log.skipped_lines
        return state

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
        """Read the snapshot if it is current, else rebuild from the log.

        The snapshot is a cache of the fold, and it used to be preferred on the
        strength of parsing: the fold was reached only when ``json.loads`` threw.
        A snapshot that parsed but was *behind* -- written by a process that had
        folded fewer events, which is what concurrent reporters produce -- was
        therefore preferred over a log that was correct, for as long as the file
        sat there. Nothing compared the two, and nothing could: the state
        recorded no position in the log at all.

        :attr:`RunState.last_seq` is that position, so the two are now
        comparable, and the log wins whenever the snapshot is behind it. A
        snapshot written before the field existed carries zero and is read as
        stale exactly once.

        The id is stamped from ``run_id`` the way :meth:`open` does, so a log
        whose genesis record is missing or unreadable still reports the run that
        was asked for rather than the random id the fold started from. Reads of
        a run that does not exist fail closed instead of inventing one.
        """
        if not self.exists(run_id):
            raise FileNotFoundError(f"no such run: {run_id}")
        snapshot = self.runs_dir / run_id / "state.json"
        if snapshot.exists():
            try:
                state = from_jsonable(json.loads(snapshot.read_text(encoding="utf-8")), RunState)
                state.id = run_id
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                # OSError included for the same reason the rename retries: on
                # Windows a read racing a replace is denied outright. The log is
                # always able to answer, so a snapshot that cannot be read right
                # now is simply not used.
                return self._fold_log(run_id)

            tail = self.log(run_id).last_seq()
            # ``None`` means the log yields no sequence at all. The fold of such
            # a log is an empty state, which is strictly worse than a snapshot
            # that at least parsed, so the snapshot stands and its damage is
            # reported by the read that produced it.
            if tail is None or state.last_seq >= tail:
                return state
        return self._fold_log(run_id)

    def save_snapshot(self, state: RunState) -> bool:
        """Write the derived snapshot atomically so a crash cannot half-write it.

        The docstring was true of the rename and of nothing else. The temporary
        file's name was ``state.json.tmp``, derived from the target and therefore
        the *same name for every writer of this run* -- so two processes
        reporting at once wrote into one file and renamed it twice, and the
        survivor was whichever finished last, holding an interleaving of both.
        The name is now unique per write, so concurrent writers cannot meet in
        it, and the rename stays the atomic step it always was.

        Nothing was flushed to the platter either. ``write_text`` returns once
        the data is in the page cache, and the rename could reach disk before the
        contents it was renaming, so a power loss could leave ``state.json``
        present, complete-looking and empty. The file is fsynced before it is
        renamed, which is the ordering that makes the rename mean something.

        A snapshot behind the one already on disk is still written rather than
        refused: it is a cache, and :meth:`load_state` compares it against the
        log before trusting it, so a stale write costs a fold on the next read
        and is corrected by the next event. Refusing it here would mean reading
        the existing file on every append to answer a question the reader already
        answers properly.

        Returns whether the file was written. A failure to write it is not fatal
        and never propagates, for the same reason :meth:`RunSession.sync_index`
        gives about the index: this is derived data, and losing it must not take
        the run down. That is now true rather than merely intended --
        ``load_state`` rebuilds from the log whenever the snapshot is missing,
        unreadable or behind, so the worst a failed write costs is the fold it
        was there to save. The event log, which is not derived, still fails
        loudly; the difference is deliberate.
        """
        return self.write_snapshot(state.id, snapshot_payload(state))

    def write_snapshot(self, run_id: str, payload: str) -> bool:
        """The blocking half of :meth:`save_snapshot`: serialised text to disk.

        Split from the serialisation so the async path can do the two in
        different places -- the text is produced on the event loop, where
        ``RunState`` is only ever touched, and only this is handed to a thread.
        A thread that took the state itself would be reading a structure the
        loop is free to mutate while it works, which is the data race that made
        "just move the emit to a thread" the wrong fix.
        """
        path = self.run_dir(run_id) / "state.json"
        handle, name = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
        tmp = Path(name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            self._replace_snapshot(tmp, path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            self.snapshot_error = str(exc)
            return False
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self.snapshot_error = None
        return True

    @staticmethod
    def _replace_snapshot(tmp: Path, path: Path) -> None:
        """Rename the new snapshot over the old one, waiting out a live reader.

        POSIX replaces a file that is open elsewhere without comment. Windows
        refuses it: a reader holding ``state.json`` -- ``supervisor status``
        against a run that is reporting, which is an ordinary thing to do --
        makes the rename raise ``PermissionError``, and that used to come out of
        ``save_snapshot``, out of ``emit``, and end the run over a *cache write*.

        A reader's hold is short, so the rename is retried briefly. If it still
        will not go, the exception stands: at that point the cause is not a
        passing reader.
        """
        for attempt in range(SNAPSHOT_REPLACE_RETRIES + 1):
            try:
                tmp.replace(path)
                return
            except PermissionError:
                if attempt == SNAPSHOT_REPLACE_RETRIES:
                    raise
                time.sleep(SNAPSHOT_REPLACE_BACKOFF)

    # -- artifacts ---------------------------------------------------------

    def write_artifact(self, run_id: str, name: str, content: str) -> Path:
        """Write one artifact into this run's directory, and only into it.

        The name was joined straight onto the path. Both call sites pass a
        literal today -- ``report.md`` and ``reconciliation.md`` -- so nothing
        exploited it, but the name is the kind of thing a later caller derives
        from a task title or a model's answer, and
        ``write_artifact(run, "../../escaped.md", ...)`` wrote outside the run
        directory. Verified before the fix, which is why this is a check rather
        than a comment saying the callers are careful.
        """
        artifacts = self.run_dir(run_id) / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        path = (artifacts / _artifact_name(name)).resolve()
        # Belt and braces against a name that survives the first check: on a
        # case-insensitive or symlinked path, resolve() is the last word on where
        # the write actually lands.
        if artifacts.resolve() not in path.parents:
            raise ValueError(f"artifact name escapes the run directory: {name!r}")
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

    def add_lesson(
        self, lesson: Lesson, *, max_occurrences: int = DEFAULT_LESSON_MAX_OCCURRENCES
    ) -> Lesson:
        """Append a lesson, merging into an identical earlier one when it repeats.

        Held under the same advisory lock the event log uses, and for the same
        reason. This is a read-modify-rewrite of one file: two runs learning at
        once both read the library, both append their own view of it, and the
        second rename discards whatever the first added. Runs are routinely
        concurrent -- that is what the harness is for -- and ``eventlog`` already
        had the lock this needed.

        ``occurrences`` is capped. It is the primary sort key for what reaches a
        brief, so an unbounded counter lets one early lesson outrank everything
        learned since, permanently.
        """
        with FileLock(self.lessons_path.with_suffix(".jsonl.lock")):
            existing = self.lessons()
            for prior in existing:
                if (
                    prior.statement.strip().lower() == lesson.statement.strip().lower()
                    and prior.target == lesson.target
                ):
                    prior.occurrences = min(max_occurrences, prior.occurrences + 1)
                    prior.confidence = min(1.0, max(prior.confidence, lesson.confidence) + 0.05)
                    prior.updated_at = now_iso()
                    # Keep the second origin. The merge used to drop it, so a
                    # lesson learned independently in three projects was still
                    # attributed to whichever one recorded it first -- and every
                    # other project then read its own experience as borrowed.
                    if lesson.workspace and not prior.learned_in(lesson.workspace):
                        prior.also_seen_in.append(lesson.workspace)
                    self._rewrite_lessons(existing)
                    return prior
            self._rewrite_lessons([*existing, lesson])
        return lesson

    def _rewrite_lessons(self, lessons: list[Lesson]) -> None:
        """Replace the library atomically. Caller holds the lock."""
        tmp = self.lessons_path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "".join(json.dumps(to_jsonable(le), ensure_ascii=False) + "\n" for le in lessons),
            encoding="utf-8",
        )
        tmp.replace(self.lessons_path)

    def prune_lessons(self, *, max_age_days: int = DEFAULT_LESSON_MAX_AGE_DAYS) -> int:
        """Drop lessons past the age cap. Returns how many went.

        The library is shared across every workspace and nothing ever removed a
        row, so it grew without bound -- and every ``add_lesson`` reads the whole
        file, which made the cost of learning quadratic in everything ever
        learned. The age cap is what bounds it.
        """
        if max_age_days <= 0:
            return 0
        with FileLock(self.lessons_path.with_suffix(".jsonl.lock")):
            existing = self.lessons()
            kept = [le for le in existing if not _older_than(le.updated_at or le.created_at,
                                                            max_age_days)]
            if len(kept) != len(existing):
                self._rewrite_lessons(kept)
        return len(existing) - len(kept)

    def lessons_for(
        self,
        targets: list[str],
        limit: int = 8,
        *,
        workspace: str = "",
        max_age_days: int = DEFAULT_LESSON_MAX_AGE_DAYS,
    ) -> list[Lesson]:
        """Highest-signal lessons applicable to the given roles or stages.

        The library stays cross-workspace -- a lesson learned in one project is
        worth having in the next, which is what it is for -- but it is no longer
        unbounded or anonymous. Lessons past the age cap are not applied, and a
        lesson learned *here* outranks one borrowed from another project at the
        same strength, so local experience leads and borrowed experience fills in
        behind it.

        "Here" means learned here at any point, not first: a lesson this
        workspace re-learned is this workspace's experience, whoever recorded it
        first. The brief says which of the two each one is.
        """
        wanted = {t.lower() for t in targets} | {"*", "supervisor"}
        hits = [
            le for le in self.lessons()
            if le.target.lower() in wanted
            and not _older_than(le.updated_at or le.created_at, max_age_days)
        ]
        here = str(workspace or "")
        hits.sort(
            key=lambda le: (le.learned_in(here), le.occurrences, le.confidence),
            reverse=True,
        )
        return hits[:limit]

    # -- index -------------------------------------------------------------

    def index(self) -> RunIndex:
        if self._index is None:
            self._index = RunIndex(self.root / "index.sqlite3")
        return self._index

    def reindex(self, run_ids: list[str] | None = None) -> int:
        """Rebuild the SQLite projection from the authoritative logs.

        A full reindex also prunes: rows for runs whose directory is gone were
        never removed by anything, so ``reindex`` did not converge -- running it
        twice on a store with a deleted run left the same stale rows both times,
        including that run's prompt and the user's absolute workspace path.
        Pruning is skipped for a partial reindex, where the caller has named the
        runs it means and the rest are not evidence of anything.
        """
        idx = self.index()
        live = self.list_run_ids()
        for run_id in run_ids or live:
            events = self.log(run_id).read_all()
            state = fold(events)
            state.id = run_id
            idx.sync_run(state, events)
        if run_ids is None:
            idx.prune(live)
        return len(run_ids or live)

    # -- retention ---------------------------------------------------------

    def delete_run(self, run_id: str) -> bool:
        """Remove one run entirely: its directory and its rows in the index.

        There was no way to do this at all. The store held every prompt, every
        absolute path and every agent's full output for the life of the machine,
        and deleting the directory by hand left the run's prompt and workspace in
        ``index.sqlite3`` with nothing that would ever clean them out.

        The log goes first. If the process dies between the two, what remains is
        a stale index row, which ``reindex`` now removes -- the other order would
        leave a run the index has forgotten but the disk still holds, which is
        the harder state to notice.
        """
        target = self.runs_dir / run_id
        if not target.exists():
            return False
        shutil.rmtree(target, ignore_errors=False)
        try:
            self.index().delete_run(run_id)
        except Exception as exc:  # noqa: BLE001 - the index is derived; reindex repairs it
            self.snapshot_error = f"index rows for {run_id} were not removed: {exc}"
        return True

    def purge(self, *, older_than_days: int, keep_last: int = 0) -> list[str]:
        """Delete runs older than a cutoff, newest first. Returns what went.

        ``keep_last`` is a floor rather than a nicety: a retention policy that
        can empty the store leaves nobody able to answer what the harness has
        been doing, and the most recent runs are the ones a user is most likely
        to still want.
        """
        if older_than_days <= 0:
            return []
        candidates = self.list_run_ids()[max(0, keep_last):]
        gone: list[str] = []
        for run_id in candidates:
            snapshot = self.runs_dir / run_id / "state.json"
            try:
                stamp = json.loads(snapshot.read_text(encoding="utf-8")).get("updated_at", "")
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                stamp = ""
            if not stamp:
                # No readable snapshot: fall back to the log's own mtime rather
                # than treating an unaged run as expired.
                try:
                    mtime = (self.runs_dir / run_id / "events.jsonl").stat().st_mtime
                    stamp = datetime.fromtimestamp(mtime, UTC).isoformat()
                except OSError:
                    continue
            if _older_than(stamp, older_than_days) and self.delete_run(run_id):
                gone.append(run_id)
        return gone



def snapshot_payload(state: RunState) -> str:
    """The snapshot's text, produced wherever ``state`` is safe to read.

    Deliberately not a method on the store: it touches ``RunState`` and nothing
    else, and keeping it separate is what lets a caller serialise on the event
    loop and write from a thread.
    """
    return json.dumps(to_jsonable(state), indent=2, ensure_ascii=False)


class RunSession:
    """A live handle on one run: emit events, keep state in step, persist.

    Emission comes in two forms, and which to use is a property of *when* the
    caller runs rather than of what it is emitting:

    ``emit`` / ``emit_many`` / ``note``
        Synchronous. Correct anywhere nothing else is in flight -- the phase
        machine between phases, the CLI, a host reporting one turn. They block
        on an advisory lock and an ``fsync``, which costs nothing when there is
        nobody to block.

    ``aemit`` / ``aemit_many`` / ``anote``
        Asynchronous, and required on any path reachable while agents are
        running under ``asyncio.gather``. The synchronous form blocks the whole
        event loop for the duration of the lock and the fsync, so every other
        agent's in-flight model call stalls behind one agent's bookkeeping --
        which is what made "parallel" agents serialise.

    The asynchronous form is not simply the synchronous one in a thread. A
    thread that took ``RunState`` with it would read a structure the loop is
    free to mutate while it works, which is the data race that made the obvious
    fix the wrong one. So the split is by *what is touched*: everything that
    reads or writes ``RunState`` stays on the loop, and only the file I/O -- an
    already-encoded event, an already-serialised snapshot -- is handed to a
    thread. An ``asyncio.Lock`` holds the whole sequence together, so two
    coroutines cannot interleave halfway through one emit.
    """

    def __init__(self, store: RunStore, state: RunState) -> None:
        self.store = store
        self.state = state
        self._log = store.log(state.id)
        self._pending_index = False
        self._index_error: str | None = None
        # Created lazily: a session is constructed in synchronous code, and an
        # asyncio.Lock built outside a running loop binds to the wrong one.
        self._alock: asyncio.Lock | None = None

    def _lock(self) -> asyncio.Lock:
        if self._alock is None:
            self._alock = asyncio.Lock()
        return self._alock

    # -- emission ----------------------------------------------------------

    def emit(
        self,
        type: EventType,
        payload: dict[str, Any] | None = None,
        actor: str = "supervisor",
    ) -> Event:
        """Append an event, apply it to in-memory state, and snapshot."""
        event = Event(
            run_id=self.state.id, type=type, actor=actor, payload=redact(payload or {})
        )
        self._log.append(event)
        self.state = _apply_contained(self.state, event)
        self._pending_index = True
        self.store.save_snapshot(self.state)
        return event

    def emit_many(self, events: list[tuple[EventType, dict[str, Any], str]]) -> list[Event]:
        """Append a batch under a single lock acquisition."""
        built = [
            Event(run_id=self.state.id, type=t, actor=actor, payload=redact(payload or {}))
            for t, payload, actor in events
        ]
        self._log.append_many(built)
        for event in built:
            self.state = _apply_contained(self.state, event)
        self._pending_index = True
        self.store.save_snapshot(self.state)
        return built

    def note(self, text: str, actor: str = "supervisor", **fields: Any) -> Event:
        return self.emit(EventType.NOTE, {"text": text, **fields}, actor=actor)

    async def aemit(
        self,
        type: EventType,
        payload: dict[str, Any] | None = None,
        actor: str = "supervisor",
    ) -> Event:
        """``emit``, without blocking the event loop on the lock or the fsync."""
        return (await self.aemit_many([(type, payload or {}, actor)]))[0]

    async def aemit_many(
        self, events: list[tuple[EventType, dict[str, Any], str]]
    ) -> list[Event]:
        """Append a batch off the loop, applying it on the loop.

        The ordering is the point. Under the lock: build the events (loop),
        append them (thread -- the file, and nothing else), fold them into the
        state (loop), serialise the snapshot (loop, so it is a consistent read
        of a structure nothing else can be touching), write it (thread).
        """
        if not events:
            return []
        async with self._lock():
            built = [
                Event(run_id=self.state.id, type=t, actor=actor, payload=redact(payload or {}))
                for t, payload, actor in events
            ]
            await asyncio.to_thread(self._log.append_many, built)
            for event in built:
                self.state = _apply_contained(self.state, event)
            self._pending_index = True
            payload_text = snapshot_payload(self.state)
            run_id = self.state.id
            await asyncio.to_thread(self.store.write_snapshot, run_id, payload_text)
            return built

    async def anote(self, text: str, actor: str = "supervisor", **fields: Any) -> Event:
        return await self.aemit(EventType.NOTE, {"text": text, **fields}, actor=actor)

    # -- persistence -------------------------------------------------------

    def events(self) -> list[Event]:
        return self._log.read_all()

    def sync_index(self) -> None:
        """Project this run into SQLite. Cheap enough to call at phase edges.

        The index is derived, so losing it must never take the run down: a
        failure is noted, the projection stays pending, and ``supervisor
        reindex`` rebuilds it from the log afterwards.
        """
        try:
            self.store.index().sync_run(self.state, self.events())
        except Exception as exc:  # noqa: BLE001 - the index is derived; never fail a run over it
            reason = str(exc)
            if reason != self._index_error:
                self._index_error = reason
                self.note(f"index projection failed, run continues: {reason}")
            self._pending_index = True
            return
        self._index_error = None
        self._pending_index = False

