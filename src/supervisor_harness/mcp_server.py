"""MCP server: the harness as a tool set for Claude Code and Cursor.

The host drives a loop:

1. ``supervisor_start`` -- describe the task and what agents you can spawn.
2. Run the returned packets, in parallel where there is more than one.
3. ``supervisor_report`` -- hand back each agent's JSON result. You get a
   directive: continue with corrections, or the agent is finished.
4. ``supervisor_advance`` -- when every packet is reported, get the next phase.
5. ``supervisor_approve`` -- after the user decides on the proposed tasks.

``supervisor_abandon`` covers the case the host is the only one who can see: a
sub-agent that crashed or was cancelled and will never report.

The harness holds the state, the supervision and the persistence. The host
contributes the tools, the repository context and the user's permission model.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import KNOWN_STAGES
from .core.supervisor import Supervisor, SupervisorResponse
from .models import Backend, RunMode

if TYPE_CHECKING:
    # Type-checked against the SDK this project supports. The runtime import
    # below is a try/except so that an older SDK still starts, and an aliased
    # name assigned in an `except` reads as `Any` to a type checker -- which
    # made every `@server.tool(...)` an untyped decorator and the twelve tools
    # under it untyped functions (finding Q-T2). The SDK itself is fully typed.
    from mcp.server.mcpserver import MCPServer as _Server
else:
    try:  # MCP SDK 2.x
        from mcp.server.mcpserver import MCPServer as _Server
    except ModuleNotFoundError:  # pragma: no cover - SDK 1.x fallback
        from mcp.server.fastmcp import FastMCP as _Server


INSTRUCTIONS = """\
The supervisor harness plans, supervises and verifies multi-agent work.

Call supervisor_start with the user's task. You will get back one or more work
packets. Each packet contains a complete brief and the exact JSON schema its
answer must match. Run each packet with your own sub-agent mechanism -- issue
independent packets in parallel, in a single message -- then call
supervisor_report with each result.

Do not summarise, paraphrase or improve a packet's brief before dispatching it:
the supervisor measures drift against that exact text. Do not answer a packet
yourself if a subagent is available; the point is parallelism and independence.

If one of your subagents crashes, is cancelled, or you cannot run its packet,
call supervisor_abandon with the agent id and what happened. The harness cannot
tell a dead agent from a slow one, and will otherwise re-issue that packet on
every advance. Never invent a result on a missing agent's behalf.

After reporting every packet, call supervisor_advance to get the next phase.
When the run reaches await_approval, present the proposed tasks to the user with
their actions, motivations and definitions of done, and let the user decide
before calling supervisor_approve. Never approve on the user's behalf.
"""


def _workspace() -> Path:
    """The workspace this server supervises.

    ``SUPERVISOR_WORKSPACE`` may arrive as an unexpanded editor template such as
    ``${workspaceFolder}`` when a host does not do variable substitution. Treat
    that as absent rather than creating a directory by that literal name.
    """
    declared = os.environ.get("SUPERVISOR_WORKSPACE", "").strip()
    if not declared or "${" in declared or declared.startswith("$"):
        return Path.cwd().resolve()
    return Path(declared).expanduser().resolve()


_supervisor: Supervisor | None = None


def supervisor() -> Supervisor:
    """One supervisor per server process, bound to the workspace."""
    global _supervisor
    if _supervisor is None:
        _supervisor = Supervisor(workspace=_workspace())
    return _supervisor


def _result(response: SupervisorResponse) -> dict[str, Any]:
    """Shape a response for the host, with the next step stated plainly."""
    payload = response.to_dict()
    payload["next_step"] = {
        "dispatch": (
            "Run every packet below with your own sub-agent tool, in parallel, passing "
            "each brief verbatim. Then call supervisor_report once per packet."
        ),
        "await_reports": (
            "Report any packets you have not yet reported. When all are in, call "
            "supervisor_advance."
        ),
        "await_approval": (
            "Show the user each proposed task -- action, motivation and definition of "
            "done -- and ask which to approve, modify or reject. Then call "
            "supervisor_approve. Do not decide for them."
        ),
        "complete": "Present report_markdown to the user, including the verified "
                    "definition-of-done results.",
        "failed": "Tell the user the run failed and why.",
    }.get(response.action, "")
    return payload


def _register_run_tools(server: _Server) -> None:
    """The tools that drive a run: start it, answer it, and decide on it."""
    @server.tool(
        description=(
            "Start a supervised run. Analyses the task from the angles that fit it, "
            "in parallel, then proposes verifiable work. Pass the agent types you can "
            "spawn so roles bind to real sub-agents."
        )
    )
    async def supervisor_start(
        prompt: str,
        mode: str = "auto",
        host_agents: list[dict[str, Any]] | None = None,
        backend: str = "",
    ) -> dict[str, Any]:
        """Begin a run.

        Args:
            prompt: The user's task, in full. Do not summarise it.
            mode: "auto" (let synthesis decide), "report" (analysis only), or
                "execute" (analysis then approved work).
            host_agents: Sub-agent types you can spawn, as
                [{"name": ..., "description": ..., "tools": [...]}, ...].
            backend: "host" to run agents yourself (default), "autonomous" to let
                the harness drive its own configured models.
        """
        response = await supervisor().start(
            prompt,
            mode=_mode(mode),
            backend=Backend(backend) if backend else None,
            host_agents=host_agents,
        )
        return _result(response)

    @server.tool(
        description=(
            "Report one agent's result. Returns the supervisor's directive: either "
            "corrections and another turn, or acceptance."
        )
    )
    async def supervisor_report(
        run_id: str,
        agent_id: str,
        result: dict[str, Any] | str,
    ) -> dict[str, Any]:
        """Hand back what an agent produced.

        Args:
            run_id: From the packet.
            agent_id: From the packet.
            result: The agent's JSON object, matching the packet's schema. Pass it
                through unmodified -- do not fill gaps or improve it, because the
                supervisor is judging the agent's real output.
        """
        payload = _as_dict(result)
        if payload is None:
            return {
                "error": "result must be a JSON object matching the packet schema",
                "received": str(result)[:200],
            }
        return _result(await supervisor().report(run_id, agent_id, payload))

    @server.tool(
        description=(
            "Give up on an agent that will never report -- its sub-agent crashed, "
            "was cancelled, or you cannot run its packet. The agent is marked "
            "failed and its phase settles instead of re-issuing the packet forever."
        )
    )
    async def supervisor_abandon(
        run_id: str, agent_id: str, reason: str = ""
    ) -> dict[str, Any]:
        """Report an agent as gone.

        Args:
            run_id: From the packet.
            agent_id: From the packet.
            reason: What happened, in one line. It goes on the run's log, so say
                what you actually know -- "sub-agent was cancelled", not "failed".
        """
        return _result(await supervisor().abandon(run_id, agent_id, reason))

    @server.tool(
        description="Move the run to its next phase once the current packets are reported."
    )
    async def supervisor_advance(
        run_id: str, host_agents: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Move to the next phase.

        Args:
            run_id: The run.
            host_agents: Sub-agent types you can spawn, in the same shape
                supervisor_start takes. Only needed if they have changed since
                the run started; the ones declared then are remembered.
        """
        return _result(await supervisor().advance(run_id, host_agents=host_agents))

    @server.tool(
        description=(
            "Record the user's decisions on proposed tasks, then start the approved "
            "work. Only call this after the user has actually decided."
        )
    )
    async def supervisor_approve(
        run_id: str,
        decisions: list[dict[str, Any]],
        renew_envelope: bool = False,
    ) -> dict[str, Any]:
        """Apply approve / modify / reject / defer decisions.

        Args:
            run_id: The run.
            decisions: One entry per task, e.g.
                {"task_id": "tsk_...", "decision": "approve"} or
                {"task_id": "tsk_...", "decision": "modify", "note": "...",
                 "modifications": {"action": "..."}}.
                Modifications may edit title, action, motivation, effort,
                scope_paths, or replace dod wholesale.
            renew_envelope: Re-grant this run's scope envelope. A resume past
                the grant's age limit pauses before execution and asks for
                this; the response's detail says so with needs="envelope_
                renewal". Show the user what the run may modify and ask whether
                it still stands before setting it. It renews the date, never
                the paths -- widening is not something approval can do.
                Pass an empty decisions list when there are no tasks to decide.
        """
        return _result(
            await supervisor().approve(run_id, decisions, renew_envelope=renew_envelope)
        )

    @server.tool(
        description="Resume a persisted run and get the next thing to do."
    )
    async def supervisor_resume(run_id: str = "") -> dict[str, Any]:
        """Pick a run up where it stopped.

        Args:
            run_id: The run. Omit it for the most recent run in this store.
        """
        sup = supervisor()
        target = run_id or (sup.store.latest_run_id() or "")
        if not target:
            return {"error": "no run to resume"}
        return _result(await sup.resume(target))


def _register_read_tools(server: _Server) -> None:
    """The tools that read a run back without changing it."""
    @server.tool(description="Current state of a run, or the most recent one.")
    async def supervisor_status(run_id: str = "") -> dict[str, Any]:
        """Phase, agents, drift and definition-of-done progress.

        Args:
            run_id: The run. Omit it for the most recent run in this store.
        """
        sup = supervisor()
        target = run_id or (sup.store.latest_run_id() or "")
        if not target:
            return {"runs": [], "message": "no runs recorded yet"}
        return sup.status(target)

    @server.tool(
        description="Why each directive was issued to each agent, with the "
                    "evidence behind it: the turn, its drift assessment and "
                    "signals, the inbox it carried, and the directive chosen."
    )
    async def supervisor_explain(run_id: str = "", agent_id: str = "") -> dict[str, Any]:
        """The decision journal for a run, assembled from its event log.

        Args:
            run_id: The run. Omit it for the most recent run in this store.
            agent_id: Only this agent's episodes. Omit for every agent.
        """
        from .core.journal import journal_to_dict

        sup = supervisor()
        target = run_id or (sup.store.latest_run_id() or "")
        if not target:
            return {"runs": [], "message": "no runs recorded yet"}
        return journal_to_dict(sup.explain(target, agent_id))

    @server.tool(description="List recent runs with their phase and progress.")
    async def supervisor_runs(limit: int = 10) -> dict[str, Any]:
        """Recent runs, newest first.

        Args:
            limit: How many to return. Default 10.
        """
        sup = supervisor()
        sup.store.reindex()
        return {"runs": sup.store.index().list_runs(limit)}

    @server.tool(
        description=(
            "Ask the drift-checking model for a second opinion on an agent's last "
            "turn. Use when an agent looks plausible but off-brief."
        )
    )
    async def supervisor_check_drift(run_id: str, agent_id: str) -> dict[str, Any]:
        """Score one agent's last turn against the brief it was given.

        Args:
            run_id: The run.
            agent_id: The agent to assess, from its packet. It must have
                reported at least one turn: there is nothing to score otherwise.
        """
        return await supervisor().supervise_with_model(run_id, agent_id)

    @server.tool(
        description=(
            "Lessons the harness has learned from previous runs, optionally filtered "
            "to a role or stage."
        )
    )
    async def supervisor_lessons(target: str = "", limit: int = 20) -> dict[str, Any]:
        """What previous runs taught the harness.

        Args:
            target: Filter to a role id, "supervisor", "dod", or "*". Omit it
                for the most recent lessons whatever they target.
            limit: How many to return. Default 20.
        """
        store = supervisor().store
        lessons = store.lessons_for([target], limit) if target else store.lessons()[-limit:]
        return {
            "lessons": [
                {
                    "statement": le.statement,
                    "category": str(le.category),
                    "target": le.target,
                    "why": le.why,
                    "how_to_apply": le.how_to_apply,
                    "occurrences": le.occurrences,
                }
                for le in lessons
            ]
        }


def _register_meta_tools(server: _Server) -> None:
    """What the harness itself is configured to do."""
    @server.tool(
        description=(
            "Which model each stage of a run is routed to, and whether each provider "
            "is reachable right now."
        )
    )
    async def supervisor_providers() -> dict[str, Any]:
        sup = supervisor()
        return {
            "host": sup.host.name,
            "backend": str(sup.config.backend),
            "workspace": str(sup.workspace),
            "config_sources": sup.config.sources,
            "routing": {
                stage: sup.config.binding_for(stage).ref()
                for stage in sorted(set(sup.config.routing) | set(KNOWN_STAGES))
            },
            "providers": await sup.router.health(),
        }


def build_server() -> _Server:
    """Construct the MCP server with the harness tools registered.

    Registration is grouped rather than one flat run of twelve decorated
    closures. A nested `def` counts toward its enclosing function's
    complexity, so the flat form measured 17 (finding Q-A2) while each tool
    in it is trivial.
    """
    """Construct the MCP server with the harness tools registered."""
    server = _Server(
        name="supervisor-harness",
        instructions=INSTRUCTIONS,
        version="0.1.0",
    )
    _register_run_tools(server)
    _register_read_tools(server)
    _register_meta_tools(server)
    return server


def _mode(value: str) -> RunMode:
    try:
        return RunMode(value.strip().lower())
    except ValueError:
        return RunMode.AUTO


def _as_dict(value: dict[str, Any] | str) -> dict[str, Any] | None:
    """Accept a dict, or a JSON string, or prose with JSON embedded in it."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        from .providers.base import extract_json

        return extract_json(value)
    return None


def main() -> None:
    """Entry point for ``supervisor-mcp`` (stdio transport)."""
    build_server().run("stdio")


if __name__ == "__main__":
    main()
