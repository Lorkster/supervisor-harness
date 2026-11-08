"""Short, time-ordered, prefixed identifiers, and the age arithmetic on them.

Lexical sort order tracks creation order (ids minted by one process are strictly
increasing; across processes, ids from the same millisecond tie arbitrarily),
which keeps the JSONL event log and any directory listing chronological without
a separate index.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime, timedelta

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U


def _b32(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


#: The random part of an id is four base32 characters: 20 bits. Drawn fresh for
#: every id that was a collision waiting to happen -- 200,000 ids in a tight loop
#: produced 56 duplicates, and a duplicate silently replaces an entry in
#: ``_upsert``, ``state.agents`` and the index's ``INSERT OR REPLACE``.
_RAND_BITS = 20
_RAND_MASK = (1 << _RAND_BITS) - 1

_clock = threading.Lock()
_last_ms = 0
_last_rand = 0


def new_id(prefix: str) -> str:
    """Return e.g. ``run_01JBQK8T4M7F3A``: 50 bits of ms time, 20 of sequence.

    Ten base32 characters of millisecond time and four of tie-breaker. Within one
    process ids are strictly increasing, which is what makes them unique: the
    tie-breaker starts at a random value in each new millisecond and counts up
    from there, and when a millisecond runs out of room (a million ids in it) the
    clock this uses moves on to the next one rather than wrapping. Across
    processes it is still random, so two ids minted in the same millisecond by
    two processes collide with probability about one in a million.
    """
    global _last_ms, _last_rand
    with _clock:
        now = int(time.time() * 1000)
        if now > _last_ms:
            stamp = now
            rand = int.from_bytes(os.urandom(3), "big") & _RAND_MASK
        elif _last_rand < _RAND_MASK:
            stamp = _last_ms                  # same millisecond, or the clock stepped back
            rand = _last_rand + 1
        else:
            stamp = _last_ms + 1
            rand = int.from_bytes(os.urandom(3), "big") & _RAND_MASK
        _last_ms, _last_rand = stamp, rand
    return f"{prefix}_{_b32(stamp, 10)}{_b32(rand, 4)}"


def now_iso() -> str:
    """UTC timestamp with millisecond precision, always ``Z``-suffixed.

    One reading of the clock. The seconds and the milliseconds were two, and
    across a second's boundary the stamp went back by up to a second -- "43.000"
    after "43.848" -- which failed a test of a renewed grant on CI.
    """
    millis = int(time.time() * 1000)
    return (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(millis // 1000))
            + f".{millis % 1000:03d}Z")


def parse_timestamp(stamp: object) -> datetime | None:
    """An ISO timestamp as an aware UTC-comparable datetime, or ``None``.

    The one parser for the log's stamps. A stamp with no zone is read as UTC
    rather than left naive, so any two results can be subtracted; one that
    cannot be read -- or is not a string at all -- is ``None``, never an
    exception.
    """
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when


def age_days(stamp: str) -> float | None:
    """How long ago ``stamp`` was, in days, or ``None`` if it cannot be read."""
    when = parse_timestamp(stamp)
    if when is None:
        return None
    return (datetime.now(UTC) - when) / timedelta(days=1)


def older_than(stamp: str, max_age_days: int) -> bool:
    """Whether an ISO timestamp is further back than the cap allows.

    An unparseable or absent stamp is treated as *not* expired. For the lessons
    library that is because dropping a record over a formatting problem would
    destroy data the cap exists only to bound. For the run envelope it is
    because refusing to resume a run over an unreadable date punishes the user
    for a bug in this file; the caller there falls back to the run's own
    creation time, so an absent stamp is answered before it reaches here.
    """
    if max_age_days <= 0:
        return False
    age = age_days(stamp)
    return age is not None and age > max_age_days
