"""CLI-level tests: the event reader and the failure surface of main()."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from supervisor_harness.cli import main
from supervisor_harness.models import RunState
from supervisor_harness.store.events import Event, EventType
from supervisor_harness.store.runstore import RunStore


@pytest.fixture
def run_with_notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A small on-disk run whose log carries notes among other events."""
    monkeypatch.delenv("SUPERVISOR_HOME", raising=False)
    store = RunStore(tmp_path / ".supervisor")
    session = store.create(RunState(prompt="harden login", workspace=str(tmp_path)))
    session.note("run created", host="test-host")
    session.emit(EventType.PHASE_CHANGED, {"phase": "analyzing"})
    session.note("agent failed: provider timed out", actor="agt_TEST")
    return session.state.id


def test_events_command_prints_the_notes_of_a_run(
    run_with_notes: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["events", run_with_notes, "-w", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "run created" in out
    assert "agent failed: provider timed out" in out
    assert "phase_changed" in out


def test_events_command_filters_by_type_and_sequence(
    run_with_notes: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["events", run_with_notes, "--type", "note", "-w", str(tmp_path)]) == 0
    notes = capsys.readouterr().out
    assert "agent failed: provider timed out" in notes
    assert "phase_changed" not in notes

    assert main(["events", run_with_notes, "--since", "3", "-w", str(tmp_path)]) == 0
    tail = capsys.readouterr().out
    assert "agent failed: provider timed out" in tail
    assert "run created" not in tail


def test_events_command_emits_payloads_as_json(
    run_with_notes: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["events", run_with_notes, "--type", "note", "--json",
                 "-w", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["run_id"] == run_with_notes
    texts = [event["payload"]["text"] for event in payload["events"]]
    assert texts == ["run created", "agent failed: provider timed out"]
    assert payload["events"][-1]["actor"] == "agt_TEST"


def test_an_unrecognised_event_type_is_shown_and_counted(
    run_with_notes: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Neither the log line nor the fact that nothing projects it is hidden."""
    store = RunStore(tmp_path / ".supervisor")
    store.log(run_with_notes).append(
        Event(run_id=run_with_notes, type="future_event", payload={"detail": "from a newer build"})
    )
    # The event went straight into the log, as one written by another build
    # would have; drop the snapshot so the state is rebuilt from the log.
    (store.runs_dir / run_with_notes / "state.json").unlink(missing_ok=True)

    assert main(["events", run_with_notes, "-w", str(tmp_path)]) == 0
    listing = capsys.readouterr().out
    assert "future_event" in listing, "the line was dropped, or shown as the sentinel"

    # The sentinel is what it is filed under, so one filter finds them all.
    assert main(["events", run_with_notes, "--type", "unknown", "-w", str(tmp_path)]) == 0
    filtered = capsys.readouterr().out
    assert "future_event" in filtered
    assert "phase_changed" not in filtered

    assert main(["status", run_with_notes, "-w", str(tmp_path)]) == 0
    assert "unhandled event types: future_event" in capsys.readouterr().out


def test_events_command_rejects_an_unknown_run_and_type(
    run_with_notes: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["events", "run_NOPE", "-w", str(tmp_path)]) == 2
    assert "no such run" in capsys.readouterr().err
    assert not (tmp_path / ".supervisor" / "runs" / "run_NOPE").exists()

    assert main(["events", run_with_notes, "--type", "notes", "-w", str(tmp_path)]) == 2
    assert "unknown event type" in capsys.readouterr().err


def test_debug_flag_re_raises_instead_of_printing_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_args: object) -> int:
        raise RuntimeError("provider exploded")

    monkeypatch.setattr("supervisor_harness.cli.cmd_runs", boom)
    monkeypatch.setattr("supervisor_harness.cli.cmd_events", boom)

    assert main(["runs", "-w", str(tmp_path)]) == 1
    assert "error: provider exploded" in capsys.readouterr().err

    with pytest.raises(RuntimeError, match="provider exploded"):
        main(["events", "-w", str(tmp_path), "--debug"])


# --------------------------------------------------------------------------
# Declared host agents, and the drift command
# --------------------------------------------------------------------------


def _registry(declared: list) -> list[str]:
    from supervisor_harness.agents.registry import AgentRegistry
    from supervisor_harness.host.detect import HostInfo

    return AgentRegistry(Path("."), HostInfo(name="claude-code"), declared).spawnable_names()


def test_a_declared_agent_may_be_written_as_a_bare_name() -> None:
    """`--host-agents '["general-purpose"]'` is the shape people write, and it
    used to raise AttributeError from inside the registry comprehension."""
    assert _registry(["general-purpose", "Explore"]) == ["general-purpose", "Explore"]
    assert _registry([{"name": "general-purpose"}]) == ["general-purpose"]
    assert _registry(["Explore", {"name": "general-purpose"}]) == [
        "Explore", "general-purpose",
    ]


def test_an_entry_that_names_no_agent_is_dropped_rather_than_invented() -> None:
    """The name is what the host is later asked to spawn, so an entry without
    one must not become a sub-agent type called 'agent'."""
    assert _registry([None, 7, "", {"description": "no name"}, {"name": "real"}]) == ["real"]


def test_host_agents_that_is_not_a_json_array_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["start", "harden login", "--host-agents", "{oops", "-w", str(tmp_path)]) == 2
    assert "--host-agents is not valid JSON" in capsys.readouterr().err

    assert main(["start", "harden login", "--host-agents", '{"name": "a"}',
                 "-w", str(tmp_path)]) == 2
    assert "must be a JSON array" in capsys.readouterr().err


def test_drift_command_reports_an_agent_with_nothing_to_assess(
    run_with_notes: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`supervisor_check_drift` had no CLI equivalent at all; it now has one,
    and it fails the way the other run-scoped commands do."""
    assert main(["drift", "agt_TEST", run_with_notes, "-w", str(tmp_path)]) == 1
    assert "no assessment to escalate" in capsys.readouterr().err


def test_drift_command_defaults_to_the_most_recent_run(
    run_with_notes: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["drift", "agt_TEST", "-w", str(tmp_path)]) == 1
    assert run_with_notes in capsys.readouterr().err


def test_drift_command_says_so_when_there_is_no_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["drift", "agt_TEST", "-w", str(tmp_path)]) == 2
    assert "no runs found" in capsys.readouterr().err
