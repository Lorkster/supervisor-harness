"""Append-only JSONL event log.

Agents report in parallel, so appends are serialised through an advisory lock
file. Sequence numbers are assigned under that lock, which makes the log
totally ordered and therefore replayable.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

from .events import Event, event_from_dict, event_to_dict

#: How many extra attempts an append makes after a contended lock times out,
#: and the pause before the first of them; the pause doubles each time.
APPEND_RETRIES = 3
APPEND_RETRY_BACKOFF = 0.25

#: How long a contended acquire pauses between attempts. Every retry path
#: sleeps for this long, so a lock that cannot be created for any reason costs
#: a bounded number of attempts rather than spinning on the file system.
LOCK_POLL_INTERVAL = 0.02

#: How many stale locks one acquire will break before giving up. A lock file
#: that keeps being found stale is contention or a broken directory, not a
#: single crashed holder, and breaking it again will not help.
MAX_STALE_BREAKS = 8

#: How long past staleness a lock file carrying no ownership token is left
#: alone. A holder writes its token immediately after creating the file, so
#: only one that died in between is still blank once this has elapsed.
BLANK_TOKEN_GRACE = 1.0


class LockTimeout(RuntimeError):
    """Raised when the log lock could not be acquired in time."""


class CorruptLog(RuntimeError):
    """Raised when a non-empty log yields no sequence number at all.

    Appending to such a file would restart sequencing at 1 and silently destroy
    the total order every replay depends on, which is worse than refusing: the
    run stops with the path named, and the log on disk is left exactly as found
    for whoever has to look at it.
    """


class FileLock:
    """Advisory lock built on exclusive file creation.

    Portable across Windows and POSIX without native extensions. The holder
    writes an ownership token into the file and only ever unlinks a file still
    carrying that token, so breaking a stale lock can never leave one writer
    deleting another's lock. While the lock is held its mtime is refreshed in
    the background, so a slow but living holder is not mistaken for a crashed
    one; only a holder that has genuinely stopped refreshing is broken after
    ``stale_after`` seconds.
    """

    def __init__(self, path: Path, timeout: float = 10.0, stale_after: float = 60.0) -> None:
        self.path = path
        self.timeout = timeout
        self.stale_after = stale_after
        self._fd: int | None = None
        self._token: str | None = None
        self._stop_refresh = threading.Event()
        self._refresher: threading.Thread | None = None

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout
        breaks = 0
        while True:
            token = f"{os.getpid()}:{secrets.token_hex(8)}"
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileExistsError, PermissionError) as exc:
                # Windows reports a lock file that another writer still has
                # open, or one whose deletion is pending, as a permission
                # error rather than as an existing file; both mean contended.
                if isinstance(exc, PermissionError) and os.name != "nt":
                    raise
                if self._break_if_stale():
                    breaks += 1
                    if breaks > MAX_STALE_BREAKS:
                        raise LockTimeout(
                            f"could not acquire {self.path}: broke {breaks - 1} stale locks "
                            "and it was taken again every time"
                        ) from None
                # Every retry path checks the deadline and then sleeps, so a
                # create that keeps failing -- a read-only directory, an ACL
                # denying the file, a lock file that keeps vanishing -- times
                # out rather than spinning on os.open without pause.
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"could not acquire {self.path} within {self.timeout}s") from None
                time.sleep(LOCK_POLL_INTERVAL)
                continue
            self._fd = fd
            self._token = token
            os.write(fd, token.encode())
            self._start_refreshing()
            return

    def _owner_token(self) -> str | None:
        """The token written by whoever currently holds the file, if readable."""
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def _break_if_stale(self) -> bool:
        # Stat before reading: ``stat`` does not block the holder's own unlink,
        # while an open read handle does on Windows, and this runs on every
        # poll of a contended lock.
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            # No lock file at all. Nothing was broken, so this is an ordinary
            # retry: reporting it as a break would skip the caller's deadline
            # check and let a create that fails for some other reason spin.
            return False
        except OSError:
            return False
        if age <= self.stale_after:
            return False
        # Only remove the very file that was found stale: a holder that
        # released and re-acquired in the meantime carries a different token.
        holder = self._owner_token()
        if holder is None:
            return False
        if not holder:
            # Created but not yet stamped -- the microseconds between os.open
            # and os.write. Two reads of an empty file agree, so the token
            # comparison cannot see it; wait the file out instead, which only
            # a holder that died in that window ever reaches.
            return age > self.stale_after + BLANK_TOKEN_GRACE and self._unlink_stale()
        if self._owner_token() != holder:
            return False
        return self._unlink_stale()

    def _unlink_stale(self) -> bool:
        try:
            self.path.unlink()
            return True
        except OSError:
            return False

    def _start_refreshing(self) -> None:
        self._stop_refresh = threading.Event()
        self._refresher = threading.Thread(
            target=self._refresh_mtime, name=f"lock-refresh-{self.path.name}", daemon=True
        )
        self._refresher.start()

    def _refresh_mtime(self) -> None:
        """Restamp the lock while it is held so it never ages into staleness."""
        interval = max(self.stale_after / 3.0, 0.005)
        while not self._stop_refresh.wait(interval):
            try:
                os.utime(self.path, None)
            except OSError:
                return

    def release(self) -> None:
        self._stop_refresh.set()
        refresher, self._refresher = self._refresher, None
        if refresher is not None:
            refresher.join(timeout=1.0)
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        token, self._token = self._token, None
        # Never unlink a lock this object no longer owns: a stale-break may
        # already have handed it to another writer that is mid-append.
        if token is None or self._owner_token() != token:
            return
        for _ in range(3):
            try:
                self.path.unlink()
                return
            except FileNotFoundError:
                return
            except OSError:
                # Windows refuses the unlink while another writer is reading
                # the lock; that read is momentary, so give it a moment.
                time.sleep(LOCK_POLL_INTERVAL)
        # Still there: the file is orphaned but no longer being refreshed, so
        # the next contender breaks it as stale rather than waiting forever.

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


class EventLog:
    """One JSONL file per run."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(self.path.with_suffix(".lock"))
        #: Lines the most recent :meth:`read` could not parse. A line that is not
        #: an event cannot describe itself, so it can never reach the fold; this
        #: is the only place its loss is countable. Reset by each read.
        self.skipped_lines = 0

    def _terminate_last_line(self, handle: BinaryIO) -> None:
        """Close an unterminated final record before anything is appended to it.

        A process killed mid-write leaves a line with no newline. The next append
        opened in ``"a"`` and wrote straight onto the end of it, producing one
        line that is the tail of the torn record followed by a whole good one --
        neither of which parses. Measured before this existed: a log holding one
        good record, a torn fragment and a third append read back as **one**
        event. The fragment was expected to be lost; the *following* record was
        not, and nothing said so.

        Terminating the fragment first costs one byte and confines the damage to
        the record that was actually torn.
        """
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            return
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) != b"\n":
            handle.write(b"\n")

    # -- writing -----------------------------------------------------------

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        """Hold the append lock, retrying with backoff when it is contended.

        A contended acquire used to raise ``LockTimeout`` straight out of
        ``append`` and out of every caller above it, losing the event entirely.
        The event is worth more than the wait, so a timeout is retried with a
        widening pause and only propagates once the retries are spent.
        """
        delay = APPEND_RETRY_BACKOFF
        for attempt in range(APPEND_RETRIES + 1):
            try:
                self._lock.acquire()
            except LockTimeout:
                if attempt == APPEND_RETRIES:
                    raise
                time.sleep(delay)
                delay *= 2
                continue
            try:
                yield
            finally:
                self._lock.release()
            return

    @staticmethod
    def _encode(event: Event) -> bytes:
        return (json.dumps(event_to_dict(event), ensure_ascii=False) + "\n").encode("utf-8")

    def append(self, event: Event) -> Event:
        """Assign the next sequence number and durably append one event."""
        with self._write_lock(), self.path.open("ab+") as handle:
            self._terminate_last_line(handle)
            event.seq = self._next_seq_unlocked()
            handle.write(self._encode(event))
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def append_many(self, events: list[Event]) -> list[Event]:
        """Append a batch atomically with respect to other writers."""
        if not events:
            return []
        with self._write_lock(), self.path.open("ab+") as handle:
            self._terminate_last_line(handle)
            seq = self._next_seq_unlocked()
            for event in events:
                event.seq = seq
                seq += 1
                handle.write(self._encode(event))
            handle.flush()
            os.fsync(handle.fileno())
        return events

    def _next_seq_unlocked(self) -> int:
        """Sequence after the last record, read by tailing rather than rescanning.

        Sequences are only ever assigned under the lock and only ever increase,
        so the final line always carries the highest one.
        """
        return self._last_seq_unlocked() + 1

    def _last_seq_unlocked(self) -> int:
        """Highest sequence in the log, found by reading backwards from the end.

        The window starts at the smaller of 4 KiB and the file size, then grows
        until the whole file has been covered. Seeding it at a fixed 4 KiB was a
        bug: the loop guard never held for a file under 2 KiB, so young logs
        reported 0 and every event written before the log grew past that size was
        assigned sequence 1.
        """
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return 0
        if size == 0:
            return 0

        window = min(size, 4096)
        with self.path.open("rb") as handle:
            while True:
                handle.seek(size - window)
                lines = [ln for ln in handle.read(window).split(b"\n") if ln.strip()]
                # Skip the first line unless the window covers the whole file:
                # it is probably truncated by the seek.
                candidates = lines if window >= size else lines[1:]
                for line in reversed(candidates):
                    try:
                        return int(json.loads(line)["seq"])
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                        continue
                if window >= size:
                    # The whole file has been read and nothing in it carries a
                    # sequence. A file of blank lines has no history to lose, so
                    # it starts from zero like an empty one; a file with content
                    # that yields no sequence is damaged, and returning 0 here
                    # restarted numbering at 1 while records numbered 1..n were
                    # already on disk. Two events would then share a sequence,
                    # read_all() sorts on it, and the total order the replay
                    # depends on is gone -- silently, and permanently, because
                    # the log is append-only.
                    if not lines:
                        return 0
                    raise CorruptLog(
                        f"{self.path} holds {len(lines)} line(s) and no readable "
                        "sequence number; refusing to append, because numbering "
                        "would restart at 1 over records that already exist"
                    )
                window = min(size, window * 4)

    # -- reading -----------------------------------------------------------

    def read(self) -> Iterator[Event]:
        """Yield every well-formed event, counting any line that is not one.

        Skipping an unreadable line is right -- there is nothing else to do with
        it -- but doing so silently meant a log could lose records and still read
        back as a complete, plausible run. :attr:`skipped_lines` is what makes
        the loss countable, and the store carries it into ``RunState`` so it
        reaches ``supervisor status`` rather than staying in this object.
        """
        self.skipped_lines = 0
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield event_from_dict(json.loads(line))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    self.skipped_lines += 1
                    continue

    def last_seq(self) -> int | None:
        """The highest sequence on disk, or ``None`` when that cannot be read.

        Read without the append lock, which is safe for the one question it
        answers: the log is append-only, so a concurrent write can only make the
        answer larger, and a caller comparing a snapshot against it wants to know
        whether the snapshot is *behind*. A racing append makes it look staler,
        never fresher, which is the direction that fails safe.

        ``None`` rather than a raise, because the callers are readers: a log too
        damaged to yield a sequence should not stop ``supervisor status`` from
        reporting what it can. :meth:`append` still refuses that log outright,
        since writing to it would restart numbering.
        """
        try:
            return self._last_seq_unlocked()
        except CorruptLog:
            return None

    def read_all(self) -> list[Event]:
        return sorted(self.read(), key=lambda e: e.seq)

    def __len__(self) -> int:
        return sum(1 for _ in self.read())
