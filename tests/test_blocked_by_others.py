"""An implementer is told not to stop for another task's files.

Minesweeper run 17: the page and the engine both stopped because the project's
lint failed on one line of the unit tests' file, which neither could change, and
both went to the owner. A check that fails there is the coordinator's to give to
the file's task (batch O, `tests/test_coordinator.py`), so the implementer is told
not to stop for it.
"""

from __future__ import annotations

from supervisor_harness.core.conversation import IMPLEMENTER_SYSTEM


def test_the_implementer_is_told_not_to_stop_for_it() -> None:
    flat = " ".join(IMPLEMENTER_SYSTEM.split())
    assert ("A project-wide check -- the type check, the lint, the test suite -- failing only "
            "in another task's files is not yours to fix, and not a reason to stop") in flat
