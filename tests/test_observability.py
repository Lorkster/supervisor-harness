"""What a run says about itself while it is happening, and afterwards.

Batch 3 of `docs/development-plan.md`, and the observation behind it was two
complaints that turned out to be different problems: a supervised run is slow,
and while it is slow nothing tells you anything. The first is partly inherent --
twenty-five to forty sequential sub-agent invocations against a usual three to
five -- and the honest answer to it is attribution, not a promise of speed. The
second was simply missing.

Three surfaces, tested here because none of them has a natural home in the
phase-machine tests:

* `assists` -- what the harness had to repair before an answer could be used
* `core/timing` -- where the wall clock went, folded from the log
* `store/progress` -- the tailable line-per-event trail
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from supervisor_harness.assists import assisting, current_assists, record_assist
from supervisor_harness.config import HarnessConfig, Policy, default_config
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.core.timing import clock, measure
from supervisor_harness.host.detect import HostInfo
from supervisor_harness.models import Backend, RunMode
from supervisor_harness.providers.base import extract_json
from supervisor_harness.store.events import Event, EventType
from supervisor_harness.store.runstore import RunStore

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"


@pytest.fixture
def host_config() -> HarnessConfig:
    cfg = default_config()
    cfg.backend = Backend.HOST
    cfg.routing = {k: "host" for k in cfg.routing}
    cfg.policy = Policy(default_max_turns=3, execution_max_turns=3, max_analysis_lenses=2)
    return cfg


@pytest.fixture
def supervisor(workspace: Path, host_config: HarnessConfig) -> Supervisor:
    return Supervisor(
        workspace=workspace, config=host_config, store=RunStore(workspace / ".supervisor"),
        host=HostInfo(name="claude-code", workspace=str(workspace), confidence=1.0),
    )


# -- assists ----------------------------------------------------------------


def test_recording_outside_a_span_is_a_no_op() -> None:
    """The property that keeps the call sites to one unconditional line.

    `extract_json` lives in `providers` and has no idea whether a run is
    happening. If recording outside a span raised, or required a guard, the
    guard would be repeated at every site and eventually forgotten at one.
    """
    record_assist("json_from_prose", "no span is open")

    assert not current_assists()
    assert current_assists().total == 0


def test_a_span_collects_what_was_repaired_inside_it() -> None:
    with assisting("analysis.security") as assists:
        record_assist("json_from_prose", "```json {...}")
        record_assist("json_from_prose", "```json {...}")
        record_assist("dependency_by_title", "Add the limiter")

    assert assists.total == 3
    assert assists.counts == {"json_from_prose": 2, "dependency_by_title": 1}
    assert assists.stage == "analysis.security"
    assert "json_from_prosex2" in assists.summary()


def test_a_span_does_not_leak_into_the_one_outside_it() -> None:
    with assisting("outer") as outer:
        with assisting("inner") as inner:
            record_assist("json_non_strict")
        record_assist("plan_unusable")

    assert inner.counts == {"json_non_strict": 1}
    assert outer.counts == {"plan_unusable": 1}


def test_samples_are_bounded() -> None:
    """A run with a thousand repairs must not carry a thousand strings."""
    with assisting() as assists:
        for i in range(50):
            record_assist("json_from_prose", f"sample {i}")

    assert assists.counts["json_from_prose"] == 50
    assert len(assists.samples["json_from_prose"]) == 3


def test_extracting_json_from_prose_is_counted_as_a_repair() -> None:
    """The deepest call site, and the one that fires most in practice."""
    with assisting("planning") as assists:
        clean = extract_json('{"a": 1}')
        assert clean == {"a": 1}
        assert not assists, "an answer that was already JSON needed no help"

        wrapped = extract_json('Here you go:\n```json\n{"a": 1}\n```\nHope that helps.')

    assert wrapped == {"a": 1}
    assert assists.counts.get("json_from_prose") == 1


def test_a_list_where_an_object_was_required_is_counted() -> None:
    with assisting() as assists:
        assert extract_json("[1, 2, 3]") == {"items": [1, 2, 3]}

    assert assists.counts.get("json_list_wrapped") == 1


async def test_a_run_that_needed_no_help_says_so_by_being_empty(
    supervisor: Supervisor,
) -> None:
    """Empty is the answer, not a missing field."""
    started = await supervisor.start(PROMPT, mode=RunMode.REPORT)

    assert supervisor.reporting.status(started.run_id)["assists"] == {}


# -- timing -----------------------------------------------------------------


def _event(seq: int, type_: EventType, ts: str, payload: dict | None = None) -> Event:
    return Event(seq=seq, run_id="run_x", type=type_, ts=ts, payload=payload or {})


def test_a_dispatch_is_measured_from_handout_to_answer() -> None:
    events = [
        _event(1, EventType.RUN_CREATED, "2026-09-07T10:00:00Z"),
        _event(2, EventType.AGENT_DISPATCHED, "2026-09-07T10:00:10Z",
               {"agent_id": "agt_1", "kind": "analysis"}),
        _event(3, EventType.TURN_RECORDED, "2026-09-07T10:00:40Z",
               {"turn": {"agent_id": "agt_1"}}),
    ]

    timing = measure(events, now="2026-09-07T10:00:40Z")

    assert timing.total_seconds == 40
    assert timing.dispatched_seconds == 30
    assert timing.by_kind == {"analysis": 30.0}
    assert timing.slowest is not None and timing.slowest.agent_id == "agt_1"


def test_an_unanswered_dispatch_is_waiting_rather_than_elapsed() -> None:
    """An agent that never answers must not inflate the run's total for ever.

    This is the case the harness cannot tell from a slow one, so it is exactly
    the case the timing has to report separately rather than absorb.
    """
    events = [
        _event(1, EventType.RUN_CREATED, "2026-09-07T10:00:00Z"),
        _event(2, EventType.AGENT_DISPATCHED, "2026-09-07T10:00:10Z",
               {"agent_id": "agt_gone", "kind": "analysis"}),
    ]

    timing = measure(events, now="2026-09-07T10:05:10Z")

    assert timing.dispatches == []
    assert timing.dispatched_seconds == 0
    assert [s.agent_id for s in timing.waiting] == ["agt_gone"]
    assert timing.waiting[0].seconds == 300
    assert "1 still out" in timing.summary()


def test_phases_are_measured_between_their_changes() -> None:
    events = [
        _event(1, EventType.PHASE_CHANGED, "2026-09-07T10:00:00Z", {"phase": "analyzing"}),
        _event(2, EventType.PHASE_CHANGED, "2026-09-07T10:01:00Z", {"phase": "synthesizing"}),
        _event(3, EventType.NOTE, "2026-09-07T10:01:30Z"),
    ]

    timing = measure(events, now="2026-09-07T10:01:30Z")

    assert timing.by_phase == {"analyzing": 60.0, "synthesizing": 30.0}


def test_a_re_issued_packet_is_measured_against_the_answer_it_gets() -> None:
    """A packet handed out twice before an answer has one span, not two."""
    events = [
        _event(1, EventType.AGENT_DISPATCHED, "2026-09-07T10:00:00Z",
               {"agent_id": "agt_1", "kind": "analysis"}),
        _event(2, EventType.AGENT_DISPATCHED, "2026-09-07T10:00:30Z",
               {"agent_id": "agt_1", "kind": "analysis"}),
        _event(3, EventType.TURN_RECORDED, "2026-09-07T10:00:40Z",
               {"turn": {"agent_id": "agt_1"}}),
    ]

    timing = measure(events, now="2026-09-07T10:00:40Z")

    assert len(timing.dispatches) == 1
    assert timing.dispatches[0].seconds == 10, "measured from the re-issue, not the first try"


def test_a_malformed_timestamp_shortens_a_measurement_rather_than_raising() -> None:
    """A projection that dies on one bad line is worse than a slight undercount."""
    events = [
        _event(1, EventType.AGENT_DISPATCHED, "not a timestamp",
               {"agent_id": "agt_1", "kind": "analysis"}),
        _event(2, EventType.TURN_RECORDED, "2026-09-07T10:00:40Z",
               {"turn": {"agent_id": "agt_1"}}),
    ]

    timing = measure(events, now="2026-09-07T10:00:40Z")

    assert timing.dispatches[0].seconds == 0.0


def test_measuring_nothing_says_nothing() -> None:
    assert measure([]).summary() == "no elapsed time recorded"


@pytest.mark.parametrize(
    ("seconds", "rendered"),
    [(0.25, "250ms"), (4.5, "4.5s"), (90, "1m30s"), (3900, "1h05m")],
)
def test_durations_read_as_someone_would_say_them(seconds: float, rendered: str) -> None:
    assert clock(seconds) == rendered


# -- the ledger -------------------------------------------------------------


async def test_the_ledger_says_where_the_run_is(supervisor: Supervisor) -> None:
    started = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)

    ledger = supervisor.reporting.ledger(started.run_id)

    assert str(started.phase) in ledger
    assert "agents done" in ledger
    assert "\n" not in ledger, "one line, or the host will summarise it instead of printing it"


async def test_the_ledger_names_an_agent_that_has_not_answered(
    supervisor: Supervisor,
) -> None:
    """The distinction the user could not make: slow, or stuck.

    A dispatched agent that has not reported is the whole difference, and it is
    the one thing the harness knows and never said.
    """
    started = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)

    assert "out, longest" in supervisor.reporting.ledger(started.run_id)


async def test_the_ledger_reaches_the_host_on_every_response(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stamped where a response reaches a person, not at each construction site."""
    from supervisor_harness import mcp_server

    monkeypatch.setattr(mcp_server, "_supervisor", supervisor, raising=False)
    monkeypatch.setattr(mcp_server, "supervisor", lambda: supervisor)

    started = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    payload = mcp_server._result(started)

    assert payload["ledger"]
    assert payload["next_step"].startswith("Print the ledger line")


# -- progress ---------------------------------------------------------------


async def test_a_run_leaves_a_tailable_trail(supervisor: Supervisor) -> None:
    """`tail -f` is the whole interface, so the file has to be line-per-event."""
    started = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    path = supervisor.store.run_dir(started.run_id) / "progress.ndjson"

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert lines, "a started run wrote no progress"
    assert all({"seq", "ts", "type", "note"} <= set(line) for line in lines)
    assert any(line["type"] == "run_created" for line in lines)
    assert any(line["note"] == "dispatched, waiting" for line in lines)


async def test_progress_lines_are_shorter_than_the_log_they_derive_from(
    supervisor: Supervisor,
) -> None:
    """The reason it is a separate file rather than "just tail events.jsonl".

    The event log is authoritative and carries whole briefs; following it means
    watching thousands of characters scroll past to learn that a lens finished.
    """
    started = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    run_dir = supervisor.store.run_dir(started.run_id)

    events = (run_dir / "events.jsonl").stat().st_size
    progress = (run_dir / "progress.ndjson").stat().st_size

    assert progress * 5 < events


def test_a_progress_file_that_cannot_be_written_does_not_fail_a_run(
    tmp_path: Path,
) -> None:
    """Derived and disposable: it must never cost the authoritative record.

    Swallowing an error is the wrong default almost everywhere in the store,
    and the right one here for exactly that reason.
    """
    from supervisor_harness.store import progress

    blocked = tmp_path / "a-file-not-a-directory" / "progress.ndjson"
    blocked.parent.write_text("I am a file", encoding="utf-8")

    progress.append(blocked, [_event(1, EventType.NOTE, "2026-09-07T10:00:00Z")])

    assert not blocked.exists()


def test_an_unmapped_event_type_still_appears() -> None:
    """A new event type vanishing from the progress view is the worse failure."""
    line = __import__(
        "supervisor_harness.store.progress", fromlist=["as_line"]
    ).as_line(_event(1, EventType.ENVELOPE_SET, "2026-09-07T10:00:00Z"))

    assert line["type"] == "envelope_set"
    assert line["note"]
