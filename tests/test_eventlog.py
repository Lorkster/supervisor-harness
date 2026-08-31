"""Event-log locking: ownership, liveness and contended appends.

The log's total ordering rests entirely on the append lock, so these tests hold
it to the three properties that ordering needs: a holder only ever unlinks its
own lock file, a living holder is never mistaken for a crashed one, and a
contended append waits rather than losing the event to a ``LockTimeout``.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from supervisor_harness.store.eventlog import (
    CorruptLog,
    EventLog,
    FileLock,
    LockTimeout,
)
from supervisor_harness.store.events import Event, EventType


def _event(i: int) -> Event:
    return Event(run_id="r", type=EventType.NOTE, payload={"i": i})


def _freeze(lock: FileLock) -> None:
    """Make a holder look crashed: stop refreshing and drop its file handle.

    Windows refuses to unlink a file another process still has open, so the
    stale breaker can only ever act on a holder whose handle is gone. Closing
    the descriptor here reproduces that state without a second process.
    """
    lock._stop_refresh.set()
    if lock._fd is not None:
        os.close(lock._fd)
        lock._fd = None


# -- lock ownership --------------------------------------------------------


def test_release_does_not_unlink_a_lock_another_holder_created(tmp_path: Path) -> None:
    """A-holds / B-breaks-stale / B-acquires / A-releases must leave B's lock."""
    path = tmp_path / "events.lock"
    first = FileLock(path, timeout=1.0, stale_after=0.0)
    first.acquire()
    _freeze(first)

    second = FileLock(path, timeout=1.0, stale_after=0.0)
    second.acquire()  # breaks the stale lock and takes its own
    assert path.read_text(encoding="utf-8").strip() == second._token

    first.release()

    assert path.exists(), "release() deleted a lock file it did not own"
    assert path.read_text(encoding="utf-8").strip() == second._token
    second.release()
    assert not path.exists()


def test_release_unlinks_the_lock_it_owns(tmp_path: Path) -> None:
    path = tmp_path / "events.lock"
    lock = FileLock(path, timeout=1.0)
    lock.acquire()
    assert path.exists()
    lock.release()
    assert not path.exists()


def test_a_live_holder_keeps_the_lock_file_fresh(tmp_path: Path) -> None:
    """The mtime used to be stamped once, so a slow holder aged into staleness."""
    path = tmp_path / "events.lock"
    lock = FileLock(path, timeout=1.0, stale_after=0.06)
    lock.acquire()
    try:
        first = path.stat().st_mtime_ns
        time.sleep(0.25)
        assert path.stat().st_mtime_ns > first
    finally:
        lock.release()


def test_a_refreshed_lock_is_not_broken_as_stale(tmp_path: Path) -> None:
    path = tmp_path / "events.lock"
    holder = FileLock(path, timeout=1.0, stale_after=0.06)
    holder.acquire()
    try:
        time.sleep(0.2)
        contender = FileLock(path, timeout=0.05, stale_after=0.06)
        with pytest.raises(LockTimeout):
            contender.acquire()
        assert path.read_text(encoding="utf-8").strip() == holder._token
    finally:
        holder.release()


def test_a_lock_that_can_never_be_created_times_out_without_spinning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock file that can never be created must time out, not hot spin.

    On Windows a ``PermissionError`` from ``os.open`` is treated as contention,
    so a read-only directory or a denying ACL leaves the lock file absent and
    the create failing forever. ``_break_if_stale`` used to report the missing
    file as a broken stale lock, which skipped both the deadline check and the
    sleep: six figures of ``os.open`` calls and no ``LockTimeout``.
    """
    path = tmp_path / "events.lock"
    lock = FileLock(path, timeout=0.2, stale_after=0.0)

    attempts = 0
    real_open = os.open

    def denying_open(file, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal attempts
        if os.fspath(file) == os.fspath(path):
            attempts += 1
            raise PermissionError(13, "access is denied", str(path))
        return real_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "open", denying_open)

    started = time.monotonic()
    with pytest.raises(LockTimeout):
        lock.acquire()
    elapsed = time.monotonic() - started

    assert not path.exists()
    assert elapsed < 2.0, f"acquire ran {elapsed:.2f}s against a 0.2s timeout"
    # One attempt per poll interval, so roughly ten here. The bound is what
    # matters: the unslept loop reached six figures inside three seconds.
    assert attempts <= 50, f"acquire made {attempts} os.open calls in {elapsed:.2f}s"


# -- contended appends -----------------------------------------------------


def test_contended_append_survives_a_lock_timeout(tmp_path: Path) -> None:
    """An append that loses the first race retries instead of losing the event."""
    log = EventLog(tmp_path / "events.jsonl")
    log._lock.timeout = 0.05
    holder = FileLock(log._lock.path, timeout=2.0)
    holder.acquire()

    releaser = threading.Thread(target=lambda: (time.sleep(0.4), holder.release()))
    releaser.start()
    try:
        event = log.append(_event(0))
    finally:
        releaser.join()

    assert event.seq == 1
    assert [e.seq for e in log.read_all()] == [1]


def test_append_raises_lock_timeout_only_after_its_retries_are_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exhausted retries still surface: a lost event must never pass silently."""
    log = EventLog(tmp_path / "events.jsonl")
    log._lock.timeout = 0.02
    monkeypatch.setattr("supervisor_harness.store.eventlog.time.sleep", lambda _s: None)

    attempts = 0
    real_acquire = log._lock.acquire

    def counting_acquire() -> None:
        nonlocal attempts
        attempts += 1
        real_acquire()

    monkeypatch.setattr(log._lock, "acquire", counting_acquire)

    blocker = FileLock(log._lock.path, timeout=1.0)
    blocker.acquire()
    try:
        with pytest.raises(LockTimeout):
            log.append(_event(0))
    finally:
        blocker.release()

    assert attempts == 4  # the first try plus APPEND_RETRIES
    assert not log.path.exists()


# -- ordering under concurrency --------------------------------------------


def test_sequence_numbers_are_unique_and_contiguous_under_concurrent_appends(
    tmp_path: Path,
) -> None:
    """Every writer opens its own log handle, as separate reporters do."""
    path = tmp_path / "events.jsonl"
    writers, per_writer = 6, 5
    errors: list[BaseException] = []
    start = threading.Barrier(writers)

    def write() -> None:
        log = EventLog(path)
        try:
            start.wait(timeout=5.0)
            for i in range(per_writer):
                log.append(_event(i))
        except BaseException as exc:  # noqa: BLE001 - re-raised through the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=write) for _ in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    seqs = sorted(e.seq for e in EventLog(path).read_all())
    assert seqs == list(range(1, writers * per_writer + 1))
    assert not path.with_suffix(".lock").exists()


def test_an_append_after_a_torn_record_does_not_destroy_the_next_one(
    tmp_path: Path,
) -> None:
    """The torn record is lost. The one written after it must not be.

    A process killed mid-write leaves a line with no newline. The next append
    opened in ``"a"`` and wrote onto the end of it, fusing the fragment and the
    following record into one line that parses as neither -- so a crash cost two
    records instead of one, and nothing said so.
    """
    path = tmp_path / "run_A.jsonl"
    log = EventLog(path)
    log.append(_event(1))

    # A write that died before its newline.
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 2, "run_id": "r", "type": "no')

    log.append(_event(3))

    events = log.read_all()
    assert [e.payload["i"] for e in events] == [1, 3]
    assert log.skipped_lines == 1, "the torn fragment, and only it, was lost"


def test_a_log_that_yields_no_sequence_refuses_the_append(tmp_path: Path) -> None:
    """Numbering must never restart over records that already exist.

    ``_last_seq_unlocked`` returned 0 for a log it could not read at all, so the
    next append took sequence 1 while records numbered 1..n were already on
    disk. ``read_all`` sorts on that number, so the total order every replay
    depends on was gone -- silently, and permanently, because the log is
    append-only.
    """
    path = tmp_path / "run_A.jsonl"
    path.write_text("\n".join("not json at all" for _ in range(300)), encoding="utf-8")

    with pytest.raises(CorruptLog) as excinfo:
        EventLog(path).append(_event(1))

    assert "300 line(s)" in str(excinfo.value)
    assert "not json at all" in path.read_text(encoding="utf-8"), "the log is left alone"


def test_a_blank_log_is_not_treated_as_damaged(tmp_path: Path) -> None:
    """A file of newlines has no history to lose, so it starts from zero."""
    path = tmp_path / "run_A.jsonl"
    path.write_text("\n\n\n", encoding="utf-8")

    assert EventLog(path).append(_event(1)).seq == 1


def test_batched_and_single_appends_share_one_sequence_space(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    log.append(_event(0))
    log.append_many([_event(1), _event(2)])
    log.append(_event(3))
    assert [e.seq for e in log.read_all()] == [1, 2, 3, 4]
