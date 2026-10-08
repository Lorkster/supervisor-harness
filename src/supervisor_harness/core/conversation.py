"""An implementer driven as one conversation, through native tool calls.

The turn contract drives every agent the same way: each supervised turn is a
fresh request whose answer is one JSON object, tool calls inside it, and the
tool results are dropped when the turn ends -- the directive replaces them. For
a lens that is fine: it reads, judges, reports. For an implementer on a local
model it was the defect behind most of what the go-live runs measured. An
implementer forgot every file it had read at each turn's end and read them
again, until the drift check stopped it for repeating itself; one wrote a whole
translation file back from the first page it had seen; and none could keep the
code it was changing in view long enough to change it. Turnstone runs the same
model well with none of this: one conversation per agent, the provider's own
tool calling, and tool descriptions written against the ways a local model
goes wrong.

So an implementer is now one conversation (``policy.implementer_loop =
"conversation"``, on a provider that takes tools natively):

* what it reads stays in the conversation, compacted only when it nears the
  context it has (:func:`compact`);
* it acts through native tool calls, and ends a stretch of work by calling
  ``report`` -- the claim supervision and the verifier then test;
* supervision still sees it: at each ``report``, and every
  :data:`CHECKPOINT_CALLS` tool calls without one, the stretch becomes a turn
  on the record (:func:`stint_payload`) and is assessed and answered as any
  turn is. The directive is appended to the conversation, not swapped in for it.

Only what an implementer does changes. What it may do -- the toolbox, its
fences, the floor -- and how its work is checked are exactly as before.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

from ..config import Policy
from ..models import AgentSpec, Usage
from ..providers.base import ChatMessage, ToolCall
from .tools import COMMAND_KINDS, WRITE_KINDS

#: Tool calls in one stretch of work before supervision looks without waiting
#: for a report. The turn contract forced an answer every six; a change that
#: needs a search, three reads, two edits and a test run is already seven.
CHECKPOINT_CALLS = 15

#: The conversation is compacted above this many characters -- about half the
#: 131k-token context the local model is run with, leaving room to think and
#: to read again what was compacted.
CONTEXT_CHARS = 240_000

#: Tool results kept whole by compaction: the most recent ones, which are what
#: the agent is working from.
KEEP_RECENT_RESULTS = 8

COMPACTED = ("[earlier tool result removed to keep the conversation within its "
             "context; call the tool again if you still need it]")

#: Text-only answers in a row before the stretch is handed to supervision.
MAX_IDLE_ANSWERS = 2

IMPLEMENTER_SYSTEM = """\
You are an implementer in a supervised run. You change code through tools, and \
you are finished only when the change is made: reading files changes nothing.

Work like this:
1. Find the code: search, list_files.
2. Read each file you will change, with read_file.
3. Make the change: edit_file for a file that exists, write_file only for a new one.
4. If you can run the project's tests, run them with run_command and fix what fails.
5. Call report.

Rules:
- Change only files inside your scope. Never run git.
- Do not stop to ask permission for something inside your scope. Creating a test \
file is part of the job.
- If something you cannot fix yourself blocks you -- a file outside your scope, a \
missing dependency -- call report with status "blocked" and say exactly what.
- Your report is a claim. An independent verifier and the harness check every \
criterion, so claim only what you did.
"""

NUDGE = ("Act through the tools: read what you need, make the change with edit_file "
         "or write_file, then call report. If you are done, call report now.")

VERIFIER_SYSTEM = """\
You are an independent verifier in a supervised run. An implementer says its task \
is done; you establish, criterion by criterion, whether that is true.

Work like this:
1. Read the code the criteria are about: search, list_files, read_file.
2. Judge each criterion you are given against the code itself, citing file:line.
3. Call verdict, with one result per criterion.

Rules:
- Judge only from what you read yourself. The implementer's account is a claim.
- A criterion that is partly met fails. There is no partial credit.
- "blocked" is only for a check that cannot be made at all; say why.
- You change nothing.
"""

#: Stretches of reading a verifier gets before its verdict is final as it stands.
VERDICT_STINTS = 2

VERDICT_NOW = ("Your reading time is up. Call verdict now, with a result for every "
               "criterion, from what you have established.")

VERDICT_NUDGE = ("Read the code each criterion is about, then call verdict with a result "
                 "for every criterion. If you have read enough, call verdict now.")


def _spec(name: str, description: str, properties: dict[str, Any],
          required: list[str]) -> dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required}}


REPORT_TOOL = _spec(
    "report",
    "Call when the task is done, or when you cannot go on. Not before you have made "
    "the change. This ends your stretch of work; the supervisor answers it.",
    {
        "status": {"type": "string", "enum": ["done", "blocked"]},
        "summary": {"type": "string",
                    "description": "What you changed, file by file, and why"},
        "criteria": {
            "type": "array",
            "description": "Your claim for each criterion id in 'Done means'",
            "items": {"type": "object", "properties": {
                "criterion_id": {"type": "string"},
                "claim": {"type": "string", "enum": ["met", "not_met", "blocked"]},
                "evidence": {"type": "string",
                             "description": "file:line, or the output you saw"},
            }, "required": ["criterion_id", "claim"]},
        },
        "blocked_on": {"type": "string",
                       "description": "Only when blocked: what you cannot do yourself"},
    },
    ["status", "summary"],
)


VERDICT_TOOL = _spec(
    "verdict",
    "Call once you have judged every criterion you were given. This ends your work.",
    {
        "results": {"type": "array", "items": {"type": "object", "properties": {
            "criterion_id": {"type": "string"},
            "status": {"type": "string", "enum": ["pass", "fail", "blocked"]},
            "evidence": {"type": "string",
                         "description": "The code you read, quoted with file:line"},
        }, "required": ["criterion_id", "status", "evidence"]}},
        "summary": {"type": "string"},
    },
    ["results"],
)

#: Per kind of agent driven as a conversation: its system prompt, the tool that
#: ends a stretch of its work, and what it is told when it only talks.
ROLE = {
    "execution": (IMPLEMENTER_SYSTEM, "report", NUDGE),
    "verification": (VERIFIER_SYSTEM, "verdict", VERDICT_NUDGE),
}


def native_tool_specs(agent: AgentSpec, policy: Policy) -> list[dict[str, Any]]:
    """The tools an implementer is offered natively, with how to use them.

    The descriptions carry the workflow, the way Turnstone's do: a local model
    reads a tool's description at the moment it chooses the tool, and that is
    where "reading alone changes nothing" does its work.
    """
    specs = [
        _spec("search", "Search file contents with a regular expression; returns "
              "path:line matches. Use it to find where something is defined or used.",
              {"pattern": {"type": "string"},
               "glob": {"type": "string", "description": "Limit to files matching this"}},
              ["pattern"]),
        _spec("list_files", "List files in the workspace matching a glob.",
              {"pattern": {"type": "string", "description": "Default **/*"}}, []),
        _spec("read_file", "Read a file, whole. Each line is shown as its number, a "
              "tab, then the line exactly as it is in the file. Read a file before "
              "you change it. Reading changes nothing: after reading, make the change "
              "with edit_file.",
              {"path": {"type": "string"},
               "start": {"type": "integer", "description": "First line, for a huge file"},
               "limit": {"type": "integer"}},
              ["path"]),
    ]
    if agent.kind.value in WRITE_KINDS:
        specs += [
            _spec("edit_file", "Change part of an existing file: replace the one place "
                  "`old` appears with `new`. Copy `old` exactly from read_file: what "
                  "comes after the tab, indentation included. If it appears more than "
                  "once, include more of the lines around it. Use this for every change "
                  "to a file that exists.",
                  {"path": {"type": "string"}, "old": {"type": "string"},
                   "new": {"type": "string"}},
                  ["path", "old", "new"]),
            _spec("write_file", "Create a new file, or replace one entirely. Every line "
                  "you leave out is deleted: to change a file that exists, use edit_file.",
                  {"path": {"type": "string"}, "content": {"type": "string"}},
                  ["path", "content"]),
            _spec("delete_file", "Delete one file in your scope -- a scratch or debug file "
                  "you made and no longer need. Leave nothing behind you did not mean "
                  "to ship.",
                  {"path": {"type": "string"}}, ["path"]),
        ]
    if policy.allow_command_execution and agent.kind.value in COMMAND_KINDS:
        specs.append(_spec(
            "run_command", "Run one of the project's check runners -- pytest, npm test, "
            "make -- to see whether your change works. Name only paths inside your "
            "scope, use no shell operators, and never git.",
            {"command": {"type": "string"}}, ["command"]))
    specs.append(VERDICT_TOOL if agent.kind.value == "verification" else REPORT_TOOL)
    return specs


@dataclass
class Stint:
    """What one stretch of an implementer's work did, for its turn on the record."""

    text: list[str] = field(default_factory=list)
    reasoning: str = ""
    actions: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    read: list[str] = field(default_factory=list)
    tool_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    #: The model call failed and the agent has been ended; nothing to record.
    failed: bool = False

    def record(self, call: ToolCall, ok: bool, path: str) -> None:
        self.tool_calls += 1
        target = str(call.arguments.get("path") or call.arguments.get("command")
                     or call.arguments.get("pattern") or "")
        self.actions.append(f"{call.name} {target}".strip() + ("" if ok else " (refused)"))
        if ok and call.name in ("edit_file", "write_file", "delete_file") and target:
            if target not in self.written:
                self.written.append(target)
        elif ok and path and path not in self.read:
            self.read.append(path)


def as_specified(arguments: dict[str, Any], spec: dict[str, Any] | None) -> dict[str, Any]:
    """Tool-call arguments with a list or object that arrived as JSON text decoded.

    A local model sometimes sends a nested argument as the JSON of it rather than
    the value: in batch F, five of 21 planner conversations proposed
    ``"tasks": "[{...}]"``, a string, and the plan read as having no tasks. Only
    where the tool's own schema says array or object, and the text decodes to
    exactly that; anything else is left as the model sent it.
    """
    properties = ((spec or {}).get("parameters") or {}).get("properties") or {}
    fixed = dict(arguments)
    for key, value in arguments.items():
        wanted = (properties.get(key) or {}).get("type")
        if wanted not in ("array", "object") or not isinstance(value, str):
            continue
        try:
            # The first whole value, ignoring what trails it: the same model
            # closed its list with one bracket too many ("Extra data"), and a
            # strict decode left all of its tasks as text.
            decoded, _ = json.JSONDecoder().raw_decode(value.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, list if wanted == "array" else dict):
            fixed[key] = decoded
    return fixed


def stint_payload(stint: Stint, report: dict[str, Any] | None) -> dict[str, Any]:
    """The stretch as a turn the supervision path already knows how to judge.

    With a ``report``, it is the agent's claim: its status, its summary, its
    claims per criterion. Without one -- a checkpoint -- it is still running,
    and its output is what it said and did, so the drift check judges the
    work, not a summary of it.
    """
    done_so_far = "; ".join(stint.actions[-20:]) or "no tool calls"
    if report is None:
        said = " ".join(t for t in stint.text if t.strip())[-3000:]
        output = (f"{said}\n\n" if said else "") + f"Actions this stretch: {done_so_far}"
        return {"output": output, "status": "running", "files_touched": stint.written,
                "files_read": stint.read, "reasoning": stint.reasoning[-4000:]}
    status = str(report.get("status", "")).lower()
    return {
        "output": f"{report.get('summary', '')}\n\nActions this stretch: {done_so_far}",
        "status": status if status in ("done", "blocked") else "done",
        "blocked_on": str(report.get("blocked_on", "")),
        "criteria_progress": [c for c in (report.get("criteria") or []) if isinstance(c, dict)],
        "files_touched": stint.written,
        "files_read": stint.read,
        "reasoning": stint.reasoning[-4000:],
    }


def compact(messages: list[ChatMessage], limit: int = CONTEXT_CHARS) -> int:
    """Shrink the oldest tool results until the conversation fits; how many were.

    The opening brief, every assistant message and every directive stay: they
    are the task, what the agent decided, and what it was told. Tool results
    are what grows, and the oldest are the ones least likely to still be
    current -- a file read before the agent edited it is stale anyway.
    """
    size = sum(len(m.content) for m in messages)
    if size <= limit:
        return 0
    results = [i for i, m in enumerate(messages) if m.role == "tool"]
    shrunk = 0
    for index in results[:-KEEP_RECENT_RESULTS] if len(results) > KEEP_RECENT_RESULTS else []:
        if size <= limit:
            break
        message = messages[index]
        if message.content == COMPACTED:
            continue
        size -= len(message.content) - len(COMPACTED)
        messages[index] = replace(message, content=COMPACTED)
        shrunk += 1
    return shrunk
