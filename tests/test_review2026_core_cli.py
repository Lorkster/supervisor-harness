"""CLI behaviour found wrong in the 2026-11 review (findings 1, 7, 14)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from supervisor_harness.cli import _utf8_streams, main

from .conftest import FakeProvider
from .test_cli_commands import PROMPT, _answer_for, json_cli, run_cli
from .test_cli_commands import cwd as cwd


def test_stdin_is_read_as_utf8_whatever_the_console_code_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`supervisor report` reads a turn from stdin; cp1252 turned → å ä ö to mojibake."""
    raw = "{\"output\": \"a → b, å ä ö\"}".encode()
    stdin = io.TextIOWrapper(io.BytesIO(raw + b"\xff"), encoding="cp1252")
    monkeypatch.setattr(sys, "stdin", stdin)

    _utf8_streams()
    text = sys.stdin.read()

    assert "a → b, å ä ö" in text
    assert "\udcff" not in text, "an invalid byte must not become a lone surrogate"


def _reach_approval(cwd: Path, fake: FakeProvider, capsys, monkeypatch) -> dict:
    response = json_cli(cwd, capsys, "start", PROMPT)
    for _ in range(60):
        if response.get("action") == "await_approval":
            return response
        if response.get("action") == "dispatch":
            last = response
            for packet in response["packets"]:
                monkeypatch.setattr(
                    "sys.stdin", io.StringIO(json.dumps(_answer_for(fake, packet)))
                )
                last = json_cli(cwd, capsys, "report", packet["run_id"], packet["agent_id"])
            response = (
                last if last.get("action") in ("await_approval", "dispatch")
                else json_cli(cwd, capsys, "advance", response["run_id"])
            )
            continue
        response = json_cli(cwd, capsys, "advance", response["run_id"])
    pytest.fail("never reached approval")


def test_approve_refuses_a_decision_it_does_not_know(
    cwd: Path, fake: FakeProvider, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _reach_approval(cwd, fake, capsys, monkeypatch)
    task_id = response["tasks"][0]["id"]
    capsys.readouterr()

    code = main(["approve", response["run_id"], "--task", f"{task_id}:rejct", "-w", str(cwd)])

    assert code != 0
    assert "rejct" in capsys.readouterr().err
    status = json_cli(cwd, capsys, "status", response["run_id"])
    assert all(t["status"] == "proposed" for t in status["tasks"]), "nothing was decided"


def test_mcp_passes_the_workspace_flag_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    seen: dict[str, str] = {}
    monkeypatch.setenv("SUPERVISOR_WORKSPACE", "")   # restored on teardown
    monkeypatch.setattr("supervisor_harness.mcp_server.main",
                        lambda: seen.update(ws=os.environ.get("SUPERVISOR_WORKSPACE", "")))

    assert main(["mcp", "-w", str(tmp_path)]) == 0

    assert Path(seen["ws"]) == tmp_path.resolve()


def test_report_and_abandon_for_an_unknown_agent_exit_non_zero(
    cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    started = json_cli(cwd, capsys, "start", PROMPT)
    run_id = started["run_id"]
    monkeypatch.setattr("sys.stdin", io.StringIO('{"output": "x", "status": "done"}'))

    assert run_cli(cwd, "report", run_id, "agt_nobody") == 1
    assert run_cli(cwd, "abandon", "agt_nobody", run_id) == 1
    assert "nothing was recorded" in capsys.readouterr().err


def test_no_runs_is_a_usage_error_everywhere(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (cwd / ".supervisor").mkdir()
    for command in (["status"], ["explain"], ["audit"], ["findings"], ["trajectory"],
                    ["escalations"], ["events"], ["advance"], ["abandon", "agt_x"],
                    ["approve", "--all"], ["resume"]):
        assert run_cli(cwd, *command) == 2, command
    capsys.readouterr()


def test_read_only_commands_do_not_create_the_store(
    cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert not (cwd / ".supervisor").exists()
    monkeypatch.setattr("supervisor_harness.providers.router.ModelRouter.health",
                        _no_health)

    assert run_cli(cwd, "status") == 2
    assert run_cli(cwd, "providers") == 0

    assert not (cwd / ".supervisor").exists()
    capsys.readouterr()


async def _no_health(self) -> dict:
    return {}
