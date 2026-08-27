"""Short, time-ordered, prefixed identifiers.

Lexical sort order tracks creation order down to the millisecond (ids minted
within the same millisecond tie arbitrarily), which keeps the JSONL event log
and any directory listing chronological without a separate index.
"""

from __future__ import annotations

import os
import time

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
