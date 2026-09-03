"""Store-level guarantees: index schema versioning, transactions and tolerance.

The SQLite index is a derived projection, so these tests hold it to the two
promises that follow from that: it never corrupts itself across a schema change,
and it never takes a run down when it fails.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from supervisor_harness.models import Lesson, RunState
from supervisor_harness.store.index import SCHEMA_VERSION, IndexSchemaError, RunIndex
from supervisor_harness.store.runstore import RunStore


def _state(run_id: str) -> RunState:
    return RunState(id=run_id, prompt=f"prompt for {run_id}", workspace="/workspace")


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _committed_run_ids(path: Path) -> list[str]:
    """The runs a *second* connection can see — that is, only committed ones.

    A connection reads back its own uncommitted writes, so asking the index's
    own connection what it wrote proves nothing about whether the write landed.
    """
    conn = sqlite3.connect(str(path), timeout=5.0)
    try:
        return [str(row[0]) for row in conn.execute("SELECT id FROM runs ORDER BY id")]
    finally:
        conn.close()


# -- schema versioning -----------------------------------------------------


def test_fresh_index_stamps_a_non_zero_user_version(tmp_path: Path) -> None:
    """A new file records the schema it was written with."""
    index = RunIndex(tmp_path / "index.sqlite3")
    try:
        found = int(index.query("PRAGMA user_version")[0]["user_version"])
        assert found == SCHEMA_VERSION
        assert found > 0
    finally:
        index.close()


def test_user_version_survives_reopening(tmp_path: Path) -> None:
    """Reopening an up-to-date index leaves its version and rows alone."""
    path = tmp_path / "index.sqlite3"
    index = RunIndex(path)
    index.sync_run(_state("run_A"), [])
    index.close()

    reopened = RunIndex(path)
    try:
        assert _user_version(path) == SCHEMA_VERSION
        assert [r["id"] for r in reopened.list_runs()] == ["run_A"]
    finally:
        reopened.close()


def test_older_schema_version_is_rebuilt_not_raised(tmp_path: Path) -> None:
    """A database from an earlier release is dropped and rebuilt, not patched."""
    path = tmp_path / "index.sqlite3"
    old = sqlite3.connect(str(path))
    old.executescript(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, prompt TEXT);"
        "INSERT INTO runs VALUES ('run_OLD', 'stale');"
        "PRAGMA user_version = 0;"
    )
    old.commit()
    old.close()

    index = RunIndex(path)
    try:
        columns = [row["name"] for row in index.query("PRAGMA table_info(runs)")]
        assert "tasks_verified" in columns, "the stale table should have been rebuilt"
        assert _user_version(path) == SCHEMA_VERSION
        assert index.list_runs() == [], "stale rows do not survive the rebuild"
        index.sync_run(_state("run_A"), [])  # would raise OperationalError before the fix
        assert [r["id"] for r in index.list_runs()] == ["run_A"]
    finally:
        index.close()


def test_newer_schema_version_refuses_to_open(tmp_path: Path) -> None:
    """A file from a future release is refused rather than written through."""
    path = tmp_path / "index.sqlite3"
    index = RunIndex(path)
    index.close()
    ahead = sqlite3.connect(str(path))
    ahead.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    ahead.commit()
    ahead.close()

    with pytest.raises(IndexSchemaError) as exc:
        RunIndex(path)
    assert "reindex" in str(exc.value)


def test_inserts_name_their_columns(tmp_path: Path) -> None:
    """An added column is a survivable error, not a positional one."""
    path = tmp_path / "index.sqlite3"
    index = RunIndex(path)
    try:
        index._conn.execute("ALTER TABLE runs ADD COLUMN future_field TEXT")
        index.sync_run(_state("run_A"), [])
        assert [r["id"] for r in index.list_runs()] == ["run_A"]
    finally:
        index.close()


# -- transactions ----------------------------------------------------------


def test_failed_sync_run_rolls_back_and_leaves_no_open_transaction(tmp_path: Path) -> None:
    """A sync that raises part-way cannot erase another run on the next commit."""
    index = RunIndex(tmp_path / "index.sqlite3")
    try:
        index.sync_run(_state("run_A"), [])
        assert [r["id"] for r in index.list_runs()] == ["run_A"]

        broken = _state("run_A")
        broken.lessons = [Lesson(id="lsn_1", run_id="run_A"), object()]  # type: ignore[list-item]
        with pytest.raises(AttributeError):
            index.sync_run(broken, [])

        assert index._conn.in_transaction is False, "the write transaction must be closed"
        assert [r["id"] for r in index.list_runs()] == ["run_A"], "the DELETE must have rolled back"

        # The next caller's commit must not carry the failed sync's deletions.
        index.sync_run(_state("run_B"), [])
        assert {r["id"] for r in index.list_runs()} == {"run_A", "run_B"}
    finally:
        index.close()


def test_failed_sync_run_does_not_wedge_later_writes(tmp_path: Path) -> None:
    """After a rollback the shared connection is usable, and its write commits.

    The second half is what the Q-C6 audit added. Read back through the same
    connection, this test was a strictly weaker copy of the one above: take the
    transaction away entirely and it still passes, because a connection sees its
    own uncommitted rows. Reading through a second connection sees only what was
    committed, so the recovery write has to have actually landed.
    """
    path = tmp_path / "index.sqlite3"
    index = RunIndex(path)
    try:
        broken = _state("run_A")
        broken.lessons = [object()]  # type: ignore[list-item]
        with pytest.raises(AttributeError):
            index.sync_run(broken, [])
        index.sync_run(_state("run_A"), [])
        assert [r["id"] for r in index.list_runs()] == ["run_A"]
        assert _committed_run_ids(path) == ["run_A"], "the recovery write was not committed"
    finally:
        index.close()


# -- tolerance -------------------------------------------------------------


def test_index_failure_does_not_abort_the_run(tmp_path: Path) -> None:
    """A broken index leaves the projection pending; the run carries on."""
    store = RunStore(tmp_path / ".supervisor")
    session = store.create(_state("run_A"))

    class BrokenIndex:
        def sync_run(self, state: RunState, events: object = None) -> None:
            raise sqlite3.OperationalError("database is locked")

    store.index = lambda: BrokenIndex()  # type: ignore[assignment,method-assign]

    session.sync_index()  # must not raise

    assert session._pending_index is True, "the projection stays pending for reindex"
    notes = [e for e in session.events() if "index projection failed" in str(e.payload)]
    assert notes, "the failure should be recorded on the log"
    assert "database is locked" in notes[0].payload["text"]

    session.note("the run continues")
    assert session.state.id == "run_A"


def test_repeated_index_failure_notes_once(tmp_path: Path) -> None:
    """The same index failure is not re-noted at every phase edge."""
    store = RunStore(tmp_path / ".supervisor")
    session = store.create(_state("run_A"))

    class BrokenIndex:
        def sync_run(self, state: RunState, events: object = None) -> None:
            raise sqlite3.OperationalError("database is locked")

    store.index = lambda: BrokenIndex()  # type: ignore[assignment,method-assign]
    session.sync_index()
    session.sync_index()

    notes = [e for e in session.events() if "index projection failed" in str(e.payload)]
    assert len(notes) == 1


# -- retention and convergence ---------------------------------------------


def test_sync_run_cannot_silently_wipe_projected_events(tmp_path: Path) -> None:
    """The wipe is unexpressible now, rather than merely unexercised.

    ``events`` defaulted to ``None`` while the deletion ran over every table
    including ``events``, so ``sync_run(state)`` dropped the run's projected
    event rows. Both callers passed events, so it never fired -- and a signature
    that cannot express the mistake is worth more than a comment asking callers
    not to make it.
    """
    import inspect

    from supervisor_harness.store.events import Event, EventType

    signature = inspect.signature(RunIndex.sync_run)
    assert signature.parameters["events"].default is inspect.Parameter.empty

    index = RunIndex(tmp_path / "index.sqlite3")
    try:
        events = [
            Event(seq=1, run_id="run_A", type=EventType.NOTE),
            Event(seq=2, run_id="run_A", type=EventType.NOTE),
        ]
        index.sync_run(_state("run_A"), events)
        assert len(index.query("SELECT id FROM events WHERE run_id = 'run_A'")) == 2
    finally:
        index.close()


def test_deleting_a_run_removes_it_from_disk_and_from_the_index(tmp_path: Path) -> None:
    """There was no way to remove a run at all.

    The store held every prompt, every absolute path and every agent's full
    output for the life of the machine, and deleting the directory by hand left
    the prompt and workspace in ``index.sqlite3`` with nothing to clean them out.
    """
    store = RunStore(tmp_path / ".supervisor")
    store.create(_state("run_A"))
    store.create(_state("run_B"))
    store.reindex()
    assert {r["id"] for r in store.index().list_runs()} == {"run_A", "run_B"}

    assert store.delete_run("run_A") is True
    assert store.delete_run("run_A") is False, "deleting twice is not an error"

    assert not (store.runs_dir / "run_A").exists()
    assert {r["id"] for r in store.index().list_runs()} == {"run_B"}
    assert "prompt for run_A" not in str(store.index().query("SELECT prompt FROM runs"))


def test_reindex_converges_after_a_run_is_removed_by_hand(tmp_path: Path) -> None:
    """Running it twice used to leave the same stale rows both times."""
    store = RunStore(tmp_path / ".supervisor")
    store.create(_state("run_A"))
    store.create(_state("run_B"))
    store.reindex()

    import shutil

    shutil.rmtree(store.runs_dir / "run_A")

    store.reindex()
    first = {r["id"] for r in store.index().list_runs()}
    store.reindex()
    second = {r["id"] for r in store.index().list_runs()}

    assert first == second == {"run_B"}


def _age(store: RunStore, run_id: str, stamp: str) -> None:
    """Backdate a run's snapshot, which is what ``purge`` reads to age it."""
    snapshot = store.runs_dir / run_id / "state.json"
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    data["updated_at"] = stamp
    snapshot.write_text(json.dumps(data), encoding="utf-8")


def test_purge_removes_only_the_runs_past_the_cutoff(tmp_path: Path) -> None:
    """``supervisor delete --older-than`` had no test on either side.

    Found beside the Q-C6 audit rather than by it, and worth the same weight:
    `purge` deletes runs, it is reachable from the CLI, and neither it nor the
    command that calls it was covered by anything at all. A retention policy is
    the last place to learn that from a user.
    """
    store = RunStore(tmp_path / ".supervisor")
    for run_id in ("run_A", "run_B", "run_C"):
        store.create(_state(run_id))
    _age(store, "run_A", "2020-01-01T00:00:00Z")
    _age(store, "run_B", "2020-01-01T00:00:00Z")

    assert store.purge(older_than_days=180) == ["run_B", "run_A"], "newest first"
    assert store.list_run_ids() == ["run_C"]
    assert not (store.runs_dir / "run_A").exists()


def test_purge_keeps_the_most_recent_runs_however_old_they_are(tmp_path: Path) -> None:
    """``keep_last`` is a floor: a retention policy that can empty the store is not one."""
    store = RunStore(tmp_path / ".supervisor")
    for run_id in ("run_A", "run_B", "run_C"):
        store.create(_state(run_id))
        _age(store, run_id, "2020-01-01T00:00:00Z")

    assert store.purge(older_than_days=180, keep_last=2) == ["run_A"]
    assert store.list_run_ids() == ["run_C", "run_B"]

    # And a cutoff of zero or less deletes nothing, however old the runs are.
    assert store.purge(older_than_days=0) == []
    assert store.purge(older_than_days=-1) == []
    assert store.list_run_ids() == ["run_C", "run_B"]


def test_purge_ages_a_run_with_no_readable_snapshot_by_its_log(tmp_path: Path) -> None:
    """An unreadable snapshot must make a run neither immortal nor expired.

    The stamp comes from ``state.json``; when that cannot be read the log's
    mtime stands in, so such a run is still aged by something real rather than
    skipped forever or deleted on a blank date.
    """
    store = RunStore(tmp_path / ".supervisor")
    for run_id in ("run_A", "run_B"):
        store.create(_state(run_id))
    (store.runs_dir / "run_A" / "state.json").write_text("{ not json", encoding="utf-8")
    (store.runs_dir / "run_B" / "state.json").unlink()

    assert store.purge(older_than_days=180) == [], "a fresh log is not expired"

    old = (datetime.now(UTC) - timedelta(days=400)).timestamp()
    for run_id in ("run_A", "run_B"):
        os.utime(store.runs_dir / run_id / "events.jsonl", (old, old))

    assert store.purge(older_than_days=180) == ["run_B", "run_A"]
    assert store.list_run_ids() == []


def test_a_run_is_still_deleted_when_its_index_rows_cannot_be(tmp_path: Path) -> None:
    """The log goes first, and a derived row left behind is recorded, not raised.

    The order is the point: dying between the two leaves a stale index row,
    which ``reindex`` removes — where the other order would leave a run the
    index has forgotten and the disk still holds, which is harder to notice.
    """
    store = RunStore(tmp_path / ".supervisor")
    store.create(_state("run_A"))

    class BrokenIndex:
        def delete_run(self, run_id: str) -> None:
            raise sqlite3.OperationalError("database is locked")

    store.index = lambda: BrokenIndex()  # type: ignore[assignment,method-assign]

    assert store.delete_run("run_A") is True
    assert not (store.runs_dir / "run_A").exists()
    assert store.snapshot_error is not None
    assert "run_A" in store.snapshot_error
    assert "database is locked" in store.snapshot_error


def test_a_lesson_keeps_the_run_that_learned_it(tmp_path: Path) -> None:
    """The row was stamped with whichever run happened to sync it last.

    ``add_lesson`` returns the earlier object unchanged when a lesson repeats, so
    the same lesson is carried by several runs' states -- and stamping the
    projecting run's id made ``reindex`` produce a different answer depending on
    the order it happened to run in.
    """
    index = RunIndex(tmp_path / "index.sqlite3")
    try:
        learned = Lesson(id="lsn_1", run_id="run_A", statement="fence the scope", target="*")
        first = _state("run_A")
        first.lessons = [learned]
        index.sync_run(first, [])

        later = _state("run_B")
        later.lessons = [learned]          # the same lesson, carried into a later run
        index.sync_run(later, [])

        rows = index.query("SELECT run_id FROM lessons WHERE id = 'lsn_1'")
        assert [r["run_id"] for r in rows] == ["run_A"]
    finally:
        index.close()


# -- the shared lessons library --------------------------------------------


def test_concurrent_lesson_writers_do_not_lose_each_others_work(tmp_path: Path) -> None:
    """A read-modify-rewrite of one file, and runs are routinely concurrent.

    Both writers read the library, both wrote their own view of it, and the
    second rename discarded whatever the first had added. ``eventlog`` already
    had the advisory lock this needed.
    """
    import threading

    store = RunStore(tmp_path / ".supervisor")
    errors: list[BaseException] = []
    start = threading.Barrier(6)

    def learn(n: int) -> None:
        try:
            start.wait(timeout=5.0)
            for i in range(10):
                store.add_lesson(
                    Lesson(run_id=f"run_{n}", statement=f"lesson {n}-{i}", target="*")
                )
        except BaseException as exc:  # noqa: BLE001 - re-raised through the assertion
            errors.append(exc)

    threads = [threading.Thread(target=learn, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(store.lessons()) == 60, "a concurrent writer's lessons were lost"


def test_a_repeating_lesson_cannot_dominate_every_later_brief(tmp_path: Path) -> None:
    """``occurrences`` is the primary sort key, and it was unbounded."""
    store = RunStore(tmp_path / ".supervisor")
    for _ in range(50):
        store.add_lesson(Lesson(run_id="run_A", statement="the same lesson", target="*"))

    lessons = store.lessons()
    assert len(lessons) == 1
    assert lessons[0].occurrences <= 20


def test_an_expired_lesson_is_neither_applied_nor_kept(tmp_path: Path) -> None:
    """The library is cross-workspace and nothing ever removed a row from it."""
    store = RunStore(tmp_path / ".supervisor")
    store.add_lesson(Lesson(run_id="run_A", statement="recent", target="*"))
    store.add_lesson(
        Lesson(run_id="run_B", statement="ancient", target="*",
               created_at="2020-01-01T00:00:00Z", updated_at="2020-01-01T00:00:00Z")
    )

    applied = [le.statement for le in store.lessons_for(["*"], max_age_days=180)]
    assert applied == ["recent"]

    assert store.prune_lessons(max_age_days=180) == 1
    assert [le.statement for le in store.lessons()] == ["recent"]


def test_a_lesson_learned_here_outranks_one_borrowed_from_elsewhere(tmp_path: Path) -> None:
    """The library stays shared; local experience leads and borrowed fills in."""
    store = RunStore(tmp_path / ".supervisor")
    store.add_lesson(
        Lesson(run_id="run_A", workspace="/other/project", statement="borrowed", target="*")
    )
    store.add_lesson(
        Lesson(run_id="run_B", workspace="/this/project", statement="local", target="*")
    )

    ranked = [le.statement for le in store.lessons_for(["*"], workspace="/this/project")]
    assert ranked == ["local", "borrowed"], "both apply; the local one leads"


def test_a_lesson_relearned_here_counts_as_local(tmp_path: Path) -> None:
    """The merge used to drop the second origin.

    Two projects learning the same thing independently produced one row, owned
    by whichever recorded it first -- so every other project read its own
    experience back as borrowed, and ranked it below a stranger's.
    """
    store = RunStore(tmp_path / ".supervisor")
    store.add_lesson(
        Lesson(run_id="run_A", workspace="/other/project", statement="shared", target="*")
    )
    store.add_lesson(
        Lesson(run_id="run_B", workspace="/this/project", statement="shared", target="*")
    )

    merged = store.lessons()
    assert len(merged) == 1, "the same statement and target is still one lesson"
    lesson = merged[0]
    assert lesson.occurrences == 2
    assert lesson.workspace == "/other/project", "first origin is still where it began"
    assert lesson.also_seen_in == ["/this/project"]

    # And both projects now read it as their own.
    assert lesson.learned_in("/other/project")
    assert lesson.learned_in("/this/project")
    assert not lesson.learned_in("/a/third/project")


def test_an_origin_is_not_recorded_twice(tmp_path: Path) -> None:
    """A guard: the same workspace relearning a lesson must not accumulate rows."""
    store = RunStore(tmp_path / ".supervisor")
    for _ in range(3):
        store.add_lesson(
            Lesson(run_id="r", workspace="/this/project", statement="repeated", target="*")
        )
    lesson = store.lessons()[0]
    assert lesson.also_seen_in == []
    assert lesson.occurrences == 3


def test_a_relearned_lesson_ranks_as_local_for_the_project_that_relearned_it(
    tmp_path: Path,
) -> None:
    """Ranking has to read the whole origin set, not just the first entry.

    The borrowed lesson is deliberately the stronger one on every other key, so
    only "this project has seen it too" can lift the shared one above it. That
    is the difference between ranking on `workspace ==` and ranking on
    `learned_in`, and nothing else in this file distinguishes them.
    """
    store = RunStore(tmp_path / ".supervisor")
    for _ in range(3):
        store.add_lesson(
            Lesson(run_id="r", workspace="/other/project",
                   statement="borrowed", target="*", confidence=0.9)
        )
    store.add_lesson(
        Lesson(run_id="r", workspace="/other/project", statement="shared", target="*")
    )
    store.add_lesson(
        Lesson(run_id="r", workspace="/this/project", statement="shared", target="*")
    )

    by_statement = {le.statement: le for le in store.lessons()}
    assert by_statement["borrowed"].occurrences == 3
    assert by_statement["shared"].occurrences == 2, "weaker on every key but origin"

    ranked = [le.statement for le in store.lessons_for(["*"], workspace="/this/project")]
    assert ranked == ["shared", "borrowed"]

    # And from a project that has never seen either, the stronger one leads.
    elsewhere = [le.statement for le in store.lessons_for(["*"], workspace="/a/third")]
    assert elsewhere == ["borrowed", "shared"]
