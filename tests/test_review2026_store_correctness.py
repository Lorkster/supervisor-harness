"""Correctness findings from the 2026-11 review of the store and its neighbours."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness.config import Policy
from supervisor_harness.core.consolidate import consolidate
from supervisor_harness.core.journal import build_journal
from supervisor_harness.core.timing import _elapsed
from supervisor_harness.ids import new_id
from supervisor_harness.models import AgentSpec, AgentTurn, Lesson, RunState
from supervisor_harness.serde import to_jsonable
from supervisor_harness.store import eventlog, progress, runstore
from supervisor_harness.store.eventlog import EventLog, FileLock
from supervisor_harness.store.events import Event, EventType, event_from_dict
from supervisor_harness.store.runstore import RunStore

ROOT = Path(__file__).resolve().parent.parent


def _store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / ".supervisor")


def _note(seq: int, text: str = "n") -> Event:
    return Event(seq=seq, run_id="run_X", type=EventType.NOTE, payload={"text": text})


# -- C2: a damaged line costs that line, never the run ------------------------


def test_a_torn_multibyte_write_does_not_make_the_log_unreadable(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append(_note(0, "before"))
    with log.path.open("ab") as handle:
        handle.write(b'{"seq": 2, "payload": {"text": "caf' + "\u00e9".encode()[:1] + b"\n")
    log.append(_note(0, "after"))

    events = log.read_all()

    assert [e.payload["text"] for e in events] == ["before", "after"]
    assert log.skipped_lines >= 1, "the damage is counted, not hidden"


def test_a_line_with_replacement_characters_still_counts_as_damaged(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    good = json.dumps({"seq": 1, "type": "note", "payload": {"text": "ok \x00"}}).encode()
    bad = good.replace(b"ok", b"o\xff")
    log.path.write_bytes(good + b"\n" + bad + b"\n")

    events = list(log.read())

    assert len(events) == 2
    assert log.skipped_lines == 1
    assert "�" in events[1].payload["text"]


@pytest.mark.parametrize("line", ["[1, 2]", "5", '"text"', "null", '{"seq": "7"}',
                                  '{"seq": 1.5}', '{"seq": true}', '{"seq": 1, "payload": [1]}'])
def test_a_line_that_is_json_but_not_an_event_is_skipped_and_counted(
    tmp_path: Path, line: str,
) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append(_note(0, "a"))
    with log.path.open("ab") as handle:
        handle.write(line.encode() + b"\n")
    log.append(_note(0, "b"))

    assert [e.payload["text"] for e in log.read_all()] == ["a", "b"]
    assert log.skipped_lines == 1
    assert log.last_seq() == 2, "a non-integer seq is not mistaken for the tail"


def test_event_from_dict_rejects_what_would_break_the_sort() -> None:
    with pytest.raises(TypeError):
        event_from_dict({"seq": "7", "type": "note"})
    with pytest.raises(TypeError):
        event_from_dict([1, 2])  # type: ignore[arg-type]


# -- C3: the lessons file is split on newlines only ---------------------------


def _lesson(text: str, **kwargs: Any) -> Lesson:
    return Lesson(statement=text, target="architecture", workspace="/w", **kwargs)


def test_a_lesson_with_a_unicode_line_separator_survives_the_next_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    odd = "keep the brief short\u2028and the second sentence\u2029too\x85"
    store.add_lesson(_lesson(odd))
    assert [le.statement for le in store.lessons()] == [odd]

    store.add_lesson(_lesson("another lesson"))

    assert [le.statement for le in store.lessons()] == [odd, "another lesson"]


def test_rewriting_the_lessons_leaves_no_temporary_file_and_uses_a_unique_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    names: list[str] = []
    real = RunStore._replace_snapshot

    def spy(tmp: Path, path: Path) -> None:
        names.append(tmp.name)
        real(tmp, path)

    monkeypatch.setattr(RunStore, "_replace_snapshot", staticmethod(spy))
    store.add_lesson(_lesson("one"))
    store.add_lesson(_lesson("two"))

    assert len(names) == 2 and names[0] != names[1], "one temp name per write"
    assert "state" not in names[0]
    assert not list(store.root.glob(".lessons-*.tmp")), "and none is left behind"
    assert not (store.root / "lessons.jsonl.tmp").exists()


def test_a_rewrite_retries_a_rename_that_windows_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.add_lesson(_lesson("one"))
    real = Path.replace
    refused = {"n": 0}

    def flaky(self: Path, target: Any) -> Path:
        if self.name.startswith(".lessons-") and refused["n"] < 2:
            refused["n"] += 1
            raise PermissionError("held by a reader")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    monkeypatch.setattr(runstore, "SNAPSHOT_REPLACE_BACKOFF", 0.0)
    store.add_lesson(_lesson("two"))

    assert refused["n"] == 2
    assert [le.statement for le in store.lessons()] == ["one", "two"]


def _tool(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_case_extractor_reads_a_log_whose_text_holds_unicode_separators(
    tmp_path: Path,
) -> None:
    extract = _tool("extract_coordinator_cases")
    path = tmp_path / "events.jsonl"
    rows = [
        {"seq": 1, "type": "note", "payload": {"text": "a\u2028b"}},
        {"seq": 2, "type": "note", "payload": {"text": "c\u0085d"}},
        {"seq": "3", "type": "note"},      # a string seq: skipped, not fatal
        [1, 2],
    ]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")

    assert [e.payload["text"] for e in extract._events(path)] == ["a\u2028b", "c\u0085d"]


def test_the_time_tool_reads_a_log_with_a_torn_character(tmp_path: Path) -> None:
    tool = _tool("where_the_time_went")
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"ts": "2026-01-01T00:00:00Z", "type": "note"}\n'
                     b'{"ts": "caf\xc3"}\n'
                     b'{"ts": "2026-01-01T00:00:05Z", "type": "note"}\n')
    assert len(tool.read_log(path)) == 3


# -- C4: confidence decays by a step per run, not by a compounding amount -----


def test_decay_is_flat_across_repeated_passes() -> None:
    policy = Policy(lesson_decay_after_runs=10, lesson_decay_per_run=0.05,
                    lesson_confidence_floor=0.0)
    lesson = _lesson("stays true", confidence=1.0, runs_since_confirmed=10)
    seen = []
    for _ in range(6):
        consolidate([lesson], policy=policy, workspace="/w")
        seen.append(round(lesson.confidence, 2))
    # Runs 11-16 overdue: one 0.05 step each, not 0.05, 0.10, 0.15 ...
    assert seen == [0.95, 0.90, 0.85, 0.80, 0.75, 0.70]


def test_a_lesson_from_another_workspace_does_not_decay() -> None:
    policy = Policy(lesson_decay_after_runs=0, lesson_decay_per_run=0.2,
                    lesson_confidence_floor=0.0)
    lesson = _lesson("somewhere else", confidence=0.9, runs_since_confirmed=5)
    consolidate([lesson], policy=policy, workspace="/elsewhere")
    assert lesson.confidence == 0.9 and lesson.runs_since_confirmed == 5


def test_merging_repeats_respects_the_occurrence_cap() -> None:
    policy = Policy(lesson_max_occurrences=20)
    first = _lesson("the same thing", occurrences=15)
    second = _lesson("The same thing.", occurrences=15)
    kept, _ = consolidate([first, second], policy=policy, workspace="/w")
    assert len(kept) == 1 and kept[0].occurrences == 20


# -- C6: ids ------------------------------------------------------------------


def test_ids_do_not_collide_in_a_tight_loop() -> None:
    ids = [new_id("evt") for _ in range(200_000)]
    assert len(set(ids)) == len(ids)


def test_ids_keep_their_shape_and_sort_in_creation_order() -> None:
    ids = [new_id("run") for _ in range(2_000)]
    assert all(len(i) == len("run_") + 14 and i.startswith("run_") for i in ids)
    assert ids == sorted(ids)


def test_ids_stay_unique_when_the_clock_stands_still_or_steps_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from supervisor_harness import ids

    monkeypatch.setattr(ids.time, "time", lambda: 1_700_000_000.0)
    frozen = [new_id("x") for _ in range(5_000)]
    monkeypatch.setattr(ids.time, "time", lambda: 1_600_000_000.0)      # went backwards
    back = [new_id("x") for _ in range(5_000)]
    assert len(set(frozen + back)) == 10_000
    assert frozen == sorted(frozen) and back == sorted(back)


# -- C7: a stale-lock break never takes a live lock ---------------------------


def test_breaking_a_stale_lock_does_not_unlink_one_that_was_just_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.lock"
    path.write_text("1:oldholder", encoding="utf-8")
    old = time.time() - 3600
    os.utime(path, (old, old))
    contender = FileLock(path, stale_after=60.0)

    real_stat = Path.stat
    swapped = {"done": False}

    def racing_stat(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        result = real_stat(self, *args, **kwargs)
        if self == path and not swapped["done"]:
            swapped["done"] = True
            # The holder releases and somebody else acquires, right after the
            # contender saw how old the file was.
            path.unlink()
            path.write_text("2:livenewholder", encoding="utf-8")
        return result

    monkeypatch.setattr(Path, "stat", racing_stat)

    assert contender._break_if_stale() is False
    assert path.read_text(encoding="utf-8") == "2:livenewholder", "the live lock is untouched"


def test_a_genuinely_stale_lock_is_still_broken(tmp_path: Path) -> None:
    path = tmp_path / "events.lock"
    path.write_text("1:crashed", encoding="utf-8")
    old = time.time() - 3600
    os.utime(path, (old, old))
    assert FileLock(path, stale_after=60.0)._break_if_stale() is True
    assert not path.exists()


# -- C9: the snapshot is derived, and its failures stay out of the run --------


def test_a_temp_file_that_cannot_be_created_does_not_raise_from_emit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    session = store.create(RunState(id="run_C", prompt="p"))

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("no temp files here")

    monkeypatch.setattr(runstore.tempfile, "mkstemp", refuse)
    session.snapshot_every = 1
    session.note("still recorded")           # must not raise

    assert store.snapshot_error and "no temp files" in store.snapshot_error
    assert [n.text for n in session.state.notes] == ["still recorded"]


def test_a_failed_snapshot_is_retried_by_the_next_emit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    session = store.create(RunState(id="run_C", prompt="p"))
    session.snapshot_every = 1000
    real = RunStore.write_snapshot
    state = {"fail": True}

    def flaky(self: RunStore, run_id: str, payload: str) -> bool:
        if state["fail"]:
            self.snapshot_error = "disk full"
            return False
        return real(self, run_id, payload)

    monkeypatch.setattr(RunStore, "write_snapshot", flaky)
    session.emit(EventType.PHASE_CHANGED, {"phase": "analyzing"})        # an edge: fails
    state["fail"] = False
    session.flush_snapshot()

    assert store.load_state("run_C").phase.value == "analyzing"
    snap = json.loads((store.runs_dir / "run_C" / "state.json").read_text(encoding="utf-8"))
    assert snap["last_seq"] == session.state.last_seq


def test_the_status_report_carries_the_snapshot_error(tmp_path: Path) -> None:
    from supervisor_harness.config import default_config
    from supervisor_harness.core.reporting import Reporting

    store = _store(tmp_path)
    store.create(RunState(id="run_C", prompt="p"))
    reporting = Reporting(default_config(), store)
    assert reporting.status("run_C")["snapshot_error"] is None
    store.snapshot_error = "disk full"
    assert reporting.status("run_C")["snapshot_error"] == "disk full"


def test_open_removes_snapshot_temporaries_a_killed_writer_left(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(RunState(id="run_C", prompt="p"))
    directory = store.runs_dir / "run_C"
    stale = directory / ".state-dead.tmp"
    fresh = directory / ".state-live.tmp"
    stale.write_text("x", encoding="utf-8")
    fresh.write_text("x", encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale, (old, old))

    store.open("run_C")

    assert not stale.exists()
    assert fresh.exists(), "a writer that may still be running keeps its file"


# -- C10: reads do not create runs --------------------------------------------


def test_reading_an_unknown_run_creates_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for read in (lambda: store.read_result("run_nope", "x.json"),
                 lambda: store.run_file("run_nope", "results", "x.json"),
                 lambda: store.log("run_nope").read_all(),
                 lambda: store.log("run_nope").last_seq(),
                 lambda: store.exists("run_nope"),
                 lambda: store.load_state("run_nope")):
        with contextlib.suppress(FileNotFoundError, ValueError):
            read()
    assert list(store.runs_dir.iterdir()) == []


def test_writing_still_creates_the_run_directory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(RunState(id="run_W", prompt="p"))
    store.write_artifact("run_W", "report.md", "x")
    assert (store.runs_dir / "run_W" / "events.jsonl").is_file()


async def test_mcp_limits_are_clamped_to_at_least_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from supervisor_harness import mcp_server

    store = _store(tmp_path)
    for i in range(3):
        store.create(RunState(id=f"run_{i}", prompt=f"p{i}"))
        store.add_lesson(_lesson(f"lesson {i}"))

    class Stub:
        def __init__(self) -> None:
            self.store = store

    monkeypatch.setattr(mcp_server, "supervisor", lambda: Stub())
    server = mcp_server.build_server()

    async def call(name: str, **args: Any) -> dict[str, Any]:
        result = await server.call_tool(name, args)
        assert result.is_error is False, result.content
        return dict(result.structured_content or {})

    runs = await call("supervisor_runs", limit=-5)
    lessons = await call("supervisor_lessons", limit=-5)
    zero = await call("supervisor_lessons", limit=0)
    assert len(runs["runs"]) == 1
    assert len(lessons["lessons"]) == 1 and len(zero["lessons"]) == 1


# -- C11: explain --agent X does not warn about the other agents ---------------


def test_filtering_the_journal_to_one_agent_is_not_a_warning() -> None:
    state = RunState(id="run_J")
    state.agents["agt_a"] = AgentSpec(id="agt_a")
    state.agents["agt_b"] = AgentSpec(id="agt_b")
    events = [
        Event(seq=1, type=EventType.TURN_RECORDED,
              payload={"turn": to_jsonable(AgentTurn(agent_id="agt_b"))}),
        Event(seq=2, type=EventType.AGENT_STATUS,
              payload={"agent_id": "agt_b", "status": "done"}),
        Event(seq=3, type=EventType.TURN_RECORDED,
              payload={"turn": to_jsonable(AgentTurn(agent_id="agt_ghost"))}),
    ]
    journal = build_journal(state, events, "agt_a")
    assert journal.unattributed == 1, "only the agent this run never spawned"
    assert build_journal(state, events[:2], "agt_a").unattributed == 0


# -- C12: timestamps and progress lines ----------------------------------------


def test_elapsed_never_raises_on_mixed_naive_and_aware_stamps() -> None:
    assert _elapsed("2026-01-01T00:00:00", "2026-01-01T00:00:01Z") == 1.0
    assert _elapsed("2026-01-01T00:00:01Z", "2026-01-01T00:00:00") == 0.0
    assert _elapsed("junk", "2026-01-01T00:00:00Z") == 0.0
    from supervisor_harness.ids import parse_timestamp

    assert parse_timestamp("2026-01-01T00:00:00") == parse_timestamp("2026-01-01T00:00:00Z")
    assert parse_timestamp(None) is None


def test_progress_lines_end_in_a_bare_newline(tmp_path: Path) -> None:
    path = tmp_path / "progress.ndjson"
    progress.append(path, [_note(1), _note(2)])
    assert b"\r" not in path.read_bytes()
    assert path.read_bytes().count(b"\n") == 2


# -- C1 again, through the async path ------------------------------------------


async def test_two_async_sessions_on_one_run_do_not_hide_each_others_events(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.create(RunState(id="run_A", prompt="p"))
    first = store.open("run_A")
    second = store.open("run_A")
    await first.anote("from first")
    await second.anote("from second")
    second.flush_snapshot()
    await asyncio.sleep(0)

    assert [n.text for n in store.load_state("run_A").notes] == ["from first", "from second"]
    assert eventlog.EventLog is EventLog
