"""The clean-checkout check obeys `policy.allow_command_execution` (review addendum).

With command execution off, the run's own worktree install is skipped, but the
clean check ran `npm ci` and the criterion's command in a fresh checkout anyway.
"""

from __future__ import annotations

import shutil

import pytest

from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import CriterionStatus, TaskStatus

from .test_clean_check import _gated

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


async def test_no_clean_checkout_is_run_when_command_execution_is_off(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, gate, _ = _gated(supervisor, "run_NoExec")
    supervisor.config.policy.allow_command_execution = False
    ran: list[object] = []
    monkeypatch.setattr(
        "supervisor_harness.core.supervisor.check_clean",
        lambda *a, **k: ran.append(a) or "should not run",
    )

    assert await supervisor._check_clean(session) == 0  # type: ignore[arg-type]

    assert not ran, "the clean checkout was built and its commands run"
    assert gate.status is TaskStatus.VERIFIED
    assert all(c.status is CriterionStatus.PASS for c in gate.dod)
    assert any("command execution is off" in n.text
               for n in session.state.notes)  # type: ignore[attr-defined]
