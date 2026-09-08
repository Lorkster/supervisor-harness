"""The offline timing tool, held to the promise it makes.

`tools/where_the_time_went.py` exists for a run on a machine whose data cannot
leave it, and its whole value rests on one claim: it prints durations, counts,
phase names and agent kinds, and **nothing else**. Someone reads the output down
a phone. If that claim were ever quietly false — a payload field creeping into a
line, a path in an error message — the tool would have exfiltrated the thing it
was written to avoid exfiltrating, and no behavioural test would have noticed.

So the first test here is the promise, not the arithmetic: a log stuffed with
secrets produces output containing none of them.

The rest is criterion 10 of [`docs/quality-standard.md`](../docs/quality-standard.md)
— an entry point nobody exercises is untested software — and the same reason
`tools/check_doc_refs.py` has `tests/test_doc_refs.py`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "where_the_time_went", ROOT / "tools" / "where_the_time_went.py"
)
assert _spec and _spec.loader
wtw = importlib.util.module_from_spec(_spec)
sys.modules["where_the_time_went"] = wtw
_spec.loader.exec_module(wtw)


def _event(type_: str, ts: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    return {"type": type_, "ts": ts, "payload": payload or {}}


def _log(tmp_path: Path, events: list[dict[str, object]]) -> Path:
    path = tmp_path / "events.jsonl"
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8"
    )
    return path


# -- the promise -----------------------------------------------------------


SECRETS = (
    "rewrite the authentication service for AcmeCorp",
    "src/acme/secret_ledger.py",
    "sk-live-9f3c2a1b",
    "the login handler leaks session tokens",
    "Add a per-IP counter to the payments gateway",
)


def test_nothing_from_a_payload_reaches_the_output(tmp_path: Path) -> None:
    """The claim the tool is for. A log full of the run's actual work, and an

    output that could be read out loud in an open-plan office.
    """
    events = [
        _event("run_created", "2026-09-08T10:00:00Z", {"run": {"prompt": SECRETS[0]}}),
        _event("phase_changed", "2026-09-08T10:00:01Z", {"phase": "analyzing"}),
        _event("brief_rendered", "2026-09-08T10:00:02Z",
               {"agent_id": "agt_1", "brief": f"# Brief\n{SECRETS[0]}\n{SECRETS[1]}"}),
        _event("agent_dispatched", "2026-09-08T10:00:03Z",
               {"agent_id": "agt_1", "kind": "analysis"}),
        _event("finding_added", "2026-09-08T10:01:00Z",
               {"finding": {"title": SECRETS[3], "evidence": [SECRETS[1]]}}),
        _event("turn_recorded", "2026-09-08T10:02:03Z", {"turn": {
            "agent_id": "agt_1", "output": SECRETS[2], "files_touched": [SECRETS[1]],
        }}),
        _event("task_proposed", "2026-09-08T10:02:10Z", {"task": {"title": SECRETS[4]}}),
    ]

    rendered = "\n".join(wtw.summarise(events))

    for secret in SECRETS:
        assert secret not in rendered, secret
    # And the parts of it that are not whole strings, either.
    for fragment in ("AcmeCorp", "secret_ledger", "sk-live", "payments"):
        assert fragment not in rendered, fragment


def test_the_output_is_only_numbers_stage_names_and_kinds(tmp_path: Path) -> None:
    """Stated as a whitelist, because "no secrets leaked" is only checkable

    against the secrets a test happened to plant. This is the stronger form: an
    unexpected word in the output fails, whatever it is.
    """
    events = [
        _event("phase_changed", "2026-09-08T10:00:00Z", {"phase": "analyzing"}),
        _event("agent_dispatched", "2026-09-08T10:00:01Z",
               {"agent_id": "agt_1", "kind": "analysis"}),
        _event("turn_recorded", "2026-09-08T10:00:05Z", {"turn": {"agent_id": "agt_1"}}),
    ]

    words = set()
    for line in wtw.summarise(events):
        words.update(w.strip(",()") for w in line.split())

    allowed = {
        "events", "total", "elapsed", "in", "dispatches", "across", "never", "answered",
        "by", "phase", "kind", "idle", "n", "slowest",
        "analyzing", "analysis",
    }
    unexpected = {
        w for w in words
        if w and w not in allowed and not w.replace(".", "").replace("s", "").isdigit()
    }

    assert not unexpected, f"unexpected words in the output: {sorted(unexpected)}"


# -- the arithmetic --------------------------------------------------------


def test_a_dispatch_is_measured_from_handout_to_answer() -> None:
    events = [
        _event("phase_changed", "2026-09-08T10:00:00Z", {"phase": "analyzing"}),
        _event("agent_dispatched", "2026-09-08T10:00:10Z",
               {"agent_id": "agt_1", "kind": "analysis"}),
        _event("turn_recorded", "2026-09-08T10:00:40Z", {"turn": {"agent_id": "agt_1"}}),
    ]

    rendered = "\n".join(wtw.summarise(events))

    assert "total elapsed         40.0s" in rendered
    assert "in dispatches         30.0s  across 1" in rendered
    assert "analyzing                40.0s       30.0s      10.0s" in rendered


def test_a_phase_far_above_its_dispatches_shows_as_idle() -> None:
    """The column the tool exists to produce.

    A phase busy with sub-agent work and one waiting on a host that went quiet
    are the same duration and different answers, and the difference is the
    whole question it was written to settle.
    """
    events = [
        _event("phase_changed", "2026-09-08T10:00:00Z", {"phase": "synthesizing"}),
        _event("agent_dispatched", "2026-09-08T10:00:00Z",
               {"agent_id": "agt_1", "kind": "synthesis"}),
        _event("turn_recorded", "2026-09-08T10:00:30Z", {"turn": {"agent_id": "agt_1"}}),
        _event("note", "2026-09-08T10:10:00Z", {"text": "nothing happened for nine minutes"}),
    ]

    line = next(le for le in wtw.summarise(events) if "synthesizing" in le)

    assert "600.0s" in line and "30.0s" in line and "570.0s" in line


def test_an_unanswered_dispatch_is_counted_rather_than_timed() -> None:
    """An agent that never reported would otherwise inflate the run for ever."""
    events = [
        _event("agent_dispatched", "2026-09-08T10:00:00Z",
               {"agent_id": "agt_gone", "kind": "analysis"}),
        _event("note", "2026-09-08T10:05:00Z"),
    ]

    rendered = "\n".join(wtw.summarise(events))

    assert "never answered    1" in rendered
    assert "across 0" in rendered


def test_a_packet_handed_out_twice_is_measured_from_the_re_issue() -> None:
    events = [
        _event("agent_dispatched", "2026-09-08T10:00:00Z",
               {"agent_id": "agt_1", "kind": "analysis"}),
        _event("agent_dispatched", "2026-09-08T10:00:50Z",
               {"agent_id": "agt_1", "kind": "analysis"}),
        _event("turn_recorded", "2026-09-08T10:01:00Z", {"turn": {"agent_id": "agt_1"}}),
    ]

    assert "in dispatches         10.0s  across 1" in "\n".join(wtw.summarise(events))


# -- surviving a log it was handed rather than one it made -----------------


def test_a_truncated_last_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    """The ordinary case: the log is append-only and may be copied mid-run."""
    path = _log(tmp_path, [_event("phase_changed", "2026-09-08T10:00:00Z", {"phase": "a"})])
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "turn_recorded", "ts": "2026-')

    assert len(wtw.read_log(path)) == 1


def test_a_malformed_timestamp_shortens_a_measurement_rather_than_raising() -> None:
    events = [
        _event("agent_dispatched", "not a timestamp", {"agent_id": "agt_1", "kind": "analysis"}),
        _event("turn_recorded", "2026-09-08T10:00:40Z", {"turn": {"agent_id": "agt_1"}}),
    ]

    assert "across 1" in "\n".join(wtw.summarise(events))


def test_an_event_type_this_tool_does_not_know_is_ignored() -> None:
    """It reads logs from builds newer than itself, and older ones."""
    events = [
        _event("phase_changed", "2026-09-08T10:00:00Z", {"phase": "analyzing"}),
        _event("some_future_event", "2026-09-08T10:00:10Z", {"anything": "at all"}),
    ]

    assert "analyzing" in "\n".join(wtw.summarise(events))


def test_an_empty_log_says_so() -> None:
    assert wtw.summarise([]) == ["the log is empty"]


# -- the entry point -------------------------------------------------------


def test_it_runs_over_a_real_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _log(tmp_path, [
        _event("phase_changed", "2026-09-08T10:00:00Z", {"phase": "analyzing"}),
        _event("agent_dispatched", "2026-09-08T10:00:01Z",
               {"agent_id": "agt_1", "kind": "analysis"}),
        _event("turn_recorded", "2026-09-08T10:00:11Z", {"turn": {"agent_id": "agt_1"}}),
    ])

    assert wtw.main(["where_the_time_went.py", str(path)]) == 0
    assert "in dispatches" in capsys.readouterr().out


def test_a_missing_file_is_refused_rather_than_raised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = wtw.main(["where_the_time_went.py", str(tmp_path / "nope.jsonl")])

    assert code == 2
    assert "no such event log" in capsys.readouterr().err


def test_no_argument_prints_how_to_call_it(capsys: pytest.CaptureFixture[str]) -> None:
    assert wtw.main(["where_the_time_went.py"]) == 2
    assert "events.jsonl" in capsys.readouterr().err


async def test_it_reads_a_log_this_harness_actually_wrote(tmp_path: Path) -> None:
    """The one test that would notice the event schema moving underneath it.

    Everything above builds its own events, which keeps the tool's arithmetic
    testable but would go on passing if `agent_dispatched` were renamed or
    `turn.agent_id` moved. This drives a real run and reads its real log.
    """
    from supervisor_harness.config import Policy, default_config
    from supervisor_harness.core.supervisor import Supervisor
    from supervisor_harness.host.detect import HostInfo
    from supervisor_harness.models import Backend, RunMode
    from supervisor_harness.store.runstore import RunStore

    config = default_config()
    config.backend = Backend.HOST
    config.routing = {k: "host" for k in config.routing}
    config.policy = Policy(default_max_turns=2, max_analysis_lenses=1)
    store = RunStore(tmp_path / ".supervisor")
    supervisor = Supervisor(
        workspace=tmp_path, config=config, store=store,
        host=HostInfo(name="claude-code", workspace=str(tmp_path), confidence=1.0),
    )
    started = await supervisor.start("Tidy the imports", mode=RunMode.REPORT)
    packet = started.packets[0]
    await supervisor.report(packet.run_id, packet.agent_id, {
        "restated_goal": "tidy imports", "mode": "report",
        "lenses": [{"role": "quality", "why": "style", "objectives": ["Read them"]}],
    })

    rendered = "\n".join(
        wtw.summarise(wtw.read_log(store.run_dir(started.run_id) / "events.jsonl"))
    )

    assert "across 1" in rendered, "the planning dispatch was not paired with its turn"
    assert "planning" in rendered
    assert "Tidy the imports" not in rendered, "the prompt is in the log and not in the output"
