"""The offline turn-budget tool, held to the same promise as its companion.

`tools/where_the_turns_went.py` exists because a run stalled twice with its
agents out of turns, and nothing anywhere could say why. It runs on a machine
whose data cannot leave it, so the first test here is the promise -- counts,
kinds, statuses and phase names, and nothing else -- and the arithmetic comes
after, exactly as in `tests/test_where_the_time_went.py`.

The claim it rests on is narrower than the timing tool's, because this one reads
`turn.output`: it hashes the content to count repeats and never prints anything
derived from it but an integer. That is worth testing directly rather than
trusting, so the whitelist test below is the load-bearing one.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "where_the_turns_went", ROOT / "tools" / "where_the_turns_went.py"
)
assert _spec and _spec.loader
wtt = importlib.util.module_from_spec(_spec)
sys.modules["where_the_turns_went"] = wtt
_spec.loader.exec_module(wtt)


def _event(type_: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    return {"type": type_, "ts": "2026-09-08T10:00:00Z", "payload": payload or {}}


def _dispatch(agent_id: str) -> dict[str, object]:
    return _event("agent_dispatched", {"agent_id": agent_id, "kind": "analysis"})


def _spawn(agent_id: str, kind: str, budget: int, role: str = "") -> dict[str, object]:
    return _event("agent_spawned", {"agent": {
        "id": agent_id, "kind": kind, "role": role, "budget": {"max_turns": budget},
    }})


def _turn(agent_id: str, output: str = "", claimed: str = "running") -> dict[str, object]:
    return _event("turn_recorded", {"turn": {
        "agent_id": agent_id, "output": output, "claimed_status": claimed,
    }})


def _directive(agent_id: str, kind: str) -> dict[str, object]:
    return _event("directive_issued", {"directive": {"agent_id": agent_id, "kind": kind}})


# -- the promise -----------------------------------------------------------


SECRETS = (
    "rewrite the authentication service for AcmeCorp",
    "src/acme/secret_ledger.py",
    "the login handler leaks session tokens",
    "payments-gateway-owner",
)


def test_nothing_from_a_payload_reaches_the_output() -> None:
    """This tool reads what an agent actually wrote, so it has to be checked.

    The timing tool never touches `turn.output`. This one hashes it, and the
    whole design rests on the hash being a dead end.
    """
    events = [
        _event("run_created", {"run": {"prompt": SECRETS[0]}}),
        _event("phase_changed", {"phase": "analyzing"}),
        _spawn("agt_1", "analysis", 6, role=SECRETS[3]),
        _event("brief_rendered", {"agent_id": "agt_1", "brief": SECRETS[1]}),
        _turn("agt_1", output=SECRETS[2]),
        _event("finding_added", {"finding": {"title": SECRETS[2]}}),
        _directive("agt_1", "continue"),
    ]

    rendered = "\n".join(wtt.summarise(events))

    for secret in SECRETS:
        assert secret not in rendered, secret
    for fragment in ("AcmeCorp", "secret_ledger", "login", "payments"):
        assert fragment not in rendered, fragment


def test_the_output_is_only_numbers_kinds_statuses_and_phase_names() -> None:
    """The stronger form: an unexpected word fails, whatever it is.

    A hash leaking into a line would fail here as surely as a prompt would --
    hex is words too.
    """
    events = [
        _event("phase_changed", {"phase": "analyzing"}),
        _spawn("agt_1", "analysis", 2, role="security"),
        _turn("agt_1", output="anything at all"),
        _directive("agt_1", "continue"),
        _event("agent_status", {"agent_id": "agt_1", "status": "stopped"}),
    ]

    words = set()
    for line in wtt.summarise(events):
        words.update(re.findall(r"[A-Za-z][A-Za-z-]*", line))

    allowed = {
        # labels and the sentences explaining two of the columns
        "events", "last", "phase", "agents", "turns", "recorded", "repeat",
        "identical", "to", "an", "earlier", "turn", "of", "the", "same", "agent",
        "out", "budget", "their", "were", "repeats", "by", "said-done", "status",
        "directives", "s", "x", "sent", "that", "spent", "without", "settling",
        "halted", "supervisor", "drift", "signals", "n",
        "never", "answered", "handed", "a", "packet", "back", "returned",
        # the only run-derived words there are
        "analyzing", "analysis", "continue", "stopped",
    }
    unexpected = {w for w in words if w not in allowed}

    assert not unexpected, f"unexpected words in the output: {sorted(unexpected)}"


def test_an_agents_role_never_appears_although_its_kind_does() -> None:
    """Kind is a fixed vocabulary. Role is a planning model's free text.

    A lens role is written by a model out of the user's own prompt, so it can
    say anything the prompt said. It is the one field on the spec that looks
    safe and is not.
    """
    events = [_spawn("agt_1", "analysis", 4, role="the AcmeCorp payments ledger")]

    rendered = "\n".join(wtt.summarise(events))

    assert "analysis" in rendered
    assert "AcmeCorp" not in rendered and "ledger" not in rendered


# -- the arithmetic --------------------------------------------------------


def test_an_agent_that_used_its_whole_budget_is_counted() -> None:
    events = [_spawn("agt_1", "analysis", 3)] + [_turn("agt_1", f"turn {i}") for i in range(3)]

    rendered = "\n".join(wtt.summarise(events))

    assert "out of turns      1" in rendered
    assert "3/3" in next(le for le in wtt.summarise(events) if "analysis#1" in le)


def test_an_agent_at_its_budget_that_was_accepted_did_not_run_out_of_turns() -> None:
    """The count's first version fired on the normal case.

    A synthesizer and a verifier are given one turn each, on purpose. Counting
    every agent at its budget as exhausted made eight of the twenty agents in
    the first real log this read a suspect, none of which had run out of
    anything -- and the count exists to find the ones that did.
    """
    events = [
        _spawn("agt_1", "synthesis", 1),
        _turn("agt_1", "the plan", claimed="done"),
        _event("agent_status", {"agent_id": "agt_1", "status": "done"}),
    ]

    assert "out of turns      0" in "\n".join(wtt.summarise(events))


def test_an_agent_at_its_budget_still_being_corrected_did_run_out() -> None:
    """The case it is for: the budget ended the agent, not the supervisor."""
    events = [
        _spawn("agt_1", "execution", 2),
        _turn("agt_1", "one"), _directive("agt_1", "refocus"),
        _turn("agt_1", "two"), _directive("agt_1", "refocus"),
    ]

    assert "out of turns      1" in "\n".join(wtt.summarise(events))


def test_an_agent_the_supervisor_stopped_is_counted_separately() -> None:
    """Halted and exhausted are different endings and want different fixes.

    A stop is the drift rules firing twice; running out is a budget that was
    too small for the work. Folding them together would send anyone reading
    this to the wrong file.
    """
    events = [
        _spawn("agt_1", "execution", 10),
        _turn("agt_1", "one"), _directive("agt_1", "narrow"),
        _turn("agt_1", "two"), _directive("agt_1", "refocus"),
        _turn("agt_1", "three"), _directive("agt_1", "stop"),
    ]

    rendered = "\n".join(wtt.summarise(events))

    assert "stopped           1" in rendered
    assert "out of turns      0" in rendered, "it had seven turns left"


def test_the_drift_signals_say_what_the_corrections_were_reacting_to() -> None:
    """A directive says what the supervisor did; a signal says why.

    `refocus x7` is a fact about the supervisor. Whether it was reacting to a
    scope violation or to objectives left uncovered is the fact that decides
    where a fix goes, and it lives one event earlier.
    """
    events = [
        _spawn("agt_1", "analysis", 6),
        _spawn("agt_2", "analysis", 6),
        _event("drift_assessed", {"agent_id": "agt_1", "assessment": {"signals": [
            {"kind": "scope_paths", "detail": "src/acme/secret_ledger.py", "score": 0.85},
        ]}}),
        _event("drift_assessed", {"agent_id": "agt_2", "assessment": {"signals": [
            {"kind": "scope_paths", "detail": "src/acme/secret_ledger.py", "score": 0.85},
            {"kind": "repetition", "detail": "84% the same", "score": 0.8},
        ]}}),
    ]

    rendered = "\n".join(wtt.summarise(events))

    assert "scope_paths          2        2" in rendered
    assert "repetition           1        1" in rendered
    assert "secret_ledger" not in rendered, "the detail names files and is not read"


def test_a_run_with_no_assessments_prints_no_signal_section() -> None:
    """An empty table under a heading reads as a finding. It is not one."""
    assert "drift signals" not in "\n".join(
        wtt.summarise([_spawn("agt_1", "analysis", 2), _turn("agt_1", "one")])
    )


def test_a_repeated_turn_is_counted_as_a_repeat() -> None:
    """The signature this tool was written to make visible.

    The turn contract carries no turn identifier, so an orchestrator that
    reports the same answer twice spends two turns and the harness cannot tell.
    After a context compaction that is exactly what a host has lost track of --
    and the budget runs out having bought one turn of work.
    """
    events = [
        _spawn("agt_1", "analysis", 3),
        _turn("agt_1", "the same answer"),
        _turn("agt_1", "the same answer"),
        _turn("agt_1", "the same answer"),
    ]

    rendered = "\n".join(wtt.summarise(events))

    assert "repeat turns      2" in rendered
    assert "of their turns  2 of 3 were repeats" in rendered


def test_three_genuinely_different_turns_are_not_repeats() -> None:
    """The other half of the same claim, or the count means nothing."""
    events = [_spawn("agt_1", "analysis", 3)] + [
        _turn("agt_1", f"answer {i}") for i in range(3)
    ]

    assert "repeat turns      0" in "\n".join(wtt.summarise(events))


def test_reasoning_counts_towards_a_turns_fingerprint() -> None:
    """Two turns with the same output and different reasoning are two turns."""
    events = [
        _spawn("agt_1", "analysis", 2),
        _event("turn_recorded", {"turn": {
            "agent_id": "agt_1", "output": "same", "reasoning": "first pass",
        }}),
        _event("turn_recorded", {"turn": {
            "agent_id": "agt_1", "output": "same", "reasoning": "second pass",
        }}),
    ]

    assert "repeat turns      0" in "\n".join(wtt.summarise(events))


def test_an_agent_that_said_it_was_finished_and_was_driven_again_shows_up() -> None:
    """A budget spent on a conversation one side thought was over."""
    events = [
        _spawn("agt_1", "execution", 4),
        _turn("agt_1", "one", claimed="done"),
        _directive("agt_1", "continue"),
        _turn("agt_1", "two", claimed="done"),
        _directive("agt_1", "continue"),
    ]

    line = next(le for le in wtt.summarise(events) if "execution#1" in le)

    assert "2" in line and "continue x2" in line


def test_the_directive_tally_separates_the_two_ways_a_budget_goes() -> None:
    """`continue x5` and `refocus x3` are different bugs in different places."""
    events = [
        _spawn("agt_1", "analysis", 4),
        _spawn("agt_2", "analysis", 4),
    ]
    events += [_directive("agt_1", "continue") for _ in range(3)] + [_directive("agt_1", "stop")]
    events += [_directive("agt_2", "refocus"), _directive("agt_2", "reject")]

    rendered = "\n".join(wtt.summarise(events))

    assert "continue x3, stop" in rendered
    assert "refocus, reject" in rendered


def test_agents_are_numbered_within_their_kind_in_spawn_order() -> None:
    events = [
        _spawn("agt_a", "analysis", 2),
        _spawn("agt_b", "analysis", 2),
        _spawn("agt_c", "execution", 5),
    ]

    rendered = "\n".join(wtt.summarise(events))

    assert "analysis#1" in rendered and "analysis#2" in rendered
    assert "execution#1" in rendered


def test_a_turn_against_an_agent_that_was_never_spawned_is_ignored() -> None:
    """A log copied mid-run can begin after a spawn; it must not crash on one."""
    assert "turns recorded    0" in "\n".join(wtt.summarise([_turn("agt_unknown", "x")]))


def test_an_agent_with_no_budget_recorded_is_not_called_exhausted() -> None:
    """Dividing by a budget nobody set would make every such agent a suspect."""
    events = [_spawn("agt_1", "analysis", 0), _turn("agt_1", "one")]

    rendered = "\n".join(wtt.summarise(events))

    assert "out of turns      0" in rendered
    assert "1/-" in next(le for le in wtt.summarise(events) if "analysis#1" in le)


def test_a_packet_issued_over_and_over_to_a_silent_agent_is_the_other_stall() -> None:
    """A stall that costs no budget at all, and reads the same from outside.

    An agent that never answers stays in `ACTIVE_AGENT_STATUSES`, so the phase
    goes on handing it the same packet. Nothing is exhausted, nothing fails, and
    the run does not move -- which is indistinguishable from a slow sub-agent
    unless someone counts the handouts against the answers.
    """
    events = [_spawn("agt_1", "analysis", 6)] + [_dispatch("agt_1") for _ in range(5)]

    rendered = "\n".join(wtt.summarise(events))

    assert "never answered    1" in rendered
    assert "out of turns      0" in rendered, "it spent no budget: that is the point"
    assert "5     0/6" in rendered


def test_an_agent_answering_every_packet_is_not_reported_as_unanswered() -> None:
    events = [_spawn("agt_1", "analysis", 2), _dispatch("agt_1"), _turn("agt_1", "one")]

    assert "never answered" not in "\n".join(wtt.summarise(events))


# -- surviving a log it was handed rather than one it made -----------------


def test_an_event_type_this_tool_does_not_know_is_ignored() -> None:
    events = [_spawn("agt_1", "analysis", 2), _event("some_future_event", {"anything": "at all"})]

    assert "agents            1" in "\n".join(wtt.summarise(events))


def test_a_truncated_last_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(_spawn("agt_1", "analysis", 2)) + "\n" + '{"type": "turn_rec',
        encoding="utf-8",
    )

    assert len(wtt.read_log(path)) == 1


def test_an_empty_log_says_so() -> None:
    assert wtt.summarise([]) == ["the log is empty"]


# -- the entry point -------------------------------------------------------


def test_it_runs_over_a_real_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in [_spawn("agt_1", "analysis", 2),
                                               _turn("agt_1", "one")]),
        encoding="utf-8",
    )

    assert wtt.main(["where_the_turns_went.py", str(path)]) == 0
    assert "turns recorded" in capsys.readouterr().out


def test_a_missing_file_is_refused_rather_than_raised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert wtt.main(["where_the_turns_went.py", str(tmp_path / "nope.jsonl")]) == 2
    assert "no such event log" in capsys.readouterr().err


def test_no_argument_prints_how_to_call_it(capsys: pytest.CaptureFixture[str]) -> None:
    assert wtt.main(["where_the_turns_went.py"]) == 2
    assert "events.jsonl" in capsys.readouterr().err


async def test_it_reads_a_log_this_harness_actually_wrote(tmp_path: Path) -> None:
    """The one test that would notice the event schema moving underneath it.

    Everything above builds its own events, so a renamed `agent_spawned` or a
    `budget` that stopped carrying `max_turns` would go unnoticed. This drives a
    real run and reads its real log.
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
        wtt.summarise(wtt.read_log(store.run_dir(started.run_id) / "events.jsonl"))
    )

    assert "turns recorded    1" in rendered, "the planning turn was not attributed"
    assert "synthesis#1" in rendered, "the planner was spawned and is not in the table"
    assert "/1" in rendered, "its budget of one turn was not read off the spec"
    assert "Tidy the imports" not in rendered
    assert "quality" not in rendered, "a model-chosen role reached the output"
