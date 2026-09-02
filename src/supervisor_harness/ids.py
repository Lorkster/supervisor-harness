"""Short, time-ordered, prefixed identifiers, and the age arithmetic on them.

Lexical sort order tracks creation order down to the millisecond (ids minted
within the same millisecond tie arbitrarily), which keeps the JSONL event log
and any directory listing chronological without a separate index.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U


def _b32(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def new_id(prefix: str) -> str:
    """Return e.g. ``run_01JBQK8T4M_7F3A``: 48 bits of ms time, 20 bits of entropy."""
    stamp = _b32(int(time.time() * 1000), 10)
    rand = _b32(int.from_bytes(os.urandom(3), "big"), 4)
    return f"{prefix}_{stamp}{rand}"


def now_iso() -> str:
    """UTC timestamp with millisecond precision, always ``Z``-suffixed."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


def age_days(stamp: str) -> float | None:
    """How long ago ``stamp`` was, in days, or ``None`` if it cannot be read."""
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
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
