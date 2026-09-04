"""The MCP boundary: the harness as Claude Code and Cursor actually see it.

Finding **Q-C1** of `docs/quality-assessment.md`, and the one the assessment
called the most serious: `mcp_server.py` was **0% covered**. 101 statements, not
one of them executed by any test. Everything behind it -- the phase machine, the
fence, the store, the packets -- is well covered. The thing a host actually
talks to was tested by nothing at all.

That is the exact failure criterion 10 names: *an untested entry point is
untested software however well covered its internals are.* A tool that failed to
register, a schema the SDK rejected, an argument coerced wrongly, or a result
that arrived without its `next_step` would all leave the internals green and the
product broken for every user of it.

## How these tests call it

Through `server.call_tool(name, arguments)` -- the same entry the host uses --
rather than by reaching for the closures inside `build_server()`. That is
deliberate: calling the functions directly would skip registration, schema
generation and argument coercion, which is most of what this module *is*. The
tools are closures precisely so that they cannot be called any other way.

The run is then driven end to end through the tool surface, following the loop
the module's own docstring documents to hosts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from supervisor_harness import mcp_server
from supervisor_harness.config import HarnessConfig, Policy, default_config
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.host.detect import HostInfo
from supervisor_harness.models import Backend
from supervisor_harness.store.runstore import RunStore

from .conftest import FakeProvider

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"

#: Every tool the server advertises. Named here rather than derived from the
#: server, so that a tool silently disappearing is a failure and not a shorter
#: list that still agrees with itself.
EXPECTED_TOOLS = {
    "supervisor_start", "supervisor_report", "supervisor_abandon", "supervisor_advance",
    "supervisor_approve", "supervisor_status", "supervisor_explain", "supervisor_runs",
    "supervisor_resume", "supervisor_check_drift", "supervisor_lessons",
    "supervisor_providers",
}


@pytest.fixture(autouse=True)
def _no_leaked_supervisor() -> Any:
    """`mcp_server` holds one supervisor per *process*, in a module global.

    That is right for a server and wrong for a test suite: without this, the
    first test to build one would bind every later test to its workspace, and
    the failures would look like state bleeding between runs rather than a
    fixture problem.
    """
    mcp_server._supervisor = None
    yield
    if mcp_server._supervisor is not None:
        mcp_server._supervisor.store.close()
    mcp_server._supervisor = None


@pytest.fixture
def host_config() -> HarnessConfig:
    cfg = default_config()
    cfg.backend = Backend.HOST
    cfg.routing = {k: "host" for k in cfg.routing}
    cfg.policy = Policy(default_max_turns=3, execution_max_turns=3, max_analysis_lenses=3)
    return cfg


@pytest.fixture
def server(workspace: Path, host_config: HarnessConfig) -> Any:
    """A server bound to a temporary workspace, with no network in reach."""
    store = RunStore(workspace / ".supervisor")
    host = HostInfo(name="claude-code", workspace=str(workspace), confidence=1.0)
    mcp_server._supervisor = Supervisor(
        workspace=workspace, config=host_config, store=store, host=host
    )
    return mcp_server.build_server()


async def call(server: Any, tool: str, **arguments: Any) -> dict[str, Any]:
    """Invoke a tool the way the host does, and return its structured result."""
    result = await server.call_tool(tool, arguments)
    assert result.is_error is False, f"{tool} failed: {result.content}"
    return dict(result.structured_content or {})


# -- the surface itself -----------------------------------------------------


async def test_every_documented_tool_is_registered(server: Any) -> None:
    """A tool that fails to register is invisible, and nothing else would notice."""
    tools = {t.name for t in await server.list_tools()}

    assert tools == EXPECTED_TOOLS


async def test_every_tool_carries_a_description_and_a_usable_schema(server: Any) -> None:
    """The description *is* the interface: it is what the host reads to decide.

    The schema matters as much. It is generated from the annotations, so a
    parameter typed in a way the SDK cannot render would break the tool at the
    boundary while the function behind it stayed perfectly correct.
    """
    for tool in await server.list_tools():
        assert tool.description and tool.description.strip(), tool.name
        assert tool.input_schema.get("type") == "object", tool.name
        assert "properties" in tool.input_schema, tool.name


async def test_the_server_tells_the_host_how_to_drive_it(server: Any) -> None:
    """`INSTRUCTIONS` is the only place the required call order is stated."""
    for expected in ("supervisor_start", "supervisor_report", "supervisor_advance",
                     "supervisor_abandon", "supervisor_approve"):
        assert expected in mcp_server.INSTRUCTIONS
    assert "in parallel" in mcp_server.INSTRUCTIONS
    assert "Never approve on the user's behalf" in mcp_server.INSTRUCTIONS


# -- a whole run, through the tools only ------------------------------------


async def _drive(server: Any, fake: FakeProvider) -> dict[str, Any]:
    """Run the loop `INSTRUCTIONS` tells a host to run, using only tool calls."""
    from supervisor_harness.providers.base import ChatMessage, CompletionRequest

    response = await call(server, "supervisor_start", prompt=PROMPT, mode="execute",
                          host_agents=[{"name": "general-purpose", "description": "any"}])
    for _ in range(60):
        action = response.get("action")
        if action in ("complete", "failed"):
            return response

        if action == "await_approval":
            response = await call(
                server, "supervisor_approve", run_id=response["run_id"],
                decisions=[{"task_id": t["id"], "decision": "approve"}
                           for t in response.get("tasks", [])],
            )
            continue

        if action == "dispatch":
            last = response
            for packet in response["packets"]:
                answer = fake.answer_for(
                    packet["kind"],
                    CompletionRequest(
                        messages=[ChatMessage("user", packet["brief"])],
                        system=packet["brief"][:200],
                        json_schema=packet["schema"],
                    ),
                )
                last = await call(server, "supervisor_report", run_id=packet["run_id"],
                                  agent_id=packet["agent_id"], result=answer)
            response = (
                last if last.get("action") in ("complete", "failed", "await_approval", "dispatch")
                else await call(server, "supervisor_advance", run_id=response["run_id"])
            )
            if response.get("action") == "await_reports":
                response = await call(server, "supervisor_advance", run_id=response["run_id"])
            continue

        response = await call(server, "supervisor_advance", run_id=response["run_id"])
    raise AssertionError("the documented loop did not terminate")


async def test_a_run_completes_through_the_tool_surface(
    server: Any, fake: FakeProvider
) -> None:
    """The whole point of the module, end to end and through nothing else."""
    final = await _drive(server, fake)

    assert final["action"] == "complete", final.get("message")
    assert final.get("report_markdown"), "the host is told to show this to the user"

    status = await call(server, "supervisor_status", run_id=final["run_id"])
    assert status["phase"] == "complete"
    assert status["tasks"], "a completed execute run reported no tasks"


async def test_a_packet_arrives_with_everything_the_host_needs(server: Any) -> None:
    """It crosses the boundary as JSON, so anything unserialisable is lost here."""
    started = await call(server, "supervisor_start", prompt=PROMPT, mode="execute")
    packet = started["packets"][0]

    assert packet["run_id"] and packet["agent_id"] and packet["kind"] == "planning"
    assert packet["brief"].strip()
    assert packet["schema"]["type"] == "object"
    assert packet["turns_remaining"] >= 1


async def test_each_action_tells_the_host_what_to_do_next(
    server: Any, fake: FakeProvider
) -> None:
    """`next_step` is guidance the host acts on, and an empty one strands it.

    It is added by `_result`, so a tool returning the raw response would leave
    the host with a state and no instruction -- which reads as the run stopping
    for no reason.
    """
    started = await call(server, "supervisor_start", prompt=PROMPT, mode="execute")
    assert "supervisor_report" in started["next_step"]

    final = await _drive(server, fake)
    assert "report_markdown" in final["next_step"]


# -- what a host gets wrong -------------------------------------------------


async def test_a_result_sent_as_a_json_string_is_accepted(server: Any) -> None:
    """Hosts send strings. A schema saying "object" does not stop them.

    `_as_dict` exists for exactly this, and it also digs an object out of prose
    -- a sub-agent that wrapped its JSON in a sentence has still done the work.
    """
    started = await call(server, "supervisor_start", prompt=PROMPT, mode="execute")
    packet = started["packets"][0]
    answer = (
        'Here is the plan you asked for:\n{"restated_goal": "rate limit login", '
        '"mode": "execute", "lenses": [{"role": "security", "why": "exposure", '
        '"objectives": ["Find the attack path"]}]}\nHope that helps.'
    )

    reported = await call(server, "supervisor_report", run_id=packet["run_id"],
                          agent_id=packet["agent_id"], result=answer)

    assert reported.get("error") is None
    assert reported["action"] != "failed"


async def test_a_result_that_is_not_an_object_is_refused_not_raised(server: Any) -> None:
    """An exception here surfaces to the user as a broken tool, not a bad call."""
    started = await call(server, "supervisor_start", prompt=PROMPT, mode="execute")
    packet = started["packets"][0]

    refused = await call(server, "supervisor_report", run_id=packet["run_id"],
                         agent_id=packet["agent_id"], result="no json here at all")

    assert "must be a JSON object" in refused["error"]
    assert refused["received"]


async def test_an_unknown_mode_falls_back_rather_than_failing(server: Any) -> None:
    """`mode` is free text from a model; a typo should not end the run."""
    started = await call(server, "supervisor_start", prompt=PROMPT, mode="EXEcute")
    assert started["action"] == "dispatch"

    nonsense = await call(server, "supervisor_start", prompt=PROMPT, mode="whatever")
    assert nonsense["action"] == "dispatch", "an unusable mode should become auto"


async def test_an_abandoned_agent_settles_instead_of_being_re_issued(
    server: Any
) -> None:
    """The case only the host can see, and the reason the tool exists.

    Without it the harness cannot tell a dead sub-agent from a slow one and
    re-offers the same packet on every advance, forever.
    """
    started = await call(server, "supervisor_start", prompt=PROMPT, mode="execute")
    packet = started["packets"][0]

    after = await call(server, "supervisor_abandon", run_id=packet["run_id"],
                       agent_id=packet["agent_id"], reason="sub-agent was cancelled")

    assert after["action"] != "failed"
    explained = await call(server, "supervisor_explain", run_id=packet["run_id"])
    assert "cancelled" in str(explained), "the reason should reach the run's record"


# -- the read-only tools, including on an empty store -----------------------


@pytest.mark.parametrize("tool", ["supervisor_status", "supervisor_explain"])
async def test_a_store_with_no_runs_answers_rather_than_failing(
    server: Any, tool: str
) -> None:
    """`init` then `status` is the first thing a new user does."""
    answer = await call(server, tool)

    assert answer["runs"] == []
    assert "no runs" in answer["message"]


async def test_resume_with_nothing_to_resume_says_so(server: Any) -> None:
    answer = await call(server, "supervisor_resume")

    assert "no run to resume" in answer["error"]


async def test_the_read_only_tools_default_to_the_most_recent_run(
    server: Any, fake: FakeProvider
) -> None:
    """Every one of them takes an optional run_id, and hosts omit it."""
    final = await _drive(server, fake)

    assert (await call(server, "supervisor_status"))["run_id"] == final["run_id"]
    assert (await call(server, "supervisor_explain"))["run_id"] == final["run_id"]
    assert (await call(server, "supervisor_runs", limit=5))["runs"][0]["id"] == final["run_id"]


async def test_lessons_are_listed_and_filtered(server: Any, fake: FakeProvider) -> None:
    """A completed run writes lessons; the tool is how a host reads them back."""
    await _drive(server, fake)

    every = await call(server, "supervisor_lessons", limit=20)
    assert every["lessons"], "the improvement stage recorded nothing"
    assert {"statement", "category", "target"} <= set(every["lessons"][0])

    filtered = await call(server, "supervisor_lessons", target="implementer", limit=5)
    assert all(le["target"] in ("implementer", "*") for le in filtered["lessons"])


async def test_providers_reports_routing_and_health_without_a_network(
    server: Any
) -> None:
    """`supervisor providers` is the command that diagnoses a misconfiguration.

    Every stage here routes to `host`, which answers without reaching anything,
    so this asserts the shape rather than a live provider.
    """
    answer = await call(server, "supervisor_providers")

    assert answer["host"] == "claude-code"
    assert answer["backend"] == str(Backend.HOST)
    assert answer["routing"]["default"] == "host"
    assert "planning" in answer["routing"], "every known stage should be listed"
    assert "host" in answer["providers"]


# -- the workspace the server binds itself to -------------------------------


def test_an_unsubstituted_editor_template_is_not_a_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hosts pass `${workspaceFolder}` through unexpanded.

    Taken literally it creates a directory by that name and supervises an empty
    one, which looks to the user like a harness that lost their repository.
    """
    monkeypatch.chdir(tmp_path)

    for template in ("${workspaceFolder}", "$WORKSPACE", "   ", ""):
        monkeypatch.setenv("SUPERVISOR_WORKSPACE", template)
        assert mcp_server._workspace() == tmp_path.resolve()

    assert not (tmp_path / "${workspaceFolder}").exists()


def test_a_real_workspace_is_expanded_and_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    target.mkdir()
    monkeypatch.setenv("SUPERVISOR_WORKSPACE", str(target))

    assert mcp_server._workspace() == target.resolve()


def test_the_supervisor_is_built_once_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second one would open a second store on the same directory."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUPERVISOR_WORKSPACE", str(tmp_path))
    mcp_server._supervisor = None

    first = mcp_server.supervisor()
    assert mcp_server.supervisor() is first


# -- the paths a host reaches less often ------------------------------------


async def test_resume_picks_up_a_run_that_was_left_mid_flight(server: Any) -> None:
    """A host restarts; the run is still in the store and must be continuable."""
    started = await call(server, "supervisor_start", prompt=PROMPT, mode="execute")

    resumed = await call(server, "supervisor_resume", run_id=started["run_id"])

    assert resumed["run_id"] == started["run_id"]
    assert resumed["action"] == "dispatch"
    assert resumed["packets"][0]["kind"] == "planning", (
        "an unreported planning packet should be re-offered, not skipped"
    )
    assert "supervisor_report" in resumed["next_step"]


async def test_check_drift_refuses_an_agent_with_nothing_to_assess(
    server: Any
) -> None:
    """The tool is optional and paid-for, so it must fail cheaply.

    An agent that has not reported has no turn to score. Returning an error
    rather than raising keeps a host's speculative call from ending the run.
    """
    started = await call(server, "supervisor_start", prompt=PROMPT, mode="execute")
    packet = started["packets"][0]

    answer = await call(server, "supervisor_check_drift", run_id=packet["run_id"],
                        agent_id=packet["agent_id"])

    assert "no assessment to escalate" in answer["error"]


def test_a_result_that_is_neither_object_nor_string_is_rejected() -> None:
    """The third branch of `_as_dict`: a list, a number, a null.

    Reached through the tool as a schema violation, so it is asserted directly
    -- but the branch exists because the argument is typed `dict | str` and a
    host can still send neither.
    """
    assert mcp_server._as_dict([1, 2, 3]) is None      # type: ignore[arg-type]
    assert mcp_server._as_dict(None) is None            # type: ignore[arg-type]
    assert mcp_server._as_dict('{"a": 1}') == {"a": 1}
    assert mcp_server._as_dict({"a": 1}) == {"a": 1}


def test_build_server_omits_version_for_a_server_class_that_lacks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK 1.x's FastMCP has no `version` constructor argument; only MCPServer
    (2.x) does. `build_server` always passed one, so any host that resolved
    the SDK-1.x fallback import crashed with `TypeError: FastMCP.__init__()
    got an unexpected keyword argument 'version'` before this could register a
    single tool. Simulated here because this suite runs against whichever SDK
    is installed, and cannot assume both are.
    """
    received: dict[str, Any] = {}

    class FakeFastMCP:
        def __init__(self, **kwargs: Any) -> None:
            if "version" in kwargs:
                raise TypeError(
                    "FastMCP.__init__() got an unexpected keyword argument 'version'"
                )
            received.update(kwargs)

        def tool(self, **_: Any) -> Any:
            return lambda fn: fn

    monkeypatch.setattr(mcp_server, "_Server", FakeFastMCP)
    monkeypatch.setattr(mcp_server, "_SERVER_TAKES_VERSION", False)

    mcp_server.build_server()

    assert "version" not in received
    assert received["name"] == "supervisor-harness"


def test_main_serves_the_built_server_over_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    """`supervisor-mcp` is a console script and nothing else calls it.

    CI's entry-point step runs `supervisor --help`, not this one, so a break in
    `main` would ship. The transport is asserted because stdio is what the
    `.mcp.json` `init` writes expects; anything else would leave every host
    unable to connect.
    """
    served: list[str] = []

    class Fake:
        def run(self, transport: str) -> None:
            served.append(transport)

    monkeypatch.setattr(mcp_server, "build_server", lambda: Fake())

    mcp_server.main()

    assert served == ["stdio"]
