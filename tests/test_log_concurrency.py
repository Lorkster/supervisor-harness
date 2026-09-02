"""Emission from the event loop: the open half of `fnd_01M130P3E5SCF8`.

The log's advisory lock is taken with a blocking sleep and the append ends in an
`fsync`, and both used to happen on the event loop. Agents gathered together
therefore did not overlap: one agent's bookkeeping stalled every other agent's
in-flight model call for the duration.

9a recorded why the obvious fix is wrong. Moving the whole emit to a thread
hands `RunState` to that thread, and the loop is free to mutate it meanwhile --
trading a latency problem for a data race. So the split is by what is touched:
`RunState` is only ever read or written on the loop, and only file I/O goes to a
thread, with an `asyncio.Lock` holding the sequence together.

These tests are about timing and interleaving, which a green suite is bad at
proving. Their limits are stated in each docstring.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from supervisor_harness.models import RunState
from supervisor_harness.store import eventlog
from supervisor_harness.store.events import EventType, fold
from supervisor_harness.store.runstore import RunStore


def _session(tmp_path: Path):
    store = RunStore(tmp_path / ".supervisor")
    return store, store.create(RunState(id="run_A", prompt="p", workspace=str(tmp_path)))


async def _ticks_during(session, patch, *, window: float = 0.15) -> int:
    """How many times the loop got scheduled while one emit ran under ``patch``.

    ``patch`` is a context manager that makes exactly one of the two blocking
    operations slow. Exactly one, deliberately: an earlier draft slowed both,
    and then either of them being off the loop was enough to satisfy the
    assertion -- so a snapshot write left on the loop would have passed.
    """
    ticks = 0
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(window / 30)
            ticks += 1

    spinner = asyncio.create_task(ticker())
    try:
        with patch:
            await session.aemit(EventType.NOTE, {"text": "one"})
    finally:
        stop.set()
        await spinner
    return ticks


class _slow:
    """Make one method of one class sleep, for the duration of a `with`."""

    def __init__(self, owner: type, name: str, seconds: float) -> None:
        self.owner, self.name, self.seconds = owner, name, seconds

    def __enter__(self) -> None:
        self.real = getattr(self.owner, self.name)
        seconds, real = self.seconds, self.real

        def slowed(*args, **kwargs):  # type: ignore[no-untyped-def]
            import time
            time.sleep(seconds)
            return real(*args, **kwargs)

        setattr(self.owner, self.name, slowed)

    def __exit__(self, *exc: object) -> None:
        setattr(self.owner, self.name, self.real)


async def test_a_slow_append_leaves_the_event_loop_free(tmp_path: Path) -> None:
    """The defect itself: a blocking append stalled every other coroutine.

    The bound is loose on purpose. This suite has already been bitten once by a
    timing assertion tight enough to fail on a loaded CI machine, so the ticker
    is given a 150ms window at 5ms intervals -- around thirty opportunities --
    and asked to prove it got three. What it cannot get, if the loop is held,
    is any.
    """
    _, session = _session(tmp_path)
    ticks = await _ticks_during(session, _slow(eventlog.EventLog, "append_many", 0.15))

    assert ticks >= 3, f"the loop was scheduled {ticks} times during a slow append"
    assert [n.text for n in session.state.notes] == ["one"]


async def test_a_slow_snapshot_write_leaves_the_event_loop_free(tmp_path: Path) -> None:
    """The second blocking half, which is easy to leave behind on the loop."""
    store, session = _session(tmp_path)
    ticks = await _ticks_during(session, _slow(type(store), "write_snapshot", 0.15))

    assert ticks >= 3, f"the loop was scheduled {ticks} times during a slow snapshot"
    assert [n.text for n in session.state.notes] == ["one"]


async def test_an_emit_holds_the_session_lock_across_its_await_points(
    tmp_path: Path,
) -> None:
    """A guard on the mechanism, and deliberately not a test of an interleaving.

    Two behavioural versions of this were written and both thrown away, which is
    worth recording because the next person will reach for the same ones.

    The first made one snapshot write slow so that a stale payload would land
    last -- but which coroutine got the slow write was decided by a race between
    two worker threads, so it was a coin flip that passed either way. The second
    watched how much of the log each snapshot was written beside, expecting
    serialised emits to see it grow one event at a time -- but whether the
    second append has landed by the time the first write starts is itself a
    race, so it passed with the lock removed.

    Concurrency is like that: the interleaving that breaks it is the one the
    test cannot reliably produce. So this asserts the mechanism instead. While a
    snapshot is being written -- the last await in an emit -- the session's lock
    is held, which is exactly the statement that no second emit can be part-way
    through at the same time.
    """
    store, session = _session(tmp_path)
    real_write = type(store).write_snapshot
    held: list[bool] = []

    def observing(self, run_id, payload):  # type: ignore[no-untyped-def]
        lock = session._alock
        held.append(lock is not None and lock.locked())
        return real_write(self, run_id, payload)

    type(store).write_snapshot = observing
    try:
        await asyncio.gather(
            session.aemit(EventType.NOTE, {"text": "first"}),
            session.aemit(EventType.NOTE, {"text": "second"}),
        )
    finally:
        type(store).write_snapshot = real_write

    assert held == [True, True], (
        "an emit reached its snapshot write without holding the session lock; "
        "another emit can interleave with it"
    )
    on_disk = store.load_state("run_A")
    assert [n.text for n in on_disk.notes] == ["first", "second"]


async def test_many_concurrent_emits_lose_nothing(tmp_path: Path) -> None:
    """The bulk case: sequences stay unique and the fold keeps every event."""
    store, session = _session(tmp_path)
    count = 24

    await asyncio.gather(*(
        session.aemit(EventType.NOTE, {"text": f"note {i}"}) for i in range(count)
    ))

    events = store.log("run_A").read_all()
    notes = [e for e in events if e.type is EventType.NOTE]
    assert len(notes) == count
    assert len({e.seq for e in events}) == len(events), "a sequence was handed out twice"
    assert len(session.state.notes) == count, "an event was folded over another"

    on_disk = store.load_state("run_A")
    assert on_disk.last_seq == max(e.seq for e in events)
    assert len(on_disk.notes) == count


async def test_the_state_a_thread_sees_is_never_the_live_one(tmp_path: Path) -> None:
    """A guard on the split, not a proof of thread safety.

    What makes offloading safe is that the thread is handed already-encoded
    work: a list of `Event` objects for the append, and a serialised string for
    the snapshot. Neither is `RunState`. If someone later passes the state
    itself to `to_thread` for convenience, this fails -- which is the only
    warning the suite can offer, since the race it would reintroduce is timing
    dependent and would not show up reliably.
    """
    _, session = _session(tmp_path)
    seen: list[object] = []
    real = asyncio.to_thread

    async def recording(func, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen.extend(args)
        return await real(func, *args, **kwargs)

    asyncio.to_thread = recording  # type: ignore[assignment]
    try:
        await session.aemit(EventType.NOTE, {"text": "one"})
    finally:
        asyncio.to_thread = real  # type: ignore[assignment]

    assert seen, "nothing was offloaded at all"
    assert not any(isinstance(arg, RunState) for arg in seen), (
        "RunState was handed to a worker thread; the loop is free to mutate it there"
    )


async def test_a_run_driven_through_the_async_path_still_folds_identically(
    tmp_path: Path,
) -> None:
    """Async emission must produce the same log a synchronous one would.

    The two paths write through different code now, and the log is the thing
    every other guarantee rests on, so the cheapest useful check is that a
    replay of what the async path wrote reproduces the state it left behind.
    """
    store, session = _session(tmp_path)
    await session.aemit(EventType.PHASE_CHANGED, {"phase": "analyzing"})
    await session.aemit_many([
        (EventType.NOTE, {"text": "a"}, "supervisor"),
        (EventType.NOTE, {"text": "b"}, "supervisor"),
    ])
    session.emit(EventType.NOTE, {"text": "c"})  # the synchronous path, interleaved

    replayed = fold(store.log("run_A").read_all())
    assert [n.text for n in replayed.notes] == ["a", "b", "c"]
    assert str(replayed.phase) == "analyzing"
    assert replayed.last_seq == session.state.last_seq
