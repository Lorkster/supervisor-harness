"""What the fold must preserve, and what a state read must never invent.

The event log is authoritative, so every event type either projects into the
state or is visibly accounted for, and a state read reports the run it was
asked about rather than one the fold happened to generate.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from supervisor_harness.models import AgentSpec, Phase, RunState
from supervisor_harness.serde import to_jsonable
from supervisor_harness.store.eventlog import EventLog
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


def test_an_unknown_event_type_reaches_the_fold_from_disk(tmp_path: Path) -> None:
    """The fold's unhandled branch is unreachable if the read path drops the line.

    Enum coercion raises for a type this build does not define, and `read`
    treats a raise as a torn line -- so a misspelled or future type in a real
    log was discarded one layer below the branch that exists to record it.
    """
    log = EventLog(tmp_path / "run_A.jsonl")
    log.append(_event(EventType.NOTE, {"text": "run created"}, seq=0))
    log.append(_event("future_event", {"whatever": 1}, seq=0))

    events = log.read_all()
    assert len(events) == 2, "the unknown type was dropped on the way back in"

    state = fold(events)
    assert state.unhandled_events == ["future_event"], (
        "the fold must report the type the log actually named, not the sentinel"
    )


def test_a_torn_line_is_still_skipped(tmp_path: Path) -> None:
    """Keeping an unknown type must not turn every unreadable line into one."""
    path = tmp_path / "run_A.jsonl"
    path.write_text(
        '{"seq": 1, "run_id": "run_A", "type": "note", "payload": {"text": "ok"}}\n'
        '{"seq": 2, "run_id": "run_A", "type": "no\n',
        encoding="utf-8",
    )

    events = EventLog(path).read_all()

    assert [e.seq for e in events] == [1]
    assert fold(events).unhandled_events == []


# -- containment -----------------------------------------------------------


def test_one_unapplicable_event_does_not_end_the_replay() -> None:
    """A single bad payload used to make a run permanently unresumable.

    ``fold`` applied every record with nothing around it, so one payload the
    build could not project raised out of ``fold``, out of ``RunStore.open`` and
    out of every command that reads a run. The log is append-only, so that state
    was permanent, and the intact events after the bad one were unreachable.
    """
    state = fold([
        _event(EventType.PHASE_CHANGED, {"phase": "analyzing"}, seq=1),
        _event(EventType.PHASE_CHANGED, {"phase": "not_a_phase"}, seq=2),
        _event(EventType.FINDING_ADDED, {"finding": {"id": "fnd_1", "title": "kept"}}, seq=3),
    ])

    assert state.phase is Phase.ANALYZING, "the good events either side still applied"
    assert [f.id for f in state.findings] == ["fnd_1"]
    assert len(state.rejected_events) == 1
    assert "phase_changed" in state.rejected_events[0]


def test_an_event_whose_target_is_absent_is_recorded_not_ignored() -> None:
    """A no-op is indistinguishable from an event that had nothing to do.

    An AGENT_STATUS naming an agent the log never spawned, or a
    CRITERION_VERIFIED for a task that is not there, simply fell through. A log
    disagreeing with itself folded to a state that looked complete.
    """
    state = fold([
        _event(EventType.AGENT_STATUS, {"agent_id": "agt_ghost", "status": "done"}, seq=1),
        _event(EventType.AGENT_DISPATCHED, {"agent_id": "agt_ghost"}, seq=2),
        _event(
            EventType.CRITERION_VERIFIED,
            {"task_id": "tsk_ghost", "criterion_id": "crt_1", "status": "pass"},
            seq=3,
        ),
    ])

    assert state.orphaned_events == [
        "agent_status -> agt_ghost",
        "agent_dispatched -> agt_ghost",
        "criterion_verified -> tsk_ghost",
    ]


def test_a_repeated_orphan_is_recorded_once() -> None:
    """The record is a diagnostic, not a tally; it must not grow per event."""
    events = [
        _event(EventType.AGENT_STATUS, {"agent_id": "agt_ghost", "status": "done"}, seq=i)
        for i in range(1, 6)
    ]
    assert fold(events).orphaned_events == ["agent_status -> agt_ghost"]


# -- idempotence -----------------------------------------------------------


def test_replaying_the_log_does_not_duplicate_what_it_records() -> None:
    """The dict branches were idempotent and the list branches were not.

    That asymmetry was per-branch rather than by design. Replaying a log --
    which ``reindex``, ``reload`` and every second reader do routinely --
    duplicated every finding, directive, message, checkpoint and lesson in it,
    while leaving agents and tasks correct.
    """
    events = [
        _event(EventType.FINDING_ADDED, {"finding": {"id": "fnd_1", "title": "one"}}, seq=1),
        _event(EventType.DIRECTIVE_ISSUED, {"directive": {"id": "dir_1"}}, seq=2),
        _event(EventType.MESSAGE_SENT, {"message": {"id": "msg_1"}}, seq=3),
        _event(EventType.LESSON_LEARNED, {"lesson": {"id": "lsn_1"}}, seq=4),
        _event(
            EventType.CHECKPOINT_RECORDED,
            {"checkpoint": {"id": "chk_1", "iteration": 1}},
            seq=5,
        ),
    ]

    once = fold(events)
    twice = fold(events + events)

    for name in ("findings", "directives", "messages", "lessons", "checkpoints"):
        assert len(getattr(twice, name)) == len(getattr(once, name)) == 1, name


def test_a_replayed_checkpoint_does_not_extend_the_remediation_budget() -> None:
    """The counter is a high-water mark, not the last iteration seen.

    Assignment let a replayed or out-of-order checkpoint move it backwards, and
    the remediation budget is bounded on it -- so a log read in a different order
    silently bought the run another round it had already spent.
    """
    def checkpoint(ident: str, iteration: int, *, seq: int) -> Event:
        return _event(
            EventType.CHECKPOINT_RECORDED,
            {"checkpoint": {"id": ident, "iteration": iteration}},
            seq=seq,
        )

    assert fold([checkpoint("chk_1", 1, seq=1), checkpoint("chk_2", 2, seq=2)]).checkpoint_iteration == 2

    # The later record carries the lower iteration: an earlier checkpoint
    # re-emitted, or two writers whose records landed in the other order.
    # Assignment took the last one seen and handed the run a spent round back.
    assert fold([checkpoint("chk_2", 2, seq=1), checkpoint("chk_1", 1, seq=2)]).checkpoint_iteration == 2

    # And folding onto a state that already holds a higher mark, as any replay
    # onto a live state does.
    resumed = fold([checkpoint("chk_1", 1, seq=1)], initial=RunState(checkpoint_iteration=3))
    assert resumed.checkpoint_iteration == 3


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
    """The directory name is the run's identity; the snapshot is derived.

    ``last_seq`` matches the log deliberately. The snapshot has to be *current*
    for this property to be the one under test -- a snapshot behind the log is
    now discarded in favour of the fold, and would prove nothing about ids.
    """
    store = _store(tmp_path)
    store.log("run_A").append(Event(run_id="run_A", type=EventType.NOTE))
    store.save_snapshot(RunState(id="run_B", prompt="written under the wrong id", last_seq=1))
    (store.runs_dir / "run_B" / "state.json").replace(store.runs_dir / "run_A" / "state.json")

    state = store.load_state("run_A")
    assert state.id == "run_A"
    assert state.prompt == "written under the wrong id"


def test_load_state_refuses_a_run_that_does_not_exist(tmp_path: Path) -> None:
    """Reading an unknown run fails closed rather than inventing a state."""
    store = _store(tmp_path)

    with pytest.raises(FileNotFoundError):
        store.load_state("run_missing")
    assert not (store.runs_dir / "run_missing").exists()


def test_a_run_reports_the_lines_its_log_could_not_read(tmp_path: Path) -> None:
    """A line that is not an event cannot describe itself, so it must be counted.

    Skipping it is right -- there is nothing else to do with it -- but doing so
    silently meant a log could lose records and still read back as a complete,
    plausible run. The count is the only trace such a line leaves.
    """
    store = _store(tmp_path)
    store.log("run_A").append(Event(run_id="run_A", type=EventType.NOTE, payload={"text": "ok"}))
    with (store.runs_dir / "run_A" / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("this is not an event\n{ also not one\n")

    state = store.open("run_A").state

    assert state.damaged_lines == 2
    assert store.load_state("run_A").damaged_lines == 2


# -- the snapshot answers to the log ----------------------------------------


def test_a_snapshot_behind_the_log_loses_to_it(tmp_path: Path) -> None:
    """The snapshot is a cache, and it used to win on the strength of parsing.

    The fold was reached only when ``json.loads`` threw, so a snapshot that
    parsed but was *behind* -- what two processes reporting at once produce --
    was preferred over a log that was correct, for as long as the file sat there.
    Nothing compared the two, and nothing could: the state recorded no position
    in the log at all.
    """
    store = _store(tmp_path)
    log = store.log("run_A")
    log.append(Event(run_id="run_A", type=EventType.RUN_CREATED,
                     payload={"run": to_jsonable(RunState(id="run_A", prompt="the real prompt"))}))
    log.append(Event(run_id="run_A", type=EventType.PHASE_CHANGED, payload={"phase": "analyzing"}))

    # What a process that had folded only the first event would leave behind.
    store.save_snapshot(RunState(id="run_A", prompt="the real prompt", last_seq=1))

    state = store.load_state("run_A")
    assert state.last_seq == 2
    assert state.phase is Phase.ANALYZING, "the log held a phase the snapshot had not seen"


def test_a_current_snapshot_is_still_preferred_to_a_fold(tmp_path: Path) -> None:
    """The watermark must not turn the cache off; it decides when to trust it."""
    store = _store(tmp_path)
    store.log("run_A").append(Event(run_id="run_A", type=EventType.NOTE))
    store.save_snapshot(RunState(id="run_A", prompt="only in the snapshot", last_seq=1))

    assert store.load_state("run_A").prompt == "only in the snapshot"


def test_a_snapshot_written_before_the_watermark_existed_reads_as_stale(
    tmp_path: Path,
) -> None:
    """Such a file carries no position, so it is refolded once and rewritten."""
    store = _store(tmp_path)
    store.log("run_A").append(Event(run_id="run_A", type=EventType.RUN_CREATED,
                                    payload={"run": to_jsonable(RunState(id="run_A", prompt="from the log"))}))
    snapshot = store.runs_dir / "run_A" / "state.json"
    snapshot.write_text(
        json.dumps({"id": "run_A", "prompt": "from a build with no last_seq"}), encoding="utf-8"
    )

    assert store.load_state("run_A").prompt == "from the log"


def test_the_fold_records_the_position_it_reached() -> None:
    """A rejected event still advances the mark: it will be rejected every time."""
    state = fold([
        _event(EventType.NOTE, seq=1),
        _event(EventType.PHASE_CHANGED, {"phase": "not_a_phase"}, seq=2),
        _event(EventType.NOTE, seq=3),
    ])

    assert state.last_seq == 3
    assert len(state.rejected_events) == 1


def test_concurrent_snapshot_writers_never_leave_a_partial_file(tmp_path: Path) -> None:
    """The temporary file used to be named after its target, so it was shared.

    ``state.json.tmp`` is the same name for every writer of a run, so two
    processes reporting at once wrote into one file and renamed it twice, and the
    survivor held an interleaving of both. Every read below must see a whole,
    parseable snapshot -- never a half-written one.
    """
    store = _store(tmp_path)
    store.log("run_A").append(Event(run_id="run_A", type=EventType.NOTE))
    big = "x" * 200_000  # large enough that a partial write would be visible
    errors: list[BaseException] = []
    start = threading.Barrier(6)

    def write(n: int) -> None:
        try:
            start.wait(timeout=5.0)
            for _ in range(10):
                store.save_snapshot(RunState(id="run_A", prompt=f"{big}{n}", last_seq=1))
                store.load_state("run_A")  # must always parse
        except BaseException as exc:  # noqa: BLE001 - re-raised through the assertion
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert store.load_state("run_A").prompt.startswith(big)
    leftovers = list((store.runs_dir / "run_A").glob(".state-*.tmp"))
    assert leftovers == [], f"temporary files left behind: {leftovers}"


def test_a_snapshot_that_cannot_be_written_does_not_end_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot is derived, so losing it must cost only the fold it saved.

    The same rule ``sync_index`` states for the index. It is true rather than
    merely intended now that ``load_state`` rebuilds from the log whenever the
    snapshot is missing, unreadable or behind. The event log, which is not
    derived, still fails loudly -- that difference is the point.
    """
    store = _store(tmp_path)
    log = store.log("run_A")
    log.append(Event(run_id="run_A", type=EventType.RUN_CREATED,
                     payload={"run": to_jsonable(RunState(id="run_A", prompt="from the log"))}))

    def denied(self: Path, target: object) -> None:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "replace", denied)
    monkeypatch.setattr("supervisor_harness.store.runstore.time.sleep", lambda _s: None)

    assert store.save_snapshot(RunState(id="run_A", prompt="never lands", last_seq=1)) is False
    assert store.snapshot_error and "denied" in store.snapshot_error
    assert list((store.runs_dir / "run_A").glob(".state-*.tmp")) == []
    assert store.load_state("run_A").prompt == "from the log"
