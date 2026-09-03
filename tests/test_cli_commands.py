"""Every CLI command, through `main()`, at the level a user actually types.

Findings **Q-C2** and **Q-A3** of `docs/quality-assessment.md`. `cli.py` was the
second-largest module, **42% covered**, and the least tested of the large ones --
while being, with `mcp_server.py`, one of the two boundaries a user touches
directly.

These are characterisation tests first and coverage second. They are written
*before* the complexity work in the same batch, so that reshaping
`cmd_status` and `_print_response` has something to be judged against: 42%
coverage is not enough protection to refactor a 19-branch function behind.

## No model is involved

A `supervisor.config.json` in the workspace routes every stage to `host`, which
is the harness's default anyway. The whole run is then driven **through the CLI
itself** -- `start`, `report`, `advance`, `approve` -- rather than through the
Supervisor API with the CLI pointed at the leftovers. That is slower to write
and is the point: `cmd_report`'s stdin handling, `cmd_approve`'s decision
parsing and `_print_response`'s rendering only run on this path.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness.cli import main

from .conftest import FakeProvider

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"


@pytest.fixture
def cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace the CLI can drive with no model and no ambient config.

    `SUPERVISOR_HOME` is cleared deliberately: with it set, every run in this
    file would land in the developer's own store and `runs` would list theirs.
    """
    monkeypatch.delenv("SUPERVISOR_HOME", raising=False)
    monkeypatch.delenv("SUPERVISOR_WORKSPACE", raising=False)
    (tmp_path / "src" / "auth").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "auth" / "login.py").write_text(
        "def login(request):\n    return check(request)\n", encoding="utf-8"
    )
    (tmp_path / "supervisor.config.json").write_text(
        json.dumps({
            "routing": {"default": "host"},
            "policy": {
                "default_max_turns": 3, "execution_max_turns": 3,
                "max_analysis_lenses": 2, "min_analysis_lenses": 2,
            },
        }),
        encoding="utf-8",
    )
    return tmp_path


def run_cli(cwd: Path, *args: str) -> int:
    return main([*args, "-w", str(cwd)])


def json_cli(cwd: Path, capsys: pytest.CaptureFixture[str], *args: str) -> dict[str, Any]:
    assert run_cli(cwd, *args, "--json") == 0
    return json.loads(capsys.readouterr().out)


def _answer_for(fake: FakeProvider, packet: dict[str, Any]) -> dict[str, Any]:
    from supervisor_harness.providers.base import ChatMessage, CompletionRequest

    return fake.answer_for(
        packet["kind"],
        CompletionRequest(
            messages=[ChatMessage("user", packet["brief"])],
            system=packet["brief"][:200],
            json_schema=packet["schema"],
        ),
    )


def drive(
    cwd: Path, fake: FakeProvider, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Run a task to completion using only CLI invocations."""
    response = json_cli(cwd, capsys, "start", PROMPT)
    for _ in range(60):
        action = response.get("action")
        if action in ("complete", "failed"):
            return response

        if action == "await_approval":
            response = json_cli(cwd, capsys, "approve", response["run_id"], "--all")
            continue

        if action == "dispatch":
            last = response
            for packet in response["packets"]:
                # `report` reads the turn from stdin when no -i is given, which
                # is how a host pipes an agent's answer in.
                monkeypatch.setattr(
                    "sys.stdin", io.StringIO(json.dumps(_answer_for(fake, packet)))
                )
                last = json_cli(cwd, capsys, "report", packet["run_id"], packet["agent_id"])
            response = (
                last if last.get("action") in ("complete", "failed", "await_approval", "dispatch")
                else json_cli(cwd, capsys, "advance", response["run_id"])
            )
            if response.get("action") == "await_reports":
                response = json_cli(cwd, capsys, "advance", response["run_id"])
            continue

        response = json_cli(cwd, capsys, "advance", response["run_id"])
    raise AssertionError("the CLI loop did not terminate")


@pytest.fixture
def finished(
    cwd: Path, fake: FakeProvider, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """A completed run on disk, produced entirely through the CLI."""
    final = drive(cwd, fake, capsys, monkeypatch)
    assert final["action"] == "complete", final.get("message")
    capsys.readouterr()
    return str(final["run_id"])


# -- the loop a host-delegated user actually runs ----------------------------


def test_a_run_completes_through_the_command_line(
    cwd: Path, fake: FakeProvider, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final = drive(cwd, fake, capsys, monkeypatch)

    assert final["action"] == "complete"
    assert final["report_markdown"]


def test_start_prints_the_packet_a_host_must_run(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The human-readable path, which `--json` skips entirely."""
    assert run_cli(cwd, "start", PROMPT) == 0
    out = capsys.readouterr().out

    assert "packet" in out
    assert "planning" in out
    assert "supervisor_report" in out, "the next step must be stated, not implied"


def test_report_reads_a_turn_from_a_file_as_well_as_stdin(
    cwd: Path, fake: FakeProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-i` is what a user types when a sub-agent wrote its answer to disk."""
    started = json_cli(cwd, capsys, "start", PROMPT)
    packet = started["packets"][0]
    answer = cwd / "answer.json"
    answer.write_text(json.dumps(_answer_for(fake, packet)), encoding="utf-8")

    reported = json_cli(
        cwd, capsys, "report", packet["run_id"], packet["agent_id"], "-i", str(answer)
    )

    assert reported["action"] != "failed"


def test_a_turn_that_is_not_json_is_refused_with_a_usable_message(
    cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answer comes from a model, so this is an ordinary Tuesday."""
    started = json_cli(cwd, capsys, "start", PROMPT)
    packet = started["packets"][0]
    monkeypatch.setattr("sys.stdin", io.StringIO("the agent said some words"))

    code = run_cli(cwd, "report", packet["run_id"], packet["agent_id"])
    captured = capsys.readouterr()

    assert code != 0
    assert "JSON" in (captured.out + captured.err)


def test_approve_states_the_whole_decision(
    cwd: Path, fake: FakeProvider, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every task not named is *rejected*, which the README calls out.

    A partial `--task` list that silently approved the rest would be the worst
    possible reading of a command about permission.
    """
    response = json_cli(cwd, capsys, "start", PROMPT)
    for _ in range(60):
        if response.get("action") == "await_approval":
            break
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
    else:
        pytest.fail("never reached approval")

    tasks = response["tasks"]
    assert tasks, "synthesis proposed nothing to decide"
    decided = json_cli(
        cwd, capsys, "approve", response["run_id"], "--task", f"{tasks[0]['id']}:reject"
    )

    assert decided["action"] != "failed"
    status = json_cli(cwd, capsys, "status", response["run_id"])
    assert any(t["status"] == "rejected" for t in status["tasks"])


def test_the_approval_screen_shows_what_a_user_must_decide_on(
    cwd: Path, fake: FakeProvider, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one screen where work is accepted or rejected, in its human form.

    Everything else in this file drives approval with `--json`, which renders
    through a different branch entirely -- so without this the whole of
    `_print_proposed_tasks` was unexercised, including the definition of done.
    A criterion the user did not see is one they did not agree to.
    """
    response = json_cli(cwd, capsys, "start", PROMPT)
    for _ in range(60):
        if response.get("action") == "await_approval":
            break
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
    else:
        pytest.fail("never reached approval")

    task = response["tasks"][0]
    capsys.readouterr()

    # Re-ask for the same state, this time rendered for a human.
    assert run_cli(cwd, "advance", response["run_id"]) == 0
    screen = capsys.readouterr().out

    assert "task(s) proposed" in screen
    assert task["id"] in screen
    assert task["title"] in screen
    assert task["action"] in screen, "the user cannot approve work they cannot see"
    assert task["motivation"] in screen
    assert "definition of done" in screen
    for criterion in task["definition_of_done"]:
        assert criterion["statement"] in screen
        flag = "required" if criterion["mandatory"] else "optional"
        assert flag in screen


# -- the read-only commands, on a finished run -------------------------------


def test_status_renders_a_finished_run(
    cwd: Path, finished: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`cmd_status` is the most branched function in the file (19).

    Every branch here is a section that appears only when the run has that kind
    of content, so a finished run is the input that reaches most of them.
    """
    status = json_cli(cwd, capsys, "status", finished)
    assert run_cli(cwd, "status", finished) == 0
    out = capsys.readouterr().out

    assert finished in out
    assert "complete" in out

    # Assert the *rows*, not the headings. A first draft checked `"agents" in
    # out`, which the word survives elsewhere in the output -- deleting the
    # whole section still passed it.
    for agent in status["agents"]:
        assert agent["id"] in out, f"agent {agent['id']} is missing from the rendering"
        assert agent["role"] in out
    for task in status["tasks"]:
        assert task["id"] in out, f"task {task['id']} is missing from the rendering"

    # The envelope is what the run was permitted to touch, so it has to be on
    # the screen a user reads to find out what happened.
    assert "envelope" in out
    assert status["envelope"]["source"] in out


def test_status_defaults_to_the_most_recent_run(
    cwd: Path, finished: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(cwd, "status") == 0
    assert finished in capsys.readouterr().out


def test_status_reports_an_empty_store_rather_than_failing(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`init` then `status` is the first thing a new user does."""
    code = run_cli(cwd, "status")
    captured = capsys.readouterr()

    assert code != 0
    assert "no runs" in (captured.out + captured.err).lower()


def test_explain_shows_why_each_directive_was_issued(
    cwd: Path, finished: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(cwd, "explain", finished) == 0
    out = capsys.readouterr().out

    assert "directive" in out.lower()
    assert "drift" in out.lower()


def test_runs_lists_what_the_store_holds(
    cwd: Path, finished: str, capsys: pytest.CaptureFixture[str]
) -> None:
    listing = json_cli(cwd, capsys, "runs")

    assert [r["id"] for r in listing["runs"]] == [finished]
    assert listing["runs"][0]["phase"] == "complete"


def test_lessons_are_shown_and_filtered(
    cwd: Path, finished: str, capsys: pytest.CaptureFixture[str]
) -> None:
    every = json_cli(cwd, capsys, "lessons")
    assert every["lessons"], "a completed run recorded no lessons"

    assert run_cli(cwd, "lessons", "-t", "implementer", "-n", "5") == 0
    assert capsys.readouterr().out.strip()


def test_providers_reports_routing_and_health(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every stage routes to `host` here, so this needs no network."""
    answer = json_cli(cwd, capsys, "providers")

    assert answer["routing"]["default"] == "host"
    assert "host" in answer["providers"]
    # The workspace config is what routed everything to `host`, so it must show
    # up as a source -- that is the half of this command that diagnoses "why is
    # it not using the model I configured".
    assert any("supervisor.config.json" in s for s in answer["config_sources"])


def test_drift_needs_a_model_and_says_so_when_the_stage_is_delegated(
    cwd: Path, finished: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every stage is routed to `host`, so there is no drift model to ask.

    This is the ordinary configuration, so the failure has to be legible rather
    than a traceback from inside a provider.
    """
    status = json_cli(cwd, capsys, "status", finished)
    agent = status["agents"][0]["id"]

    code = run_cli(cwd, "drift", agent, finished)
    captured = capsys.readouterr()

    assert isinstance(code, int)
    assert (captured.out + captured.err).strip(), "the command said nothing at all"


# -- resume, abandon and the run-level commands ------------------------------


def test_resume_picks_a_run_up_where_it_stopped(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    started = json_cli(cwd, capsys, "start", PROMPT)

    resumed = json_cli(cwd, capsys, "resume", started["run_id"])

    assert resumed["run_id"] == started["run_id"]
    assert resumed["packets"][0]["kind"] == "planning"


def test_abandon_settles_an_agent_that_will_never_report(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    started = json_cli(cwd, capsys, "start", PROMPT)
    packet = started["packets"][0]

    after = json_cli(
        cwd, capsys, "abandon", packet["agent_id"], packet["run_id"],
        "--reason", "sub-agent was cancelled",
    )

    assert after["action"] != "failed"
    assert "cancelled" in json.dumps(json_cli(cwd, capsys, "explain", packet["run_id"]))


def test_reindex_rebuilds_the_projection(
    cwd: Path, finished: str, capsys: pytest.CaptureFixture[str]
) -> None:
    index = cwd / ".supervisor" / "index.sqlite3"
    assert index.exists()
    index.unlink()

    assert run_cli(cwd, "reindex") == 0
    assert "Reindexed" in capsys.readouterr().out
    assert json_cli(cwd, capsys, "runs")["runs"][0]["id"] == finished


def test_delete_removes_a_run_from_disk_and_from_the_index(
    cwd: Path, finished: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(cwd, "delete", finished) == 0
    capsys.readouterr()

    assert json_cli(cwd, capsys, "runs")["runs"] == []
    assert not (cwd / ".supervisor" / "runs" / finished).exists()


def test_delete_by_age_keeps_the_most_recent(
    cwd: Path, finished: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--older-than 0` would take everything; `--keep-last` is the guard."""
    assert run_cli(cwd, "delete", "--older-than", "0", "--keep-last", "1") == 0
    capsys.readouterr()

    assert [r["id"] for r in json_cli(cwd, capsys, "runs")["runs"]] == [finished]


def test_prune_lessons_drops_nothing_when_they_are_all_new(
    cwd: Path, finished: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(cwd, "prune-lessons", "--older-than", "365") == 0
    assert "0 lesson" in capsys.readouterr().out


# -- init --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("claude", [".claude/skills/supervise/SKILL.md", ".claude/commands/supervise.md"]),
        ("cursor", [".cursor/rules/supervisor.mdc", ".cursor/commands/supervise.md"]),
    ],
)
def test_init_installs_what_the_readme_says_it_does(
    cwd: Path, capsys: pytest.CaptureFixture[str], host: str, expected: list[str]
) -> None:
    """The README documents these paths, and a user follows them by hand."""
    assert run_cli(cwd, "init", "--host", host) == 0
    capsys.readouterr()

    for rel in [*expected, ".mcp.json"]:
        assert (cwd / rel).is_file(), rel

    registered = json.loads((cwd / ".mcp.json").read_text(encoding="utf-8"))
    assert "supervisor" in registered.get("mcpServers", {})


def test_init_installs_for_both_hosts_at_once(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(cwd, "init", "--host", "both") == 0
    capsys.readouterr()

    assert (cwd / ".claude" / "skills" / "supervise" / "SKILL.md").is_file()
    assert (cwd / ".cursor" / "rules" / "supervisor.mdc").is_file()


def test_init_does_not_overwrite_without_force(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A user's edited skill file is theirs; clobbering it silently is not on."""
    assert run_cli(cwd, "init", "--host", "claude") == 0
    skill = cwd / ".claude" / "skills" / "supervise" / "SKILL.md"
    skill.write_text("my own notes\n", encoding="utf-8")
    capsys.readouterr()

    assert run_cli(cwd, "init", "--host", "claude") == 0
    assert skill.read_text(encoding="utf-8") == "my own notes\n"

    assert run_cli(cwd, "init", "--host", "claude", "--force") == 0
    assert skill.read_text(encoding="utf-8") != "my own notes\n"


# -- main() itself -----------------------------------------------------------


def test_an_unknown_run_id_fails_without_a_traceback(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_cli(cwd, "status", "run_DOESNOTEXIST")
    captured = capsys.readouterr()

    assert code != 0
    assert "Traceback" not in (captured.out + captured.err)


def test_debug_lets_the_failure_raise(cwd: Path) -> None:
    """`--debug` exists so a bug report can carry a traceback.

    The exception type is asserted rather than merely "something raised": the
    point of the flag is that the *original* error survives instead of being
    turned into an exit code, and `pytest.raises(BaseException)` would pass on
    a `SystemExit` from argparse, which is the opposite of what is wanted.
    """
    with pytest.raises(FileNotFoundError, match="run_DOESNOTEXIST"):
        run_cli(cwd, "status", "run_DOESNOTEXIST", "--debug")


def test_version_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_code:
        main(["--version"])

    assert exit_code.value.code == 0
    assert capsys.readouterr().out.strip()


def test_no_command_prints_usage_rather_than_failing_obscurely(
    capsys: pytest.CaptureFixture[str]
) -> None:
    """argparse exits rather than returning, so this is a SystemExit, not a code.

    Worth pinning as the exit *status*: a shell script driving the harness reads
    it, and argparse's 2 for a usage error is the convention.
    """
    with pytest.raises(SystemExit) as exit_code:
        main([])

    assert exit_code.value.code == 2
    captured = capsys.readouterr()
    assert "usage" in (captured.out + captured.err).lower()
