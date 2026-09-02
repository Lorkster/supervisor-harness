"""A run's grant has a shelf life, and a stale one is re-approved before it writes.

Duration was already bounded nearly everywhere: `Budget` stops an agent on
turns, tokens, seconds and tool calls; the abandonment bounds catch a silent
host agent; a phase transition ends the agents of the phase it leaves; and the
lessons library has an age cap. What none of them bounded was the *grant*. A run
resumed months later, on the same host and in the same directory, produced no
divergence at all -- `_check_resume_fidelity` compares host and workspace and
never time -- and executed against an envelope approved in a context that had
had months to move on.

The decision recorded with this work: a stale grant does not fail the run and
does not narrow it. Analysis and reporting continue freely; only the spawning of
an execution agent waits, which is the harness's existing rule -- nothing
touches your code before you approve it -- applied to consent that has gone
stale rather than to consent that was never given.
"""

from __future__ import annotations

from supervisor_harness.core.envelope import stale_reason
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import Phase, RunMode, ScopeEnvelope, TaskStatus
from supervisor_harness.serde import to_jsonable
from supervisor_harness.store.events import EventType

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"

ANCIENT = "2020-01-01T00:00:00Z"


async def _reach_approval(supervisor: Supervisor):
    response = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    while response.action not in ("await_approval", "complete", "failed"):
        response = await supervisor.advance(response.run_id)
    assert response.action == "await_approval", response.message
    return response


def _age_the_grant(supervisor: Supervisor, run_id: str, when: str = ANCIENT) -> None:
    """Re-record the run's envelope with an old grant date.

    Through the log, not by poking the snapshot: staleness is derived from what
    the log says, and a test that edited the state directly would not prove the
    derivation survives a fold.
    """
    session = supervisor.store.open(run_id)
    aged = ScopeEnvelope(
        paths=list(session.state.envelope.paths),
        forbidden_paths=list(session.state.envelope.forbidden_paths),
        source="run plan",
        granted_at=when,
    )
    session.emit(EventType.ENVELOPE_SET, {"envelope": to_jsonable(aged)})


async def _blocked_at_execution(supervisor: Supervisor):
    """Reach the point where the run would execute, with the grant already old.

    The order matters. Approving the tasks is what advances into execution, so
    the grant has to be aged *before* that call -- age it afterwards and the
    fake provider has already driven the whole run to completion, and there is
    nothing left to hold.
    """
    response = await _reach_approval(supervisor)
    _age_the_grant(supervisor, response.run_id)
    return await supervisor.approve(
        response.run_id,
        [{"task_id": t["id"], "decision": "approve"} for t in response.tasks],
    )


# --------------------------------------------------------------------------
# The rule itself
# --------------------------------------------------------------------------


def test_the_grant_is_dated_from_when_it_was_made() -> None:
    envelope = ScopeEnvelope(paths=["src/**"])
    assert envelope.granted_at, "a grant with no date cannot be aged"
    assert stale_reason(envelope, "", 7) is None


def test_an_old_grant_is_stale_and_a_disabled_cap_never_is() -> None:
    old = ScopeEnvelope(paths=["src/**"], granted_at=ANCIENT)
    reason = stale_reason(old, "", 7)
    assert reason and "past the 7-day limit" in reason
    assert stale_reason(old, "", 0) is None, "0 disables the bound"


def test_a_run_recorded_before_grants_were_dated_ages_from_its_creation() -> None:
    """Envelopes written by an earlier build carry no date of their own."""
    undated = ScopeEnvelope(paths=["src/**"], granted_at="")
    assert stale_reason(undated, ANCIENT, 7)
    assert stale_reason(undated, "", 7) is None, "nothing to measure from"
    # And a run that predates envelopes entirely still has a creation date.
    assert stale_reason(None, ANCIENT, 7)


def test_an_unreadable_date_does_not_block_a_resume() -> None:
    """A bug in date parsing must not be able to stop a run.

    The opposite choice -- treat anything unreadable as expired -- is the safe
    direction for a fence, and is the wrong one here: this gate does not protect
    the workspace from the agent (the scope fence and its floor do that), it
    asks the user to confirm intent. Failing it closed would refuse a resume
    over a formatting problem.
    """
    assert stale_reason(ScopeEnvelope(granted_at="not-a-date"), "", 7) is None


# --------------------------------------------------------------------------
# What it does to a run
# --------------------------------------------------------------------------


async def test_a_stale_grant_pauses_before_execution_and_says_why(
    supervisor: Supervisor,
) -> None:
    paused = await _blocked_at_execution(supervisor)

    assert paused.action == "await_approval"
    assert paused.detail["needs"] == "envelope_renewal"
    assert "past the 7-day limit" in paused.message
    assert paused.detail["envelope"]["paths"] == ["src/**", "tests/**"]

    state = supervisor.store.load_state(paused.run_id)
    assert state.phase is Phase.EXECUTING, "it waits in execution, it does not fail"
    assert not [a for a in state.agents.values() if a.kind.value == "execution"], (
        "an execution agent was spawned against a stale grant"
    )

    # And it keeps holding: the pause is a property of the grant, not a one-off.
    again = await supervisor.resume(paused.run_id)
    assert again.detail["needs"] == "envelope_renewal"


async def test_the_analysis_a_stale_run_already_did_is_not_thrown_away(
    supervisor: Supervisor,
) -> None:
    """Only writing waits. What the run already established is still there."""
    paused = await _blocked_at_execution(supervisor)

    state = supervisor.store.load_state(paused.run_id)
    assert state.findings, "the findings survived"
    assert state.report is not None, "the report survived"
    assert all(t.status is TaskStatus.APPROVED for t in state.tasks.values())


async def test_renewing_the_grant_lets_the_run_finish(supervisor: Supervisor) -> None:
    paused = await _blocked_at_execution(supervisor)
    run_id = paused.run_id
    assert paused.detail["needs"] == "envelope_renewal"

    renewed = await supervisor.approve(run_id, [], renew_envelope=True)
    assert renewed.action != "await_approval", renewed.message

    state = supervisor.store.load_state(run_id)
    assert state.envelope.source == "renewed on resume"
    assert stale_reason(state.envelope, state.created_at, 7) is None
    assert [a for a in state.agents.values() if a.kind.value == "execution"], (
        "execution was still refused after the grant was renewed"
    )


async def test_renewing_a_grant_renews_its_date_and_not_its_extent(
    supervisor: Supervisor,
) -> None:
    """The rule approval has always been held to, applied to renewal.

    Widening at approval was refused when the envelope was built, for a reason
    that does not change because the grant got old: a bound a per-task decision
    can move is only as strong as the most permissive decision anyone made.
    """
    response = await _reach_approval(supervisor)
    run_id = response.run_id
    before = supervisor.store.load_state(run_id).envelope

    _age_the_grant(supervisor, run_id)
    await supervisor.approve(run_id, [], renew_envelope=True)
    after = supervisor.store.load_state(run_id).envelope

    assert after.paths == before.paths
    assert after.forbidden_paths == before.forbidden_paths
    assert after.granted_at > before.granted_at


async def test_a_disabled_cap_leaves_an_old_run_alone(
    supervisor: Supervisor, config,
) -> None:
    """A guard on the escape hatch: someone who does not want this can turn it off."""
    config.policy.envelope_max_age_days = 0

    resumed = await _blocked_at_execution(supervisor)
    assert resumed.detail.get("needs") != "envelope_renewal"


async def test_status_reports_a_stale_grant(supervisor: Supervisor) -> None:
    response = await _reach_approval(supervisor)
    run_id = response.run_id

    assert supervisor.status(run_id)["envelope_stale"] is None

    _age_the_grant(supervisor, run_id)
    stale = supervisor.status(run_id)["envelope_stale"]
    assert stale and "past the 7-day limit" in stale
