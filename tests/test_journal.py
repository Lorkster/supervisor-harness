"""The decision journal: why each directive was issued, and on what evidence.

The plan that scheduled this work said every input was already in `RunState`.
The first test here is the measurement that showed otherwise, and it is why
`build_journal` reads the event log instead. Keep it: if `RunState.drift` ever
becomes a per-turn record, that test fails, and reading the log stops being
necessary rather than merely remaining correct.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from supervisor_harness.core.journal import (
    build_journal,
    journal_to_dict,
    render_journal,
)
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import AgentKind, DirectiveKind, RunMode
from supervisor_harness.store.events import Event, EventType

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"

#: An analysis answer that keeps the agent running, so one agent takes several
#: supervised turns. Without a multi-turn agent nothing overwrites anything and
#: the whole reason this reads the log is invisible.
STILL_WORKING = {
    "output": "Still reading src/auth/login.py; no conclusion yet.",
    "findings": [],
    "files_examined": ["src/auth/login.py"],
    "status": "running",
}

#: An analysis answer that is plainly off brief, so a real directive with real
#: corrections is issued rather than a bare CONTINUE.
WANDERED = {
    "output": "Rewrote the billing exporter in docs/ and refactored the CI config.",
    "findings": [],
    "files_examined": ["docs/billing.md"],
    "status": "running",
}


async def _run(supervisor: Supervisor) -> str:
    response = await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    return response.run_id


def _journal(supervisor: Supervisor, run_id: str, agent_id: str = ""):
    return supervisor.explain(run_id, agent_id)


# --------------------------------------------------------------------------
# Why the log, and not the snapshot
# --------------------------------------------------------------------------


async def test_runstate_keeps_only_the_newest_assessment_per_agent(
    supervisor: Supervisor, fake,
) -> None:
    """The measurement that decided where the journal reads from.

    `RunState.drift` is a dict keyed by agent id, so `DRIFT_ASSESSED` overwrites
    and every earlier assessment for that agent is gone from the snapshot. An
    assessment that has been overwritten cannot explain the directive it
    produced, which is the whole question this feature answers.
    """
    fake.script("analysis", dict(STILL_WORKING), dict(STILL_WORKING))
    run_id = await _run(supervisor)

    events = supervisor.store.log(run_id).read_all()
    state = supervisor.store.load_state(run_id)
    on_log = [e for e in events if e.type is EventType.DRIFT_ASSESSED]

    assert len(on_log) > len(state.drift), (
        "no agent took more than one supervised turn, so this run cannot show "
        "the overwrite the journal exists to work around"
    )

    # And the journal recovers every one of them.
    journal = _journal(supervisor, run_id)
    kept = sum(len(ep.assessments) for a in journal.agents for ep in a.episodes)
    assert kept == len(on_log)


async def test_every_assessment_names_the_turn_it_judged(
    supervisor: Supervisor, fake,
) -> None:
    """Without this the association is positional, which is weaker evidence."""
    fake.script("analysis", dict(STILL_WORKING))
    run_id = await _run(supervisor)

    journal = _journal(supervisor, run_id)
    for entry in journal.agents:
        for episode in entry.episodes:
            if episode.turn is None:
                continue
            for assessment in episode.assessments:
                assert assessment.turn_id == episode.turn.id
            if episode.directive is not None:
                assert episode.directive.turn_id == episode.turn.id


# --------------------------------------------------------------------------
# Assembling episodes
# --------------------------------------------------------------------------


async def test_an_episode_carries_the_turn_its_assessment_and_its_directive(
    supervisor: Supervisor, fake,
) -> None:
    """The one question the journal exists to answer, in one place."""
    fake.script("analysis", dict(WANDERED))
    run_id = await _run(supervisor)

    journal = _journal(supervisor, run_id)
    drifted = [
        ep
        for entry in journal.agents
        for ep in entry.episodes
        if ep.directive is not None
        and ep.directive.kind in (DirectiveKind.NARROW, DirectiveKind.REFOCUS)
    ]
    assert drifted, "the wandering agent should have been corrected"

    episode = drifted[0]
    assert episode.turn is not None
    assert "billing" in episode.turn.output
    assert episode.assessments and episode.assessments[0].signals
    assert episode.directive.rationale
    # The evidence and the decision are the same object, not two lookups.
    assert any(s.kind == "scope_paths" for s in episode.assessments[0].signals)


async def test_a_model_escalation_is_a_second_assessment_on_the_same_turn(
    supervisor: Supervisor, fake,
) -> None:
    """A second opinion must not read as a second turn, or as the only verdict."""
    fake.script("analysis", dict(WANDERED))
    run_id = await _run(supervisor)

    journal = _journal(supervisor, run_id)
    escalated = [
        ep for entry in journal.agents for ep in entry.episodes if ep.escalated
    ]
    assert escalated, "a hard drift signal should have escalated to the model"

    episode = escalated[0]
    assert len(episode.assessments) >= 2
    assert episode.assessments[0].checked_by == "heuristics"
    assert episode.assessments[-1].checked_by != "heuristics"
    # Both judge the same turn.
    assert {a.turn_id for a in episode.assessments} == {episode.turn.id}


async def test_a_verifier_has_no_directive_and_that_is_not_an_anomaly(
    supervisor: Supervisor,
) -> None:
    """A verifier is settled by its own verdict; a directive would act on nothing."""
    run_id = await _run(supervisor)

    journal = _journal(supervisor, run_id)
    verifiers = [
        entry for entry in journal.agents
        if entry.agent is not None and entry.agent.kind is AgentKind.VERIFICATION
    ]
    assert verifiers
    for entry in verifiers:
        turned = [ep for ep in entry.episodes if ep.turn is not None]
        assert turned
        for episode in turned:
            assert episode.directive is None
            assert episode.assessments, "a verifier is still assessed"
            assert not episode.anomalies


async def test_notes_recorded_before_an_agents_first_turn_are_kept(
    supervisor: Supervisor, config, fake,
) -> None:
    """The scope narrowings from the envelope work land here, or nowhere.

    They are emitted against the agent at spawn, before it has answered
    anything, so they belong to no turn. An assembler that only knew how to
    attach events to turns would drop exactly the record of what authority the
    agent was cut down to.
    """
    config.policy.scope_envelope = ["src/auth/**"]
    run_id = await _run(supervisor)

    journal = _journal(supervisor, run_id)
    opening = [
        ep for entry in journal.agents for ep in entry.episodes if ep.turn is None
    ]
    assert opening, "no pre-turn episode was opened"
    narrowings = [
        note.text for ep in opening for note in ep.notes if "scope narrowed" in note.text
    ]
    assert narrowings


async def test_filtering_by_agent_keeps_the_run_level_facts(
    supervisor: Supervisor,
) -> None:
    """An agent's scope means nothing without the envelope above it."""
    run_id = await _run(supervisor)
    full = _journal(supervisor, run_id)
    target = full.agents[0].agent.id

    one = _journal(supervisor, run_id, target)
    assert [entry.agent.id for entry in one.agents] == [target]
    assert one.envelope_history == full.envelope_history
    assert one.prompt == full.prompt and one.phase == full.phase


async def test_the_envelope_chain_is_shown_in_full(supervisor: Supervisor) -> None:
    """Both links, not just the last one: the provenance is the point."""
    run_id = await _run(supervisor)
    journal = _journal(supervisor, run_id)

    assert [e.source for e in journal.envelope_history] == ["configuration", "run plan"]
    assert journal.envelope == journal.envelope_history[-1]


# --------------------------------------------------------------------------
# Logs the journal did not write
# --------------------------------------------------------------------------


async def test_a_log_written_before_turn_ids_still_explains(
    supervisor: Supervisor, fake,
) -> None:
    """The positional fallback, which is the only thing an older run has.

    `turn_id` was added with this feature, so every log recorded before it is
    missing the field. Stripping it reproduces such a log exactly, and the
    journal must still place each assessment and directive on the turn it
    followed -- from the order the events were appended in.
    """
    fake.script("analysis", dict(STILL_WORKING))
    run_id = await _run(supervisor)
    state = supervisor.store.load_state(run_id)
    events = supervisor.store.log(run_id).read_all()

    stripped: list[Event] = []
    for event in events:
        payload = json.loads(json.dumps(event.payload))
        if event.type is EventType.DRIFT_ASSESSED:
            payload["assessment"].pop("turn_id", None)
        elif event.type is EventType.DIRECTIVE_ISSUED:
            payload["directive"].pop("turn_id", None)
        stripped.append(
            Event(seq=event.seq, id=event.id, run_id=event.run_id, type=event.type,
                  actor=event.actor, ts=event.ts, payload=payload)
        )

    modern = build_journal(state, events)
    legacy = build_journal(state, stripped)

    def shape(journal):
        return [
            [
                (
                    ep.turn.id if ep.turn else None,
                    [a.score for a in ep.assessments],
                    ep.directive.kind if ep.directive else None,
                )
                for ep in entry.episodes
            ]
            for entry in journal.agents
        ]

    assert shape(legacy) == shape(modern)
    # And the fallback is silent, because a missing link is not a disagreement.
    assert not [a for e in legacy.agents for ep in e.episodes for a in ep.anomalies]


async def test_a_turn_id_disagreeing_with_the_logs_order_is_reported(
    supervisor: Supervisor, fake,
) -> None:
    """A journal that quietly picks one of two answers is worse than one that
    says the record is ambiguous."""
    fake.script("analysis", dict(WANDERED))
    run_id = await _run(supervisor)
    state = supervisor.store.load_state(run_id)
    events = supervisor.store.log(run_id).read_all()

    corrupted: list[Event] = []
    for event in events:
        payload = json.loads(json.dumps(event.payload))
        if event.type is EventType.DIRECTIVE_ISSUED:
            payload["directive"]["turn_id"] = "trn_SOMETHINGELSE"
        corrupted.append(
            Event(seq=event.seq, id=event.id, run_id=event.run_id, type=event.type,
                  actor=event.actor, ts=event.ts, payload=payload)
        )

    journal = build_journal(state, corrupted)
    anomalies = [
        a for entry in journal.agents for ep in entry.episodes for a in ep.anomalies
    ]
    assert anomalies
    assert "trn_SOMETHINGELSE" in anomalies[0]


async def test_events_naming_an_unspawned_agent_are_counted_not_dropped(
    supervisor: Supervisor,
) -> None:
    """The same rule the fold follows for orphans: say so rather than no-op."""
    run_id = await _run(supervisor)
    state = supervisor.store.load_state(run_id)
    events = supervisor.store.log(run_id).read_all()
    events.append(
        Event(seq=10_000, run_id=run_id, type=EventType.AGENT_STATUS,
              payload={"agent_id": "agt_NEVERSPAWNED", "status": "running"})
    )

    journal = build_journal(state, events)
    assert journal.unattributed == 1


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


async def test_the_rendered_journal_names_the_evidence_behind_a_correction(
    supervisor: Supervisor, fake,
) -> None:
    fake.script("analysis", dict(WANDERED))
    run_id = await _run(supervisor)

    text = render_journal(_journal(supervisor, run_id))
    assert "DIRECTIVE NARROW" in text
    assert "scope_paths" in text          # the signal
    assert "docs/billing.md" in text      # the evidence
    assert "because" in text              # the rationale
    assert "envelope" in text


async def test_turns_are_numbered_by_turn_not_by_episode(
    supervisor: Supervisor, config,
) -> None:
    """An opening episode is not a turn, and must not shift the count.

    Found by reading the output rather than by a failing assertion: an agent
    whose scope was narrowed at spawn had its first answer rendered as "turn 1".
    """
    config.policy.scope_envelope = ["src/auth/**"]
    run_id = await _run(supervisor)

    text = render_journal(_journal(supervisor, run_id))
    assert "before the first turn" in text, "this run did not exercise the case"
    assert "turn 0 |" in text
    # Every agent's first rendered turn is turn 0, whatever preceded it.
    for block in text.split(chr(10) + "  agt_")[1:]:
        turns = [l for l in block.splitlines() if l.strip().startswith("turn ")]
        if turns:
            assert turns[0].strip().startswith("turn 0 |"), turns[0]


def test_the_renderer_contains_no_non_ascii_literal() -> None:
    """A guard, not a proof of correctness.

    Terminal output in this package is ASCII throughout, and CI runs on a
    Windows console whose encoding is not UTF-8; a stray typographic character
    is a `UnicodeEncodeError` on someone else's machine and nowhere on ours. It
    was one: the first draft separated a turn from its timestamp with U+00B7.

    This reads the module source rather than one run's output on purpose.
    Rendering a run only exercises the branches that run happened to take, so a
    typographic character in -- say -- the line for an agent with no directive
    would pass unnoticed until someone hit it.
    """
    from supervisor_harness.core import journal as module

    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    offenders = sorted({c for c in source if ord(c) > 127})
    assert not offenders, f"non-ASCII in the renderer: {offenders}"


async def test_the_rendered_journal_is_ascii(supervisor: Supervisor, fake) -> None:
    """The same guard on an actual run, which the source check cannot replace:
    a value interpolated from a model's answer is not a literal."""
    fake.script("analysis", dict(WANDERED))
    run_id = await _run(supervisor)

    text = render_journal(_journal(supervisor, run_id))
    text.encode("ascii")


async def test_the_journal_is_json_serialisable(supervisor: Supervisor, fake) -> None:
    """It is served over MCP and by `--json`, so it must survive a dump."""
    fake.script("analysis", dict(WANDERED))
    run_id = await _run(supervisor)

    payload = journal_to_dict(_journal(supervisor, run_id))
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped["run_id"] == run_id
    assert round_tripped["agents"]
    assert any(
        ep["directive"] for a in round_tripped["agents"] for ep in a["episodes"]
    )


@pytest.mark.parametrize("argv_agent", ["", "agt_NOSUCHAGENT"])
async def test_the_cli_refuses_an_unknown_agent_and_accepts_a_known_run(
    supervisor: Supervisor, workspace, argv_agent: str,
) -> None:
    """`explain` is a read command; it must fail loudly rather than print nothing."""
    from supervisor_harness import cli

    run_id = await _run(supervisor)
    args = type("Args", (), {
        "run_id": run_id, "agent": argv_agent, "width": 96,
        "json": False, "workspace": str(workspace), "debug": False,
    })()

    original = cli._supervisor
    cli._supervisor = lambda _: supervisor
    try:
        code = cli.cmd_explain(args)
    finally:
        cli._supervisor = original

    assert code == (2 if argv_agent else 0)
