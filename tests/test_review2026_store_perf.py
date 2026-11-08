"""Performance regressions found by the 2026-11 review, asserted structurally.

Wall-clock thresholds flake on a loaded CI machine, so every test here counts
*work* -- calls, reads, parsed events -- which does not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness import serde
from supervisor_harness.config import default_config
from supervisor_harness.core.reporting import Reporting
from supervisor_harness.models import (
    AgentSpec,
    AgentTurn,
    Finding,
    Message,
    RunState,
    Usage,
)
from supervisor_harness.serde import from_jsonable, to_jsonable
from supervisor_harness.store import eventlog, runstore
from supervisor_harness.store.events import Event, EventType, fold
from supervisor_harness.store.runstore import RunStore


def _store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / ".supervisor")


def _finding_event(i: int, run_id: str = "run_P") -> Event:
    finding = Finding(id=f"fnd_{i}", agent_id="agt_1", lens="l", title=f"finding {i}")
    return Event(seq=i + 2, run_id=run_id, type=EventType.FINDING_ADDED,
                 payload={"finding": to_jsonable(finding)})


# -- P2: type hints are resolved once per class ------------------------------


def test_from_jsonable_resolves_type_hints_once_per_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    real = serde.get_type_hints

    def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    serde._field_plan.cache_clear()
    monkeypatch.setattr(serde, "get_type_hints", counting)
    raw = to_jsonable(Finding(id="fnd_1", title="t"))
    for _ in range(50):
        from_jsonable(raw, Finding)
    assert calls <= 2, f"get_type_hints ran {calls} times for 50 instances of one class"


def test_cached_field_plan_still_round_trips_nested_types() -> None:
    turn = AgentTurn(agent_id="agt_1", findings=[Finding(id="fnd_1", title="x")],
                     messages=[Message(id="msg_1", subject="s")], usage=Usage(input_tokens=7))
    again = from_jsonable(json.loads(json.dumps(to_jsonable(turn))), AgentTurn)
    assert again == turn


# -- P3: the fold does not rescan its lists ----------------------------------


class _CountingList(list[Any]):
    """A list that counts how often it is walked, to tell a scan from an index."""

    iterations = 0

    def __iter__(self) -> Any:
        type(self).iterations += 1
        return super().__iter__()


def test_fold_does_not_scan_the_findings_list_per_event() -> None:
    _CountingList.iterations = 0
    state = RunState(id="run_P")
    state.findings = _CountingList()
    events = [_finding_event(i) for i in range(300)]
    folded = fold(events, initial=state)
    assert len(folded.findings) == 300
    assert _CountingList.iterations <= 3, (
        f"the findings list was walked {_CountingList.iterations} times for 300 events"
    )


def test_fold_output_is_unchanged_by_the_index() -> None:
    """Replays and repeated ids still replace in place, in first-seen order."""
    events = [_finding_event(i) for i in range(5)]
    again = Event(seq=99, run_id="run_P", type=EventType.FINDING_ADDED,
                  payload={"finding": to_jsonable(
                      Finding(id="fnd_2", agent_id="agt_1", lens="l", title="rewritten"))})
    state = fold([*events, *events, again])
    assert [f.id for f in state.findings] == [f"fnd_{i}" for i in range(5)]
    assert state.findings[2].title == "rewritten"


def test_fold_skips_the_sort_when_events_arrive_ordered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from supervisor_harness.store import events as events_mod

    calls = 0
    real = sorted

    def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(events_mod, "sorted", counting, raising=False)
    fold([_finding_event(i) for i in range(10)])
    assert calls == 0
    fold([_finding_event(2), _finding_event(1)])     # out of order still gets sorted
    assert calls == 1


def test_fact_artifact_and_delivery_branches_keep_their_semantics() -> None:
    from supervisor_harness.models import Fact

    fact = to_jsonable(Fact(key="k", statement="s", agent_id="a"))
    msg = Message(id="msg_1", recipient="agt_2")
    events = [
        Event(seq=1, type=EventType.FACT_ESTABLISHED, payload={"fact": fact}),
        Event(seq=2, type=EventType.FACT_ESTABLISHED, payload={"fact": fact}),
        Event(seq=3, type=EventType.MESSAGE_SENT, payload={"message": to_jsonable(msg)}),
        Event(seq=4, type=EventType.MESSAGE_DELIVERED,
              payload={"message_ids": ["msg_1", "nope"], "agent_id": "agt_2"}),
        Event(seq=5, type=EventType.MESSAGE_DELIVERED,
              payload={"message_ids": ["msg_1"], "agent_id": "agt_2"}),
        Event(seq=6, type=EventType.ARTIFACT_WRITTEN, payload={"path": "a.md", "kind": "report"}),
        Event(seq=7, type=EventType.ARTIFACT_WRITTEN, payload={"path": "b.md", "kind": "report"}),
        Event(seq=8, type=EventType.ARTIFACT_WRITTEN, payload={"path": "a.md", "kind": "report"}),
    ]
    state = fold(events)
    assert len(state.established) == 1
    assert state.messages[0].delivered_to == ["agt_2"]
    assert [a.path for a in state.artifacts] == ["b.md", "a.md"]


# -- P1 / C1: snapshots at intervals, never ahead of what was folded ----------


def _count_snapshot_writes(store: RunStore, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    writes: list[int] = []
    real = RunStore.write_snapshot

    def counting(self: RunStore, run_id: str, payload: str) -> bool:
        writes.append(len(payload))
        return real(self, run_id, payload)

    monkeypatch.setattr(RunStore, "write_snapshot", counting)
    return writes


def test_emit_does_not_rewrite_the_snapshot_every_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    session = store.create(RunState(id="run_P", prompt="p"))
    writes = _count_snapshot_writes(store, monkeypatch)
    for i in range(250):
        session.note(f"n{i}")
    assert len(writes) <= 250 // runstore.SNAPSHOT_EVERY + 1, len(writes)
    assert len(writes) >= 1, "it still snapshots on an interval"


def test_snapshot_is_written_at_phase_edges_and_by_sync_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from supervisor_harness.models import Phase

    store = _store(tmp_path)
    session = store.create(RunState(id="run_P", prompt="p"))
    writes = _count_snapshot_writes(store, monkeypatch)
    session.emit(EventType.PHASE_CHANGED, {"phase": str(Phase.ANALYZING)})
    assert len(writes) == 1
    session.note("x")
    assert len(writes) == 1
    session.sync_index()
    assert len(writes) == 2, "the end of a phase flushes the pending snapshot"


def test_snapshot_is_compact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create(RunState(id="run_P", prompt="p"))
    session.note("x")
    text = (store.runs_dir / "run_P" / "state.json").read_text(encoding="utf-8")
    assert "\n" not in text.strip(), "indented JSON roughly doubles the file for nothing"


def test_a_stale_snapshot_is_completed_from_the_log_tail(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create(RunState(id="run_P", prompt="p"))
    for i in range(30):
        session.note(f"n{i}")           # far fewer than SNAPSHOT_EVERY: snapshot is behind
    snap = json.loads((store.runs_dir / "run_P" / "state.json").read_text(encoding="utf-8"))
    assert snap["last_seq"] < 31, "the snapshot is behind the log by design"

    loaded = store.load_state("run_P")
    assert loaded.last_seq == 31
    assert [n.text for n in loaded.notes] == [f"n{i}" for i in range(30)]
    cold = fold(store.log("run_P").read_all())
    cold.id = "run_P"
    assert to_jsonable(loaded) == to_jsonable(cold)


def test_two_sessions_on_one_run_do_not_hide_each_others_events(tmp_path: Path) -> None:
    """C1: a session's snapshot used to claim events it had never folded."""
    store = _store(tmp_path)
    store.create(RunState(id="run_P", prompt="p"))
    a = store.open("run_P")
    b = store.open("run_P")
    a.note("from A")
    b.note("from B")
    b.sync_index()                       # forces B's snapshot out
    texts = [n.text for n in store.load_state("run_P").notes]
    assert texts == ["from A", "from B"]
    assert [n.text for n in b.state.notes] == ["from A", "from B"], "B caught up from the log"


def test_open_uses_the_snapshot_and_reads_only_the_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    session = store.create(RunState(id="run_P", prompt="p"))
    for i in range(40):
        session.note(f"n{i}")
    session.sync_index()                 # snapshot now current
    for i in range(5):
        session.note(f"late{i}")         # five events past the snapshot

    parsed = 0
    real = eventlog.event_from_dict

    def counting(data: Any) -> Any:
        nonlocal parsed
        parsed += 1
        return real(data)

    monkeypatch.setattr(eventlog, "event_from_dict", counting)
    reopened = store.open("run_P")
    assert len(reopened.state.notes) == 45
    assert parsed <= 6, f"open parsed {parsed} events instead of the five past the snapshot"


def test_sync_index_does_not_reread_the_whole_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    session = store.create(RunState(id="run_P", prompt="p"))
    for i in range(30):
        session.note(f"n{i}")
    session.sync_index()
    for i in range(3):
        session.note(f"late{i}")

    reads = 0
    real = eventlog.EventLog.read_all

    def counting(self: eventlog.EventLog) -> list[Event]:
        nonlocal reads
        reads += 1
        return real(self)

    monkeypatch.setattr(eventlog.EventLog, "read_all", counting)
    session.sync_index()
    assert reads == 0
    rows = store.index().query("SELECT COUNT(*) AS n FROM events WHERE run_id = 'run_P'")
    assert rows[0]["n"] == 34, "the index still holds every event"


# -- P5: a ledger after one more event parses one event -----------------------


def test_ledger_is_incremental(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    session = store.create(RunState(id="run_P", prompt="p"))
    session.emit(EventType.AGENT_SPAWNED, {"agent": to_jsonable(AgentSpec(id="agt_1"))})
    for i in range(60):
        session.note(f"n{i}")
    reporting = Reporting(default_config(), store)
    first = reporting.ledger("run_P")

    parsed = 0
    real = eventlog.event_from_dict

    def counting(data: Any) -> Any:
        nonlocal parsed
        parsed += 1
        return real(data)

    monkeypatch.setattr(eventlog, "event_from_dict", counting)
    assert reporting.ledger("run_P") == first
    assert parsed == 0, "an unchanged log is not parsed again"
    session.note("one more")
    reporting.ledger("run_P")
    reporting.status("run_P")
    assert parsed == 1, f"{parsed} events parsed for one new event"


def test_status_and_ledger_agree_with_a_cold_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from supervisor_harness.core import timing

    monkeypatch.setattr(timing, "now_iso", lambda: "2031-01-01T00:00:00.000Z")
    store = _store(tmp_path)
    session = store.create(RunState(id="run_P", prompt="p"))
    session.emit(EventType.AGENT_SPAWNED, {"agent": to_jsonable(AgentSpec(id="agt_1"))})
    warm = Reporting(default_config(), store)
    warm.ledger("run_P")
    for i in range(5):
        session.emit(EventType.AGENT_DISPATCHED, {"agent_id": "agt_1", "kind": "analysis"})
        session.note(f"n{i}")
        assert warm.ledger("run_P") == Reporting(default_config(), store).ledger("run_P")
    a = warm.status("run_P")
    b = Reporting(default_config(), store).status("run_P")
    for key in ("timing", "updated_at"):
        a.pop(key), b.pop(key)
    assert a == b


# -- P6: refreshing the index touches only runs whose log moved ---------------


def test_refreshing_the_index_skips_runs_that_did_not_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    for rid in ("run_A", "run_B"):
        s = store.create(RunState(id=rid, prompt=f"prompt {rid}"))
        for i in range(10):
            s.note(f"n{i}")
    assert store.refresh_index() == 2
    assert store.refresh_index() == 0, "nothing changed, nothing was synced"

    parsed = 0
    real = eventlog.event_from_dict

    def counting(data: Any) -> Any:
        nonlocal parsed
        parsed += 1
        return real(data)

    monkeypatch.setattr(eventlog, "event_from_dict", counting)
    later = store.open("run_B")
    later.note("more")
    later.flush_snapshot()             # what the end of a phase does
    parsed = 0
    assert store.refresh_index() == 1
    assert parsed <= 3, f"{parsed} events parsed to add one"
    rows = store.index().query("SELECT COUNT(*) AS n FROM events WHERE run_id = 'run_B'")
    assert rows[0]["n"] == 12
    assert store.index().list_runs()[0]["id"] in {"run_A", "run_B"}


def test_reindex_still_rebuilds_event_rows_from_scratch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    s = store.create(RunState(id="run_A", prompt="p"))
    s.note("x")
    store.reindex()
    conn = store.index()._conn
    with conn:
        conn.execute("INSERT INTO events (id, run_id, seq, type, actor, ts) "
                     "VALUES ('ghost', 'run_A', 99, 'note', 'x', '')")
    store.reindex(["run_A"])
    ids = {r["id"] for r in store.index().query("SELECT id FROM events WHERE run_id='run_A'")}
    assert "ghost" not in ids


# -- P7: contradiction detection tokenises each finding once -------------------


def test_detect_contradictions_tokenises_each_finding_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from supervisor_harness.core import blackboard, drift

    calls = 0
    real = drift.tokens

    def counting(text: str) -> Any:
        nonlocal calls
        calls += 1
        return real(text)

    monkeypatch.setattr(drift, "tokens", counting)
    findings = [
        Finding(id=f"f{i}", lens="a" if i % 2 else "b", title=f"cookie handling {i}",
                detail="the cookie is secure and validated", confidence=0.9)
        for i in range(40)
    ]
    blackboard.detect_contradictions(findings)
    assert calls <= len(findings), f"tokens() ran {calls} times for {len(findings)} findings"


def test_detect_contradictions_results_are_unchanged() -> None:
    from supervisor_harness.core.blackboard import detect_contradictions

    pro = Finding(id="f1", lens="sec", title="session cookie is secure and validated",
                  detail="session cookie handling is correct", confidence=0.9)
    con = Finding(id="f2", lens="qa", title="session cookie is not secure",
                  detail="session cookie handling is broken and unvalidated", confidence=0.9)
    other = Finding(id="f3", lens="ops", title="session cookie is not secure",
                    detail="session cookie handling is broken", confidence=0.9)
    low = Finding(id="f4", lens="perf", title="session cookie is not secure",
                  detail="session cookie handling is broken", confidence=0.1)
    out = detect_contradictions([pro, con, other, low])
    assert len(out) == 2, "pro clashes with con and with other; low confidence is ignored"
    assert "`sec` says" in out[0] and "`qa` says" in out[0]
    assert "`ops` says" in out[1]
