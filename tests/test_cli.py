"""CLI-level tests: the event reader and the failure surface of main()."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from supervisor_harness.cli import main
from supervisor_harness.models import RunState
from supervisor_harness.store.events import EventType
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
