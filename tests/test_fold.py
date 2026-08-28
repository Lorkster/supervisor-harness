"""What the fold must preserve, and what a state read must never invent.

The event log is authoritative, so every event type either projects into the
state or is visibly accounted for, and a state read reports the run it was
asked about rather than one the fold happened to generate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from supervisor_harness.models import AgentSpec, Phase, RunState
from supervisor_harness.serde import to_jsonable
from supervisor_harness.store.events import Event, EventType, fold
from supervisor_harness.store.runstore import RunStore


def _event(type: EventType | str, payload: dict | None = None, *, seq: int, actor: str = "supervisor") -> Event:
    return Event(seq=seq, run_id="run_A", type=type, actor=actor, payload=payload or {})


# -- artifacts -------------------------------------------------------------


def test_an_artifact_written_event_reaches_the_folded_state() -> None:
    """A run's artifacts are recoverable by replay, not only from the disk."""
    state = fold([
        _event(EventType.RUN_CREATED, {"run": to_jsonable(RunState(id="run_A"))}, seq=1),
        _event(
            EventType.ARTIFACT_WRITTEN,
            {"path": "/runs/run_A/artifacts/report.md", "kind": "report"},
            seq=2,
            actor="agt_1",
        ),
    ])

    assert [(a.path, a.kind, a.actor) for a in state.artifacts] == [
        ("/runs/run_A/artifacts/report.md", "report", "agt_1")
    ]
    assert state.artifacts[0].ts


def test_a_rewritten_artifact_keeps_one_entry_per_path() -> None:
    """The report is rewritten each improvement iteration; the latest wins."""
    state = fold([
        _event(EventType.ARTIFACT_WRITTEN, {"path": "report.md", "kind": "report"}, seq=1),
        _event(EventType.ARTIFACT_WRITTEN, {"path": "notes.md", "kind": "note"}, seq=2),
        _event(EventType.ARTIFACT_WRITTEN, {"path": "report.md", "kind": "final-report"}, seq=3),
    ])

    assert [(a.path, a.kind) for a in state.artifacts] == [
        ("notes.md", "note"),
        ("report.md", "final-report"),
    ]


# -- unhandled types -------------------------------------------------------


def test_an_event_type_with_no_branch_is_recorded_not_dropped() -> None:
    """A type added later must be visible in the state, not silently discarded."""
    state = fold([
        _event("future_event", {"whatever": 1}, seq=1),
        _event("future_event", {"whatever": 2}, seq=2),
    ])

    assert state.unhandled_events == ["future_event"]


def test_the_declared_types_the_fold_handles_are_not_reported_as_unhandled() -> None:
    """Including NOTE, whose no-op is deliberate rather than an omission."""
    state = fold([
        _event(EventType.NOTE, {"text": "run created"}, seq=1),
        _event(EventType.ARTIFACT_WRITTEN, {"path": "report.md"}, seq=2),
        _event(EventType.PHASE_CHANGED, {"phase": "analyzing"}, seq=3),
    ])

    assert state.unhandled_events == []


# -- genesis ---------------------------------------------------------------


def test_a_second_run_created_does_not_discard_the_fold() -> None:
    """A duplicated genesis record must not erase the history before it."""
    genesis = {"run": to_jsonable(RunState(id="run_A", prompt="harden login"))}
    state = fold([
        _event(EventType.RUN_CREATED, genesis, seq=1),
        _event(EventType.AGENT_SPAWNED, {"agent": to_jsonable(AgentSpec(id="agt_1"))}, seq=2),
        _event(EventType.PHASE_CHANGED, {"phase": "analyzing"}, seq=3),
        _event(EventType.RUN_CREATED, genesis, seq=4),
    ])

    assert list(state.agents) == ["agt_1"]
    assert state.phase is Phase.ANALYZING
    assert state.prompt == "harden login"


def test_fold_keeps_the_initial_state_given_to_it() -> None:
    """RUN_CREATED merges into the accumulator instead of replacing it."""
    initial = RunState(id="run_A", shared_context="prior context")
    state = fold(
        [_event(EventType.RUN_CREATED, {"run": to_jsonable(RunState(id="run_A", prompt="p"))}, seq=1)],
        initial,
    )

    assert state.shared_context == "prior context"
    assert state.prompt == "p"


# -- load_state ------------------------------------------------------------


def _store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / ".supervisor")


def test_load_state_reports_the_run_id_it_was_asked_for(tmp_path: Path) -> None:
    """A log without a readable genesis record still identifies its own run."""
    store = _store(tmp_path)
    log = store.log("run_A")
    log.append(Event(run_id="run_A", type=EventType.NOTE, payload={"text": "no genesis here"}))

    assert store.load_state("run_A").id == "run_A"


def test_load_state_corrects_a_snapshot_carrying_the_wrong_id(tmp_path: Path) -> None:
    """The directory name is the run's identity; the snapshot is derived."""
    store = _store(tmp_path)
    store.log("run_A").append(Event(run_id="run_A", type=EventType.NOTE))
    store.save_snapshot(RunState(id="run_B", prompt="stale"))
    (store.runs_dir / "run_B" / "state.json").replace(store.runs_dir / "run_A" / "state.json")

    state = store.load_state("run_A")
    assert state.id == "run_A"
    assert state.prompt == "stale"


def test_load_state_refuses_a_run_that_does_not_exist(tmp_path: Path) -> None:
    """Reading an unknown run fails closed rather than inventing a state."""
    store = _store(tmp_path)

    with pytest.raises(FileNotFoundError):
        store.load_state("run_missing")
    assert not (store.runs_dir / "run_missing").exists()
