"""Command line interface.

Two audiences. A person inspecting or driving runs by hand:

    supervisor init
    supervisor run "harden the login endpoint" --yes
    supervisor status
    supervisor events <run> --type note
    supervisor lessons

and a host or script driving the same protocol the MCP server exposes:

    supervisor start "..." --json | jq .packets
    supervisor report <run> <agent> --input turn.json --json
    supervisor advance <run> --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import KNOWN_STAGES, PROJECT_CONFIG, load_config, write_example
from .core.supervisor import Supervisor, SupervisorResponse
from .host.detect import detect_host
from .models import Backend, RunMode
from .store.events import (
    UNRECOGNISED_TYPE_KEY,
    Event,
    EventType,
    event_to_dict,
)
from .store.runstore import RunStore

INTEGRATIONS = Path(__file__).parent / "integrations"


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def _emit(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))


def _print_response(response: SupervisorResponse, as_json: bool) -> None:
    if as_json:
        _emit(response.to_dict(), True)
        return

    print(f"\nrun {response.run_id}  [{response.phase}]  -> {response.action}")
    if response.message:
        print(f"  {response.message}")

    for packet in response.packets:
        target = f" via {packet.host_agent_type}" if packet.host_agent_type else ""
        print(f"\n  packet {packet.agent_id} ({packet.kind}){target}")
        print(f"    {packet.title}  [{packet.model}, {packet.turns_remaining} turn(s) left]")

    if response.tasks:
        print(f"\n  {len(response.tasks)} task(s) proposed:")
        for task in response.tasks:
            print(f"\n  - {task['id']}  {task['title']}  "
                  f"(risk {task['risk']}, effort {task['effort']})")
            print(f"      action:     {task['action']}")
            print(f"      motivation: {task['motivation']}")
            print("      definition of done:")
            for crit in task["definition_of_done"]:
                flag = "required" if crit["mandatory"] else "optional"
                print(f"        [{crit['method']}, {flag}] {crit['statement']}")
                if crit.get("command"):
                    print(f"            $ {crit['command']}")
            for note in response.task_notes.get(task["id"], []):
                print(f"      note: {note}")

    if response.directive:
        d = response.directive
        print(f"\n  directive: {d['kind']} -- {d['rationale']}")
        for correction in d.get("corrections", []):
            print(f"    - {correction}")

    if response.action == "complete":
        print()
        print(response.report_markdown)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _supervisor(args: argparse.Namespace) -> Supervisor:
    workspace = Path(args.workspace).resolve()
    config = load_config(workspace)
    if getattr(args, "backend", None):
        config.backend = Backend(args.backend)
    return Supervisor(workspace=workspace, config=config)


def cmd_init(args: argparse.Namespace) -> int:
    """Install host integrations and an example configuration."""
    workspace = Path(args.workspace).resolve()
    written: list[str] = []

    config_path = workspace / PROJECT_CONFIG
    if not config_path.exists() or args.force:
        write_example(config_path)
        written.append(str(config_path.relative_to(workspace)))

    host = detect_host(workspace)
    targets = {"claude", "cursor"} if args.host == "both" else {args.host or host.name.split("-")[0]}

    if "claude" in targets or host.name == "claude-code":
        skill_dir = workspace / ".claude" / "skills" / "supervise"
        skill_dir.mkdir(parents=True, exist_ok=True)
        _copy(INTEGRATIONS / "claude_code" / "SKILL.md", skill_dir / "SKILL.md", args.force, written, workspace)
        cmd_dir = workspace / ".claude" / "commands"
        cmd_dir.mkdir(parents=True, exist_ok=True)
        _copy(INTEGRATIONS / "claude_code" / "supervise.md", cmd_dir / "supervise.md", args.force, written, workspace)

    if "cursor" in targets or host.name == "cursor":
        rules_dir = workspace / ".cursor" / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)
        _copy(INTEGRATIONS / "cursor" / "supervisor.mdc", rules_dir / "supervisor.mdc", args.force, written, workspace)
        cmd_dir = workspace / ".cursor" / "commands"
        cmd_dir.mkdir(parents=True, exist_ok=True)
        _copy(INTEGRATIONS / "cursor" / "supervise.md", cmd_dir / "supervise.md", args.force, written, workspace)

    mcp_path = workspace / ".mcp.json"
    template = json.loads((INTEGRATIONS / "mcp.json").read_text(encoding="utf-8"))
    existing: dict[str, Any] = {}
    if mcp_path.exists():
        try:
            existing = json.loads(mcp_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    servers = existing.setdefault("mcpServers", {})
    if "supervisor" not in servers or args.force:
        servers["supervisor"] = template["mcpServers"]["supervisor"]
        mcp_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        written.append(".mcp.json")

    if args.json:
        _emit({"workspace": str(workspace), "host": host.name, "written": written}, True)
        return 0

    print(f"Workspace: {workspace}")
    print(f"Host detected: {host.name} (confidence {host.confidence:.1f})")
    if written:
        print("Wrote:")
        for path in written:
            print(f"  {path}")
    else:
        print("Nothing to write -- everything is already in place (use --force to overwrite).")
    print(
        "\nNext: restart your host so it picks up the MCP server, then ask it to "
        "'supervise' a task, or run `supervisor run \"...\"` for an autonomous run."
    )
    return 0


def _copy(src: Path, dst: Path, force: bool, written: list[str], root: Path) -> None:
    if dst.exists() and not force:
        return
    shutil.copyfile(src, dst)
    written.append(str(dst.relative_to(root)))


def cmd_run(args: argparse.Namespace) -> int:
    """Drive a run to completion without a host."""
    sup = _supervisor(args)
    if sup.config.backend is Backend.HOST and not args.backend:
        sup.config.backend = Backend.AUTONOMOUS

    unrouted = [
        stage for stage in ("planning", "analysis", "synthesis", "verification")
        if sup.config.binding_for(stage).provider == "host"
    ]
    if unrouted:
        print(
            f"error: stage(s) {', '.join(unrouted)} are routed to 'host', but an "
            "autonomous run has no host to delegate to.\n"
            f"Point them at a provider in {PROJECT_CONFIG}, for example:\n"
            '  "routing": { "default": "ollama:qwen3.8-code:latest" }\n'
            "Then re-run. To drive a host-delegated run instead, use `supervisor start`.",
            file=sys.stderr,
        )
        return 2

    async def go() -> SupervisorResponse:
        try:
            return await sup.run(args.prompt, mode=RunMode(args.mode), auto_approve=args.yes)
        finally:
            await sup.aclose()

    response = asyncio.run(go())
    _print_response(response, args.json)
    if response.action == "await_approval" and not args.json:
        print("\nApprove with:")
        print(f"  supervisor approve {response.run_id} --all")
    return 0 if response.action != "failed" else 1


def _host_agents(raw: str) -> list[Any] | None:
    """Parse ``--host-agents``, or say what is wrong with it.

    The flag is JSON typed on a command line, so it is wrong often enough to
    deserve an answer better than whatever the JSON decoder or the registry
    happens to raise. The registry normalises the entries themselves -- a bare
    name is as good as an object there -- so this only has to establish that
    what arrived is a list at all.
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        # ValueError, not TypeError: this is a malformed flag value being
        # reported to a person, not a type contract between two functions.
        raise ValueError(  # noqa: TRY004
            f"must be a JSON array, not {type(parsed).__name__}. Each entry is an "
            'agent type you can spawn: \'["general-purpose"]\' or '
            '\'[{"name": "general-purpose", "description": "..."}]\''
        )
    return parsed


def cmd_start(args: argparse.Namespace) -> int:
    """Begin a host-delegated run and print the first packets."""
    sup = _supervisor(args)
    try:
        host_agents = _host_agents(args.host_agents)
    except ValueError as exc:
        print(f"error: --host-agents {exc}", file=sys.stderr)
        return 2

    async def go() -> SupervisorResponse:
        try:
            return await sup.start(args.prompt, mode=RunMode(args.mode), host_agents=host_agents)
        finally:
            await sup.aclose()

    _print_response(asyncio.run(go()), args.json)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    sup = _supervisor(args)
    raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
    payload = _parse_turn(raw)
    if payload is None:
        print("error: could not find a JSON object in the reported turn", file=sys.stderr)
        return 2

    async def go() -> SupervisorResponse:
        try:
            return await sup.report(args.run_id, args.agent_id, payload)
        finally:
            await sup.aclose()

    _print_response(asyncio.run(go()), args.json)
    return 0


def _parse_turn(raw: str) -> dict[str, Any] | None:
    """Decode a reported turn as leniently as the MCP tool does.

    Agents emit JSON wrapped in prose, inside fences, or with literal newlines
    in string values. The MCP path already tolerated all of that via
    extract_json; requiring strict JSON here meant the CLI rejected turns the
    MCP server would have accepted.
    """
    from .providers.base import extract_json

    for attempt in (lambda: json.loads(raw), lambda: json.loads(raw, strict=False)):
        try:
            parsed = attempt()
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return extract_json(raw)


def cmd_advance(args: argparse.Namespace) -> int:
    sup = _supervisor(args)
    run_id = args.run_id or sup.store.latest_run_id()
    if not run_id:
        print("error: no runs found", file=sys.stderr)
        return 2

    async def go() -> SupervisorResponse:
        try:
            return await sup.advance(run_id)
        finally:
            await sup.aclose()

    _print_response(asyncio.run(go()), args.json)
    return 0


def cmd_abandon(args: argparse.Namespace) -> int:
    sup = _supervisor(args)
    run_id = args.run_id or sup.store.latest_run_id()
    if not run_id:
        print("error: no runs found", file=sys.stderr)
        return 2

    async def go() -> SupervisorResponse:
        try:
            return await sup.abandon(run_id, args.agent_id, args.reason)
        finally:
            await sup.aclose()

    _print_response(asyncio.run(go()), args.json)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    sup = _supervisor(args)
    run_id = args.run_id or sup.store.latest_run_id()
    if not run_id:
        print("error: no runs found", file=sys.stderr)
        return 2

    state = sup.store.load_state(run_id)
    proposed = [t for t in state.tasks.values() if str(t.status) == "proposed"]
    # Renewing the envelope is a decision in its own right, and the case it
    # exists for -- a resume past the grant's age -- has no proposed tasks left
    # to decide, because they were approved before the run was put down.
    if not proposed and not args.renew_envelope:
        print("No tasks awaiting a decision. Pass --renew-envelope to re-grant "
              "this run's scope envelope.", file=sys.stderr)
        return 1

    if not proposed:
        decisions = []
    elif args.all:
        decisions = [{"task_id": t.id, "decision": "approve"} for t in proposed]
    elif args.task:
        wanted = {}
        for entry in args.task:
            task_id, _, decision = entry.partition(":")
            wanted[task_id] = decision or "approve"
        decisions = [
            {"task_id": t.id, "decision": wanted.get(t.id, "reject")} for t in proposed
        ]
    else:
        print("error: pass --all or one or more --task <id>[:approve|reject|defer]",
              file=sys.stderr)
        return 2

    async def go() -> SupervisorResponse:
        try:
            return await sup.approve(
                run_id, decisions, renew_envelope=args.renew_envelope
            )
        finally:
            await sup.aclose()

    _print_response(asyncio.run(go()), args.json)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    sup = _supervisor(args)
    run_id = args.run_id or sup.store.latest_run_id()
    if not run_id:
        print("error: no runs found", file=sys.stderr)
        return 2

    async def go() -> SupervisorResponse:
        try:
            return await sup.resume(run_id)
        finally:
            await sup.aclose()

    _print_response(asyncio.run(go()), args.json)
    return 0


def _render_globs(patterns: list[str]) -> str:
    """A scope pattern list as the CLI should say it out loud.

    ``[]`` and ``["<nothing>"]`` are opposites that both read as punctuation,
    so both are spelled out; anything else is shown verbatim, because a fence
    the user cannot read back is one they cannot check.
    """
    from .core.paths import NOTHING

    if not patterns:
        return "the whole workspace"
    if list(patterns) == [NOTHING]:
        return "no path at all"
    return ", ".join(patterns)


def cmd_status(args: argparse.Namespace) -> int:
    sup = _supervisor(args)
    run_id = args.run_id or sup.store.latest_run_id()
    if not run_id:
        print("No runs recorded yet.", file=sys.stderr)
        return 1

    status = sup.status(run_id)
    if args.json:
        _emit(status, True)
        return 0

    print(f"run {status['run_id']}  [{status['phase']}]  mode={status['mode']} "
          f"backend={status['backend']}")
    print(f"  {status['prompt']}")
    print(f"  updated {status['updated_at']}")

    envelope = status.get("envelope")
    if envelope is None:
        print("  envelope: none established (bounded only by the unconditional floor)")
    else:
        print(f"  envelope [{envelope['source']}]: may modify "
              f"{_render_globs(envelope['paths'])}")
        if envelope["forbidden_paths"]:
            print(f"            never: {_render_globs(envelope['forbidden_paths'])}")
        if status.get("envelope_stale"):
            print(f"            STALE: {status['envelope_stale']}")
            print("            re-grant with: supervisor approve --renew-envelope")

    if status["agents"]:
        print("\n  agents")
        for agent in status["agents"]:
            drift = f" drift {agent['drift']:.2f}" if agent["drift"] is not None else ""
            print(f"    {agent['id']}  {agent['role']:<16} {agent['status']:<8} "
                  f"turns {agent['turns']}{drift}  [{agent['model']}]")

    if status["tasks"]:
        print("\n  tasks")
        for task in status["tasks"]:
            mark = "done" if task["satisfied"] else "open"
            print(f"    {task['id']}  {task['status']:<22} dod {task['dod']:<6} {mark}  "
                  f"{task['title']}")

    for checkpoint in status["checkpoints"]:
        print(f"\n  checkpoint {checkpoint['iteration']}: "
              f"{'passed' if checkpoint['passed'] else 'not passed'} "
              f"(quality {checkpoint['quality']}, scope {checkpoint['scope_fidelity']}, "
              f"completeness {checkpoint['completeness']})")

    if status["artifacts"]:
        print("\n  artifacts")
        for artifact in status["artifacts"]:
            print(f"    {artifact['kind']:<14} {artifact['path']}")

    if status.get("established"):
        print("\n  established by this run's agents")
        contested = set(status.get("contested_facts") or [])
        for fact in status["established"]:
            mark = "CONTESTED " if fact["key"] in contested else ""
            print(f"    {mark}{fact['key']}: {fact['statement']}  "
                  f"({fact['by']}, {fact['evidence']})")
    if status.get("open_questions"):
        print("\n  open questions")
        for question in status["open_questions"]:
            print(f"    {question}")

    print(f"\n  findings {status['findings']}  lessons {status['lessons']}")
    if status["unhandled_events"]:
        print(f"  unhandled event types: {', '.join(status['unhandled_events'])}"
              " (in the log, but nothing projects them)")
    if status["error"]:
        print(f"  error: {status['error']}")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    """Print the decision journal: why each directive was issued, and on what."""
    sup = _supervisor(args)
    run_id = args.run_id or sup.store.latest_run_id()
    if not run_id:
        print("No runs recorded yet.", file=sys.stderr)
        return 1
    if run_id not in sup.store.list_run_ids():
        print(f"error: no such run: {run_id}", file=sys.stderr)
        return 2

    from .core.journal import journal_to_dict, render_journal

    if args.agent and args.agent not in sup.store.load_state(run_id).agents:
        print(f"error: no such agent in {run_id}: {args.agent}", file=sys.stderr)
        return 2

    journal = sup.explain(run_id, args.agent)
    if args.json:
        _emit(journal_to_dict(journal), True)
        return 0
    print(render_journal(journal, width=args.width))
    return 0


def cmd_drift(args: argparse.Namespace) -> int:
    """Ask the drift model for a second opinion on one agent's last turn."""
    sup = _supervisor(args)
    run_id = args.run_id or sup.store.latest_run_id()
    if not run_id:
        print("error: no runs found", file=sys.stderr)
        return 2

    async def go() -> dict[str, Any]:
        try:
            return await sup.supervise_with_model(run_id, args.agent_id)
        finally:
            await sup.aclose()

    assessment = asyncio.run(go())

    if args.json:
        _emit(assessment, True)
        return 1 if assessment.get("error") else 0

    if assessment.get("error"):
        # The two reasons are both about the agent, not the harness: it has no
        # heuristic assessment yet, or it has reported no turn to assess.
        print(f"error: {assessment['error']} for {args.agent_id} in {run_id}",
              file=sys.stderr)
        return 1

    verdict = "on brief" if assessment.get("on_task") else "off brief"
    print(f"\nagent {args.agent_id}  drift {assessment.get('score', 0):.2f}  {verdict}")
    print(f"judged by {assessment.get('checked_by', '(unknown)')}")
    if assessment.get("summary"):
        print(f"\n{assessment['summary']}")
    signals = assessment.get("signals") or []
    if signals:
        print("\nsignals")
        for signal in signals:
            print(f"  {signal.get('kind', '?'):<16} {signal.get('detail', '')}")
    print()
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    """Print a run's event log -- the only place the diagnostic notes live."""
    store = RunStore.discover(Path(args.workspace).resolve())
    run_id = args.run_id or store.latest_run_id()
    if not run_id:
        print("No runs recorded yet.", file=sys.stderr)
        return 1
    # Membership rather than a path probe: the id comes from the command line
    # and is about to be joined onto the runs directory.
    if run_id not in store.list_run_ids():
        print(f"error: no such run: {run_id}", file=sys.stderr)
        return 2

    wanted = args.type.strip().lower()
    if wanted and wanted not in _EVENT_TYPES:
        print(f"error: unknown event type '{args.type}' -- choose one of "
              f"{', '.join(sorted(_EVENT_TYPES))}", file=sys.stderr)
        return 2

    events = [
        event for event in store.log(run_id).read_all()
        if event.seq > args.since and (not wanted or str(event.type) == wanted)
    ]

    if args.json:
        _emit({"run_id": run_id, "events": [event_to_dict(e) for e in events]}, True)
        return 0
    if not events:
        print("No matching events.")
        return 0
    for event in events:
        print(f"{event.seq:>5}  {event.ts}  {_event_type_label(event):<18} {event.actor:<22} "
              f"{_event_summary(event)}")
    return 0


_EVENT_TYPES = {str(t) for t in EventType}


def _event_type_label(event: Event) -> str:
    """The type the log recorded, which for an unknown one is not the sentinel.

    A type this build does not define is read as ``unknown`` so the line is not
    lost; it still carries its own name, and that is the name worth printing.
    Filtering stays on the sentinel, so ``--type unknown`` lists all of them.
    """
    recorded = str(event.payload.get(UNRECOGNISED_TYPE_KEY, "")).strip()
    return recorded or str(event.type)


def _event_summary(event: Event) -> str:
    """One line of an event's payload: the note text, else its remaining fields."""
    text = str(event.payload.get("text", "")).strip()
    if text:
        return text.replace("\n", " ")
    fields = " ".join(
        f"{key}={value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}"
        for key, value in event.payload.items()
    ).replace("\n", " ")
    return fields if len(fields) <= 160 else fields[:157] + "..."


def cmd_runs(args: argparse.Namespace) -> int:
    store = RunStore.discover(Path(args.workspace).resolve())
    store.reindex()
    runs = store.index().list_runs(args.limit)
    if args.json:
        _emit({"runs": runs}, True)
        return 0
    if not runs:
        print("No runs recorded yet.")
        return 0
    for run in runs:
        print(f"{run['id']}  {run['phase']:<18} findings {run['findings']:<4} "
              f"tasks {run['tasks_verified']}/{run['tasks']:<4} {run['prompt'][:60]}")
    return 0


def cmd_lessons(args: argparse.Namespace) -> int:
    workspace = str(Path(args.workspace).resolve())
    store = RunStore.discover(Path(args.workspace).resolve())
    lessons = (
        store.lessons_for([args.target], args.limit, workspace=workspace)
        if args.target else store.lessons()
    )
    if args.json:
        from .serde import to_jsonable

        _emit({"lessons": [to_jsonable(le) for le in lessons]}, True)
        return 0
    if not lessons:
        print("No lessons recorded yet. They accumulate as runs complete.")
        return 0
    for lesson in sorted(lessons, key=lambda le: (le.occurrences, le.confidence), reverse=True):
        origin = lesson.origin_label(workspace)
        print(f"\n[{lesson.category}] -> {lesson.target}  "
              f"(seen {lesson.occurrences}x, confidence {lesson.confidence:.2f}, "
              f"learned {'here' if origin == 'here' else 'in ' + origin})")
        print(f"  {lesson.statement}")
        if lesson.how_to_apply:
            print(f"  apply: {lesson.how_to_apply}")
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    sup = _supervisor(args)

    async def go() -> dict[str, Any]:
        try:
            return await sup.router.health()
        finally:
            await sup.aclose()

    health = asyncio.run(go())
    stages = sorted(set(sup.config.routing) | set(KNOWN_STAGES) | {
        "analysis.security", "analysis.architecture"
    })
    routing = {stage: sup.config.binding_for(stage).ref() for stage in stages}

    if args.json:
        _emit({"host": sup.host.name, "backend": str(sup.config.backend),
               "config_sources": sup.config.sources,
               "routing": routing, "providers": health}, True)
        return 0

    if sup.config.rejected_settings:
        print("ignored (a workspace config file may not set these):")
        for entry in sup.config.rejected_settings:
            print(f"  {entry}")
        print()
    print(f"host      {sup.host.name} (confidence {sup.host.confidence:.1f})")
    print(f"backend   {sup.config.backend}")
    print(f"config    {', '.join(sup.config.sources) or '(built-in defaults only)'}")
    print("\nproviders")
    for name, info in health.items():
        if not info.get("enabled", True):
            print(f"  {name:<12} disabled")
            continue
        mark = "ok" if info.get("available") else "unavailable"
        detail = info.get("error") or info.get("default_model", "")
        print(f"  {name:<12} {mark:<12} {detail}")
    print("\nrouting")
    for stage, ref in routing.items():
        print(f"  {stage:<24} {ref}")
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    store = RunStore.discover(Path(args.workspace).resolve())
    count = store.reindex()
    print(f"Reindexed {count} run(s) into {store.root / 'index.sqlite3'}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    """Remove one run, or every run past a cutoff."""
    store = RunStore.discover(Path(args.workspace).resolve())

    if args.older_than is not None:
        gone = store.purge(older_than_days=args.older_than, keep_last=args.keep_last)
        if not gone:
            print(f"No runs older than {args.older_than} day(s).")
            return 0
        print(f"Deleted {len(gone)} run(s): {', '.join(gone)}")
        return 0

    if not args.run:
        print("Name a run, or pass --older-than DAYS.", file=sys.stderr)
        return 2
    if not store.delete_run(args.run):
        print(f"No such run: {args.run}", file=sys.stderr)
        return 1
    print(f"Deleted {args.run} and its rows in the index.")
    return 0


def cmd_prune_lessons(args: argparse.Namespace) -> int:
    """Drop lessons past the age cap from the shared library."""
    store = RunStore.discover(Path(args.workspace).resolve())
    dropped = store.prune_lessons(max_age_days=args.older_than)
    print(f"Dropped {dropped} lesson(s) older than {args.older_than} day(s).")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import main as serve

    serve()
    return 0


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="supervisor",
        description="Supervised multi-agent execution with verified definitions of done.",
    )
    parser.add_argument("--version", action="version", version=f"supervisor-harness {__version__}")
    parser.add_argument("-w", "--workspace", default=".", help="workspace root (default: cwd)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--debug", action="store_true",
                        help="let an unexpected failure raise, with its traceback")

    # The same flags again, accepted after the subcommand. argparse only looks
    # for top-level options before the subcommand, so `supervisor start x --json`
    # was a usage error -- including in the form the README documented.
    # SUPPRESS keeps the subparser copies from overwriting the top-level values
    # with their own defaults when they are not given.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")
    common.add_argument("-w", "--workspace", default=argparse.SUPPRESS,
                        help="workspace root (default: cwd)")
    common.add_argument("--debug", action="store_true", default=argparse.SUPPRESS,
                        help="let an unexpected failure raise, with its traceback")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", parents=[common], help="install host integrations and an example config")
    p.add_argument("--host", choices=["claude", "cursor", "both"], default="",
                   help="which host to install for (default: whichever is detected)")
    p.add_argument("--force", action="store_true", help="overwrite existing files")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("run", parents=[common], help="run a task to completion without a host")
    p.add_argument("prompt", help="what you want done, in your own words")
    p.add_argument("--mode", choices=["auto", "report", "execute"], default="auto",
                   help="what the run should produce: 'report' ends with the analysis, "
                        "'execute' proposes tasks for you to approve, 'auto' (default) "
                        "lets synthesis decide which the request asks for")
    p.add_argument("--backend", choices=["host", "autonomous"], default="",
                   help="who runs the agents, overriding config: 'autonomous' is the "
                        "harness against your configured models, which is what `run` "
                        "assumes when config says host and nothing is passed here")
    p.add_argument("-y", "--yes", action="store_true",
                   help="approve every proposed task without asking")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("start", parents=[common], help="begin a host-delegated run and print its packets")
    p.add_argument("prompt", help="what you want done, in your own words")
    p.add_argument("--mode", choices=["auto", "report", "execute"], default="auto",
                   help="what the run should produce: 'report' ends with the analysis, "
                        "'execute' proposes tasks for you to approve, 'auto' (default) "
                        "lets synthesis decide which the request asks for")
    p.add_argument("--host-agents", default="", metavar="JSON",
                   help='the subagent types you can spawn, as a JSON array of objects: '
                        '[{"name": "general-purpose", "description": "..."}]. Roles are '
                        'matched against these names')
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("report", parents=[common], help="report an agent turn from a file or stdin")
    p.add_argument("run_id", help="the run the agent belongs to")
    p.add_argument("agent_id", help="the agent whose turn this is, from its work packet")
    p.add_argument("-i", "--input", default="-", help="JSON file, or - for stdin")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("advance", parents=[common], help="move a run to its next phase")
    p.add_argument("run_id", nargs="?", default="",
                   help="which run (default: the most recent one in this store)")
    p.set_defaults(func=cmd_advance)

    p = sub.add_parser("abandon", parents=[common],
                       help="give up on an agent that will never report")
    p.add_argument("agent_id", help="the agent that crashed, was cancelled, or will "
                                    "never report for any other reason")
    p.add_argument("run_id", nargs="?", default="",
                   help="which run (default: the most recent one in this store)")
    p.add_argument("--reason", default="", help="what happened, for the run's log")
    p.set_defaults(func=cmd_abandon)

    p = sub.add_parser("approve", parents=[common], help="decide on proposed tasks")
    p.add_argument("run_id", nargs="?", default="",
                   help="which run (default: the most recent one in this store)")
    p.add_argument("--all", action="store_true", help="approve every proposed task")
    p.add_argument("--task", action="append", metavar="ID[:DECISION]",
                   help="decide one task, where DECISION is approve (the default), "
                        "reject or defer. Repeatable. Every proposed task you do not "
                        "name is rejected, so this says the whole decision, not part "
                        "of it")
    p.add_argument("--renew-envelope", action="store_true",
                   help="re-grant this run's scope envelope, which a resume past "
                        "policy.envelope_max_age_days needs before it will execute. "
                        "Renews the date, never the paths")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("resume", parents=[common], help="resume a persisted run")
    p.add_argument("run_id", nargs="?", default="",
                   help="which run (default: the most recent one in this store)")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("status", parents=[common], help="show a run in detail")
    p.add_argument("run_id", nargs="?", default="",
                   help="which run (default: the most recent one in this store)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("explain", parents=[common],
                       help="why each directive was issued, and on what evidence")
    p.add_argument("run_id", nargs="?", default="",
                   help="which run (default: the most recent one in this store)")
    p.add_argument("-a", "--agent", default="", metavar="AGENT_ID",
                   help="only this agent's episodes")
    p.add_argument("--width", type=int, default=96, metavar="COLS",
                   help="wrap long text at this width (default: 96)")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("drift", parents=[common],
                       help="ask the drift model about an agent that looks off-brief")
    p.add_argument("agent_id", help="the agent to assess; it must have reported a turn")
    p.add_argument("run_id", nargs="?", default="",
                   help="which run (default: the most recent one in this store)")
    p.set_defaults(func=cmd_drift)

    p = sub.add_parser("events", parents=[common],
                       help="print a run's event log, including its diagnostic notes")
    p.add_argument("run_id", nargs="?", default="",
                   help="which run (default: the most recent one in this store)")
    p.add_argument("-t", "--type", default="", metavar="TYPE",
                   help="only events of this type, e.g. note")
    p.add_argument("--since", type=int, default=0, metavar="SEQ",
                   help="only events after this sequence number")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("runs", parents=[common], help="list recent runs")
    p.add_argument("-n", "--limit", type=int, default=20,
                   help="how many to list, newest first (default: 20)")
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("lessons", parents=[common], help="show what previous runs taught the harness")
    p.add_argument("-t", "--target", default="",
                   help="filter to a role id, 'supervisor', 'dod', or '*'")
    p.add_argument("-n", "--limit", type=int, default=20,
                   help="how many to show, most recent first (default: 20)")
    p.set_defaults(func=cmd_lessons)

    p = sub.add_parser("providers", parents=[common], help="show model routing and provider health")
    p.set_defaults(func=cmd_providers)

    p = sub.add_parser("reindex", parents=[common], help="rebuild the SQLite index from the event logs")
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser(
        "delete", parents=[common],
        help="delete a run, or every run older than a cutoff, from disk and the index",
    )
    p.add_argument("run", nargs="?", help="the run to delete; omit when using --older-than")
    p.add_argument("--older-than", type=int, metavar="DAYS",
                   help="delete every run last updated more than DAYS ago")
    p.add_argument("--keep-last", type=int, default=5, metavar="N",
                   help="never delete the N most recent runs (default: 5)")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser(
        "prune-lessons", parents=[common],
        help="drop lessons older than a cutoff from the cross-run library",
    )
    p.add_argument("--older-than", type=int, default=180, metavar="DAYS",
                   help="drop lessons not seen for more than DAYS (default: 180)")
    p.set_defaults(func=cmd_prune_lessons)

    p = sub.add_parser("mcp", parents=[common], help="run the MCP server on stdio")
    p.set_defaults(func=cmd_mcp)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted -- the run is persisted; resume it with `supervisor resume`",
              file=sys.stderr)
        return 130
    except FileNotFoundError as exc:
        if args.debug:
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - the CLI reports, it does not traceback
        if args.debug:
            raise
        print(f"error: {exc}\n(re-run with --debug for a traceback)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
