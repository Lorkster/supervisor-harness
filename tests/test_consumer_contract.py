"""What security-eval reads from this harness, held as a contract.

[security-eval](https://github.com/Lorkster/security-eval) runs this harness as
one condition of a study and imports its provider layer for another. It reads
only published surfaces -- the command line with ``--json``, a handful of
provider-layer names, two environment variables and the trusted config path --
and it pins a commit, so a change here reaches it only when the group moves the
pin. Until this file, nothing in this repository failed when one of those
surfaces changed. The first anyone would have heard was a study cell reading
an empty field as a real result.

Each test reads the harness the way security-eval's own code does
(`runners/harness.py`, `runners/baseline.py`, `batch.py`, `preflight.py`), so
the assertions name the fields it uses rather than everything the harness
happens to emit. A test here failing means that consumer is about to break. Fix
the harness, or change the contract deliberately and tell the group.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness import cli
from supervisor_harness.config import HarnessConfig, load_config
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.host.detect import HostInfo
from supervisor_harness.models import EndCause
from supervisor_harness.providers.base import (
    DEFAULT_MAX_TOKENS,
    ChatMessage,
    CompletionRequest,
    CompletionResponse,
    ProviderRefusal,
    schema_instruction,
)
from supervisor_harness.providers.router import ModelRouter
from supervisor_harness.store.runstore import RunStore

from .conftest import FakeProvider

TASK = "Review src/auth for security vulnerabilities and report each with its location"


def _cli_supervisor(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, config: HarnessConfig, provider: FakeProvider
) -> None:
    """Every CLI command in this file builds its supervisor against ``provider``.

    The CLI makes a fresh `Supervisor` per command and closes it after, so the
    factory does the same: what is shared between commands is the store on
    disk, which is exactly what security-eval shares between its calls.
    """

    def make(args: Any) -> Supervisor:
        host = HostInfo(name="test-host", workspace=str(workspace), confidence=1.0)
        router = ModelRouter(config, host_name=host.name)
        router.register("fake", provider)
        return Supervisor(workspace=workspace, config=config,
                          store=RunStore(workspace / ".supervisor"), host=host, router=router)

    monkeypatch.setattr(cli, "_supervisor", make)
    monkeypatch.delenv("SUPERVISOR_HOME", raising=False)


def _json(capsys: pytest.CaptureFixture[str], argv: list[str]) -> dict[str, Any]:
    capsys.readouterr()
    code = cli.main(argv)
    out = capsys.readouterr().out
    assert code == 0, f"`supervisor {argv[0]}` exited {code}: {out[-500:]}"
    data = json.loads(out)
    assert isinstance(data, dict), f"`supervisor {argv[0]} --json` must print one object"
    return data


def _report_run(capsys: pytest.CaptureFixture[str], workspace: Path) -> dict[str, Any]:
    """The exact argv security-eval's harness runner starts a cell with."""
    return _json(capsys, ["run", TASK, "--mode", "report", "--backend", "autonomous",
                          "--yes", "--json", "-w", str(workspace)])


# -- the command line ----------------------------------------------------------


def test_a_report_run_answers_with_what_the_runner_reads(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    workspace: Path, config: HarnessConfig, fake: FakeProvider,
) -> None:
    _cli_supervisor(monkeypatch, workspace, config, fake)
    response = _report_run(capsys, workspace)

    assert isinstance(response["run_id"], str) and response["run_id"]
    assert response["action"] == "complete", "the runner reads `failed` as an error"
    # The cell's one-line summary is `ledger`, falling back to `message`.
    assert isinstance(response.get("ledger") or response.get("message"), str)


def test_findings_status_and_events_carry_the_fields_the_runner_folds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    workspace: Path, config: HarnessConfig, fake: FakeProvider,
) -> None:
    _cli_supervisor(monkeypatch, workspace, config, fake)
    run_id = _report_run(capsys, workspace)["run_id"]
    where = ["--json", "-w", str(workspace)]

    findings = _json(capsys, ["findings", run_id, *where])["findings"]
    assert findings, "the fake security lens reports a finding"
    for record in findings:
        # `from_export`: each of these is read, and a missing one is a silent zero.
        for key in ("id", "title", "lens", "severity", "path", "line_start", "line_end",
                    "cwe", "evidence", "detail", "recommendation", "confidence"):
            assert key in record, f"findings --json lost {key!r}"
        assert isinstance(record["evidence"], list)

    status = _json(capsys, ["status", run_id, *where])
    assert status["phase"] == "complete"
    assert status["agents"], "the runner counts agents, stopped agents and analysis agents"
    for agent in status["agents"]:
        assert {"kind", "status", "ended"} <= set(agent)
    assert any(a["kind"] == "analysis" for a in status["agents"])

    events = _json(capsys, ["events", run_id, *where])["events"]
    turns = [e for e in events if e["type"] == "turn_recorded"]
    assert turns, "token usage is summed from recorded turns"
    for event in turns:
        turn = event["payload"]["turn"]
        assert "id" in turn, "a re-reported turn is de-duplicated by id"
        assert {"input_tokens", "output_tokens"} <= set(turn["usage"])
        # Its presence, not its contents, is what tells a measured coverage
        # of nothing from a harness that never measured it.
        assert "files_read" in turn
    for event in events:
        assert {"type", "payload", "actor"} <= set(event)


class Stalling(FakeProvider):
    """Analysis that never claims to be done, so the supervisor has to stop it."""

    def _analysis(self, request: CompletionRequest) -> dict[str, Any]:
        payload = super()._analysis(request)
        payload["status"] = "in_progress"
        return payload


def test_a_stopped_agent_says_why_in_the_note_and_as_a_field(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    workspace: Path, config: HarnessConfig,
) -> None:
    """The note is what the runner parses today; the field is what it can move to.

    `stop_reasons` splits note text on ``finished (stop):``. That wording is
    kept. `status --json` now also says it as data, as an `EndCause` value and
    the reason, so the parse can be retired without losing anything.
    """
    _cli_supervisor(monkeypatch, workspace, config, Stalling())
    run_id = _report_run(capsys, workspace)["run_id"]
    where = ["--json", "-w", str(workspace)]

    status = _json(capsys, ["status", run_id, *where])
    stopped = [a for a in status["agents"] if a["status"] == "stopped"]
    assert stopped, "an agent that never finishes is stopped by the supervisor"
    for agent in stopped:
        assert agent["ended"] is not None, f"{agent['id']} stopped with no recorded cause"
        assert agent["ended"]["cause"] in {str(c) for c in EndCause}
        assert agent["ended"]["reason"], "the cause comes with the reason given for it"

    events = _json(capsys, ["events", run_id, *where])["events"]
    notes = [str(e["payload"].get("text", "")) for e in events if e["type"] == "note"]
    reasons = [n.split("finished (stop):", 1)[1].strip()
               for n in notes if "finished (stop):" in n]
    by_directive = [a for a in stopped if a["ended"]["cause"] == "stop"]
    assert by_directive, "this run's agents are stopped by a directive, not by running dry"
    assert len(reasons) >= len(by_directive), (
        "every agent a directive stopped must still leave the note the runner parses"
    )
    assert {a["ended"]["reason"] for a in by_directive} <= set(reasons)


class Refusing(FakeProvider):
    """Every analysis request refused, the way a provider reports a policy refusal."""

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if self._classify(request) == "analysis":
            raise ProviderRefusal("fake", model="fake-1", category="cyber",
                                  explanation="declined")
        return await super().complete(request)


def test_a_refusal_is_a_note_with_its_category_and_the_agent_as_actor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    workspace: Path, config: HarnessConfig,
) -> None:
    """`refusals`: a note whose payload has ``refusal`` set, read with ``category``."""
    _cli_supervisor(monkeypatch, workspace, config, Refusing())
    response = _json(capsys, ["run", TASK, "--mode", "report", "--backend", "autonomous",
                              "--yes", "--json", "-w", str(workspace)])
    events = _json(capsys, ["events", response["run_id"], "--json",
                            "-w", str(workspace)])["events"]

    refused = [e for e in events if e["type"] == "note" and e["payload"].get("refusal")]
    assert refused, "a refused agent must be on the log as a refusal"
    status = _json(capsys, ["status", response["run_id"], "--json", "-w", str(workspace)])
    analysis = {a["id"] for a in status["agents"] if a["kind"] == "analysis"}
    assert {e["actor"] for e in refused} >= analysis, (
        "the runner calls a cell refused when every analysis agent appears as an actor"
    )
    assert all(e["payload"].get("category") == "cyber" for e in refused)
    assert {a["ended"]["cause"] for a in status["agents"]
            if a["id"] in analysis} == {"refused"}


def test_providers_reports_routing_and_availability(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    workspace: Path, config: HarnessConfig, fake: FakeProvider,
) -> None:
    """`pinned_env` checks `routing`; preflight checks `providers.<name>.available`."""
    _cli_supervisor(monkeypatch, workspace, config, fake)
    data = _json(capsys, ["providers", "--json", "-w", str(workspace)])

    assert isinstance(data["routing"], dict)
    for stage in ("default", "planning", "analysis", "synthesis", "verification", "drift"):
        assert stage in data["routing"], f"routing lost the {stage!r} stage"
    assert isinstance(data["providers"], dict)
    for info in data["providers"].values():
        assert "available" in info


# -- the environment and the trusted config ------------------------------------


def test_route_variables_and_the_trusted_home_reach_every_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """How a cell pins every stage to the model under test, and sets its params.

    ``analysis.security`` becomes ``SUPERVISOR_ROUTE_ANALYSIS__SECURITY``, and a
    ``config.json`` under ``SUPERVISOR_HOME`` is trusted, so its provider
    ``params`` (which a workspace file may not set) are honoured.
    """
    for key in list(os.environ):
        if key.startswith("SUPERVISOR_ROUTE_"):
            monkeypatch.delenv(key)
    home, ws = tmp_path / "home", tmp_path / "ws"
    home.mkdir()
    ws.mkdir()
    (home / "config.json").write_text(
        json.dumps({"providers": {"ollama": {"params": {"think": False}}}}), encoding="utf-8"
    )
    monkeypatch.setenv("SUPERVISOR_HOME", str(home))
    monkeypatch.setenv("SUPERVISOR_ROUTE_DEFAULT", "ollama:model-under-test")
    monkeypatch.setenv("SUPERVISOR_ROUTE_ANALYSIS__SECURITY", "ollama:model-under-test")

    config = load_config(ws)
    assert config.routing["analysis.security"] == "ollama:model-under-test"
    for stage in ("planning", "analysis", "analysis.security", "synthesis", "verification"):
        assert config.binding_for(stage).ref() == "ollama:model-under-test"
    assert config.providers["ollama"].params == {"think": False}
    # preflight reads this to decide whether the harness may run commands.
    assert isinstance(config.policy.allow_command_execution, bool)


# -- the provider layer --------------------------------------------------------


async def test_the_baseline_call_shape_still_works(tmp_path: Path) -> None:
    """`harness_complete`, line for line: the baseline's every live request."""
    config = load_config(tmp_path)
    config.routing = {"default": "fake:fake-1"}
    router = ModelRouter(config)
    router.register("fake", FakeProvider())
    try:
        response = await router.complete(
            "analysis",
            CompletionRequest(system="find vulnerabilities",
                              messages=[ChatMessage("user", "the code")],
                              json_schema=None, max_tokens=1000, timeout=900.0, extra={}),
            retries=0,
        )
    finally:
        await router.aclose()
    assert isinstance(response.text, str) and response.text
    assert isinstance(response.finish_reason, str)
    for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        assert isinstance(getattr(response.usage, field), int)


async def test_a_refusal_reaches_the_baseline_as_an_exception_with_a_category(
    tmp_path: Path,
) -> None:
    config = load_config(tmp_path)
    config.routing = {"default": "fake:fake-1"}
    router = ModelRouter(config)
    router.register("fake", Refusing())
    try:
        with pytest.raises(ProviderRefusal) as caught:
            await router.complete(
                "analysis",
                CompletionRequest(system="find vulnerabilities",
                                  messages=[ChatMessage("user", "the code")]),
                retries=0,
            )
    finally:
        await router.aclose()
    assert caught.value.category == "cyber"


def test_what_the_batch_body_is_built_from(tmp_path: Path) -> None:
    """`harness_body` rebuilds the Anthropic request from these pieces."""
    config = load_config(tmp_path)
    config.routing = {"default": "anthropic:claude-sonnet-5-5"}
    binding = config.binding_for("analysis")
    assert binding.model == "claude-sonnet-5-5"
    assert isinstance(binding.params, dict)
    assert isinstance(DEFAULT_MAX_TOKENS, int) and DEFAULT_MAX_TOKENS > 0
    assert "JSON" in schema_instruction({"type": "object"})
    anthropic = config.providers.get("anthropic")
    if anthropic is not None:  # `_anthropic_credentials` reads both
        assert isinstance(anthropic.resolved_key(), str)
        assert isinstance(anthropic.base_url, str)
