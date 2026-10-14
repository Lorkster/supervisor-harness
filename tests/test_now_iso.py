"""A timestamp is one reading of the clock.

Its seconds and its milliseconds were two readings, and across a second's
boundary a stamp went back by up to a second: CI failed a renewed grant whose
date, "07:20:43.000Z", came before the grant it renewed, "07:20:43.848Z".
"""

from __future__ import annotations

import pytest

from supervisor_harness.ids import now_iso


def test_the_seconds_and_the_milliseconds_are_one_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("supervisor_harness.ids.time.time", lambda: 1_000_000.9996)
    assert now_iso() == "1970-01-12T13:46:40.999Z"
