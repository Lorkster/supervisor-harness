"""Append-only JSONL event log.

Agents report in parallel, so appends are serialised through an advisory lock
file. Sequence numbers are assigned under that lock, which makes the log
totally ordered and therefore replayable.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType

from .events import Event, event_from_dict, event_to_dict


class LockTimeout(RuntimeError):
    """Raised when the log lock could not be acquired in time."""


class FileLock:
    """Advisory lock built on exclusive file creation.

    Portable across Windows and POSIX without native extensions. Stale locks
    (from a crashed process) are broken after ``stale_after`` seconds.
    """

    def __init__(self, path: Path, timeout: float = 10.0, stale_after: float = 60.0) -> None:
        self.path = path
        self.timeout = timeout
        self.stale_after = stale_after
        self._fd: int | None = None

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return
            except FileExistsError:
                if self._break_if_stale():
                    continue
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"could not acquire {self.path} within {self.timeout}s") from None
                time.sleep(0.02)

    def _break_if_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return True
        if age > self.stale_after:
            try:
                self.path.unlink()
                return True
            except OSError:
                return False
        return False

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

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

    # -- writing -----------------------------------------------------------

    def append(self, event: Event) -> Event:
        """Assign the next sequence number and durably append one event."""
        with self._lock:
            event.seq = self._next_seq_unlocked()
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(event_to_dict(event), ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return event

    def append_many(self, events: list[Event]) -> list[Event]:
        """Append a batch atomically with respect to other writers."""
        if not events:
            return []
        with self._lock:
            seq = self._next_seq_unlocked()
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                for event in events:
                    event.seq = seq
                    seq += 1
                    handle.write(json.dumps(event_to_dict(event), ensure_ascii=False) + "\n")
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
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return 0
        if size == 0:
            return 0
        window = 4096
        with self.path.open("rb") as handle:
            while window <= max(size, 1) * 2:
                handle.seek(max(0, size - window))
                lines = [ln for ln in handle.read().split(b"\n") if ln.strip()]
                for line in reversed(lines):
                    try:
                        return int(json.loads(line)["seq"])
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                        continue
                if window >= size:
                    break
                window *= 4
        return 0

    # -- reading -----------------------------------------------------------

    def read(self) -> Iterator[Event]:
        """Yield every well-formed event, skipping any torn trailing line."""
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
                    continue

    def read_all(self) -> list[Event]:
        return sorted(self.read(), key=lambda e: e.seq)

    def __len__(self) -> int:
        return sum(1 for _ in self.read())
