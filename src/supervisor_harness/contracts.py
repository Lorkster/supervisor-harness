"""Structured output contracts.

Every agent, and every supervisory stage, answers in a schema defined here. That
is what makes supervision mechanical rather than impressionistic: drift scores,
definitions of done and checkpoint verdicts are all parsed fields, not prose the
supervisor has to interpret twice.

Each schema has a matching ``parse_*`` function that turns the raw dict into
domain objects and tolerates the small deviations models actually make -- a
missing optional list, a severity spelled with different case, a number arriving
as a string.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, TypeVar

from .ids import now_iso
from .models import (
    AcceptanceTests,
    AgentStatus,
    Checkpoint,
    Decision,
    DoDCriterion,
    DriftAssessment,
    DriftSignal,
    ExecutionTask,
    Fact,
    Finding,
    Lesson,
    LessonCategory,
    Message,
    MessageKind,
    RunMode,
    Scope,
    Severity,
    TaskStatus,
    VerifyMethod,
)

SEVERITIES = [s.value for s in Severity]
# `fails_before` is the harness's own bar (`core/fails_before.py`): it is added
# where a task admits it and proven by running the tests, so it is not a method
# a model is offered to propose.
VERIFY_METHODS = [m.value for m in VerifyMethod if m is not VerifyMethod.FAILS_BEFORE]

# --------------------------------------------------------------------------
# Shared fragments
# --------------------------------------------------------------------------

_FINDING = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "One line, specific, not a category name"},
        "detail": {"type": "string", "description": "What is true and why it matters"},
        "severity": {"type": "string", "enum": SEVERITIES},
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "file:line references, command output, or quoted code",
        },
        "recommendation": {"type": "string", "description": "The concrete next action"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "tags": {"type": "array", "items": {"type": "string"}},
        "location": {
            "type": "string",
            "description": "Where it is: path/to/file.ext:start-end, relative to the "
                           "workspace root. Omit if it has no single place",
        },
        "cwe": {"type": "string", "description": "The CWE id, for a security weakness"},
    },
    "required": ["title", "detail", "severity"],
}

# `path/to/file.ext:12` or `...:12-18`. The whole string, not a search: this is
# a field the agent was asked to fill in that shape, and anything else is left
# empty rather than guessed at.
_LOCATION = re.compile(r"\s*`?(?P<path>[^`:\s][^`]*?):(?P<start>\d+)(?:-(?P<end>\d+))?`?\s*")
_CWE = re.compile(r"\s*CWE[-_ ]?(?P<n>\d{1,5})\s*", re.IGNORECASE)

_MESSAGE = {
    "type": "object",
    "properties": {
        "recipient": {
            "type": "string",
            "description": "Another agent's id, '*' to broadcast, or 'supervisor'",
        },
        "kind": {"type": "string", "enum": [k.value for k in MessageKind]},
        "subject": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["recipient", "kind", "content"],
}

_DOD = {
    "type": "object",
    "properties": {
        "statement": {
            "type": "string",
            "description": "A single condition, objectively checkable, no conjunctions",
        },
        "method": {"type": "string", "enum": VERIFY_METHODS},
        "command": {
            "type": "string",
            "description": (
                "Exact command for method=command|test, one command with no &&, | or ;. "
                "Empty for inspection and review: the harness reads files itself. If it "
                "selects part of a suite (-k, -run, -t), name test node ids or set expect "
                "to a minimum count: a filter that matches nothing exits 0 and proves "
                "nothing"
            ),
        },
        "expect": {
            "type": "string",
            "description": (
                "What proves it: an exit code or output substring for a command; for "
                "method=inspection, 'path/to/file: text that must be present' -- the "
                "file and the exact text, not a grep"
            ),
        },
        "rubric": {"type": "string", "description": "For method=review: what a pass looks like"},
        "mandatory": {"type": "boolean"},
    },
    # `command` and `expect` are required, and empty where they do not apply. A
    # model decoding against this schema leaves an optional field out: in batch
    # F's first measurement no `test` criterion of the one-shot planner carried
    # a command, and every one went back as unenforceable.
    "required": ["statement", "method", "command", "expect", "mandatory"],
}


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

_TOOL_CALLS = {
    "type": "array",
    "description": (
        "Tools to run before you answer. Leave it empty when you are answering. "
        "Only meaningful when the harness drives you directly; a host-run agent "
        "uses its host's own tools and leaves this empty."
    ),
    "items": {
        "type": "object",
        "properties": {
            "tool": {"type": "string"},
            "args": {"type": "object"},
        },
        "required": ["tool"],
    },
}


#: What a turn cost. Optional on every schema that carries it, because a host
#: that cannot measure a figure should omit it rather than invent one -- but
#: without the field there is no way to report it at all, and there was no such
#: field anywhere in this module. ``Budget`` declares ceilings on tokens, seconds
#: and tool calls; on the default host backend ``state.usage`` stayed empty, so
#: those three ceilings could not be enforced even in principle. The harness
#: fills this in itself when it drives the model, from the provider's own count.
_USAGE = {
    "type": "object",
    "description": (
        "What this turn cost, if you can measure it. Omit any figure you cannot "
        "-- an absent number is read as unknown, a wrong one is read as fact."
    ),
    "properties": {
        "input_tokens": {"type": "integer"},
        "output_tokens": {"type": "integer"},
        "seconds": {"type": "number"},
        "tool_calls": {"type": "integer"},
    },
}


ANALYSIS_TURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "usage": _USAGE,
        "reasoning": {"type": "string", "description": "How you reached these conclusions"},
        "output": {"type": "string", "description": "Your analysis, written for a reader"},
        "findings": {"type": "array", "items": _FINDING},
        "files_examined": {"type": "array", "items": {"type": "string"}},
        "tool_calls": _TOOL_CALLS,
        "messages": {"type": "array", "items": _MESSAGE},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "established": {
            "type": "array",
            "description": (
                "Facts you have established that the other agents in this run "
                "need. Only things you checked yourself and can cite: an "
                "assertion with no evidence is a finding you have not made yet, "
                "and one with no evidence is dropped. Key them by the thing they "
                "are about ('counter store', 'login entrypoint'), not by a "
                "sentence, so another agent's claim about the same thing can be "
                "compared with yours."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "What it is about"},
                    "statement": {"type": "string", "description": "What you established"},
                    "evidence": {"type": "string", "description": "file:line, or real output"},
                    "anchor": {
                        "type": "string",
                        "description": (
                            "The one file this is about, if it is about a file. "
                            "Other agents are editing this tree while you work, "
                            "so naming it lets a later reader be told the file "
                            "changed after you looked."
                        ),
                    },
                },
                "required": ["key", "statement", "evidence"],
            },
        },
        "status": {
            "type": "string",
            "enum": ["running", "blocked", "done"],
            "description": "'done' only when every objective is addressed",
        },
        "self_assessment": {
            "type": "string",
            "description": "Honestly: what did you not manage to establish?",
        },
        "blocked_on": {"type": "string"},
    },
    "required": ["output", "findings", "status"],
}

EXECUTION_TURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "usage": _USAGE,
        "reasoning": {"type": "string"},
        "output": {"type": "string", "description": "What you changed and why"},
        "files_touched": {"type": "array", "items": {"type": "string"}},
        "commands_run": {"type": "array", "items": {"type": "string"}},
        "criteria_progress": {
            "type": "array",
            "description": "Your claim per criterion. The verifier decides, not you.",
            "items": {
                "type": "object",
                "properties": {
                    "criterion_id": {"type": "string"},
                    "claim": {"type": "string", "enum": ["met", "not_met", "blocked"]},
                    "evidence": {"type": "string"},
                },
                "required": ["criterion_id", "claim"],
            },
        },
        "tool_calls": _TOOL_CALLS,
        "messages": {"type": "array", "items": _MESSAGE},
        "status": {"type": "string", "enum": ["running", "blocked", "done"]},
        "self_assessment": {"type": "string"},
        "blocked_on": {"type": "string"},
        "needs": {"type": "string", "enum": ["work", "owner"]},
        "waiting_for": {"type": "string"},
    },
    "required": ["output", "status"],
}

PLANNING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "usage": _USAGE,
        "restated_goal": {"type": "string", "description": "The task in your own words"},
        "mode": {
            "type": "string",
            "enum": ["report", "execute"],
            "description": "'report' if the answer is analysis; 'execute' if work is needed",
        },
        "lenses": {
            "type": "array",
            "description": "Analysis lenses to run, each with a sharpened objective set",
            "items": {
                "type": "object",
                "properties": {
                    "role": {"type": "string"},
                    "why": {"type": "string"},
                    "objectives": {"type": "array", "items": {"type": "string"}},
                    "scope_paths": {"type": "array", "items": {"type": "string"}},
                    "out_of_scope": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["role", "why", "objectives"],
            },
        },
        "shared_context": {
            "type": "string",
            "description": "Facts every agent needs: stack, conventions, constraints",
        },
        "envelope_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Every path this whole run may modify, as workspace-relative globs. "
                "No agent and no task will be allowed outside it, so draw it around "
                "the work rather than around one lens. Omit it to leave the run "
                "bounded by configuration alone."
            ),
        },
        "envelope_forbidden_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Paths no agent in this run may modify, whatever its own scope says",
        },
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["restated_goal", "mode", "lenses"],
}

REQUIREMENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "requirements": {
            "type": "array",
            "description": (
                "What the request requires of the result: each one thing someone could "
                "check when they try it -- a behaviour, a rule, an interface, a limit"
            ),
            "items": {
                "type": "object",
                "properties": {
                    "quote": {
                        "type": "string",
                        "description": (
                            "The words that state it, copied exactly from the source; "
                            "shorten a long passage with ... but change no word"
                        ),
                    },
                    "source": {
                        "type": "string",
                        "description": ("Where the quote is: 'request', or the workspace "
                                        "path of the document, such as docs/SPEC.md"),
                    },
                    "whole": {
                        "type": "boolean",
                        "description": (
                            "true when it can only hold once the whole result is built -- "
                            "a final gate, a 'done when', something every part contributes "
                            "to; false when one part of the work can deliver it"
                        ),
                    },
                },
                "required": ["quote", "source", "whole"],
            },
        },
    },
    "required": ["requirements"],
}

_REASONED: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "requirement": {"type": "string", "description": "Its id, such as R4"},
            "reason": {"type": "string"},
        },
        "required": ["requirement", "reason"],
    },
}

SYNTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "usage": _USAGE,
        "summary": {"type": "string", "description": "What the analysis established, in prose"},
        "conflicts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Where two lenses disagree, and which you judge correct",
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "recommended_mode": {"type": "string", "enum": ["report", "execute"]},
        "goal": {
            "type": "array",
            "minItems": 1,
            "description": (
                "What the result must be for whoever asked: the few things they would "
                "check first when they try it -- what they would see or be able to do -- "
                "each delivered by one task. Include what is too obvious to write "
                "anywhere else: a game must look like that game, a page must open, a "
                "command must run. Each is checked when its task is done"
            ),
            "items": {
                "type": "object",
                "properties": {
                    "statement": {
                        "type": "string",
                        "description": (
                            "One expectation, as a person trying the result would put "
                            "it -- for a command-line CSV converter: 'converting a CSV "
                            "file prints one JSON object per row'"
                        ),
                    },
                    "delivered_by": {"type": "string",
                                     "description": "The title of the task that delivers it"},
                    "look": {
                        "type": "string",
                        "description": (
                            "When it can be seen on a page this project builds: the page "
                            "to open, relative to the repository, as it is after `build` "
                            "-- for example dist/index.html; prefix it with file: when it "
                            "must work opened straight from disk. The path alone, no words "
                            "around it. Empty when there is no page"
                        ),
                    },
                    "whole": {
                        "type": "boolean",
                        "description": (
                            "true when it holds only once the whole result is built -- a "
                            "playable game, a working app; its task must come after every "
                            "task that builds a part of it"
                        ),
                    },
                    "build": {
                        "type": "string",
                        "description": "The command that builds that page; empty if none",
                    },
                },
                "required": ["statement", "delivered_by", "look", "build", "whole"],
            },
        },
        "tasks": {
            "type": "array",
            "description": "Proposed execution tasks. Empty when the deliverable is a report.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "action": {
                        "type": "string",
                        "description": "Concretely what will be done, in imperative form",
                    },
                    "motivation": {
                        "type": "string",
                        "description": "Why this is worth doing, tied to a finding",
                    },
                    "rationale_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Ids of the findings this task closes, copied verbatim "
                            "(fnd_...). Required: a task that closes no finding is "
                            "work nothing asked for, and a finding no task names is "
                            "reported as still open when the run ends"
                        ),
                        "minItems": 1,
                    },
                    "dod": {"type": "array", "items": _DOD, "minItems": 2},
                    "scope_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": (
                            "The files this task changes or creates, or the directories "
                            "it works in. Never empty: an empty scope lets the task "
                            "change the whole workspace"
                        ),
                    },
                    "out_of_scope": {"type": "array", "items": {"type": "string"}},
                    "suggested_role": {"type": "string"},
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Titles of the tasks that must finish first; [] if none",
                    },
                    # Undescribed, a local model read it as difficulty: the Minesweeper
                    # plan rated a new project's pure game engine "high", and under
                    # envelope approval a high-risk task waits for the owner.
                    "risk": {
                        "type": "string", "enum": SEVERITIES,
                        "description": (
                            "The harm if this change is wrong: to security, to people's "
                            "data, to code or users that already rely on what it changes, "
                            "or by something that cannot be undone. Not how hard it is -- "
                            "effort says that. New code nothing relies on yet is low"
                        ),
                    },
                    "effort": {"type": "string", "enum": ["small", "medium", "large"]},
                    "requirements": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": ("The ids of the listed requirements this task "
                                        "delivers, such as R3; [] if none"),
                    },
                    "acceptance_tests": {
                        "type": "array",
                        "description": (
                            "When this task writes the acceptance tests for requirements "
                            "another task delivers: per command, the ids its tests cover. "
                            "Each test's title carries its id in brackets, like "
                            "'[R3] ...'. [] if this task writes none"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "requirements": {"type": "array", "items": {"type": "string"}},
                                "command": {
                                    "type": "string",
                                    "description": "One command that runs those tests",
                                },
                            },
                            "required": ["requirements", "command"],
                        },
                    },
                },
                # `scope_paths` and `depends_on` are required for the reason a
                # criterion's command is: decoding against the schema, the
                # one-shot planner left every task's scope empty -- read as the
                # whole workspace, in 39 of 70 go-live tasks -- and declared no order.
                "required": ["title", "action", "motivation", "rationale_refs", "dod",
                             "scope_paths", "depends_on"],
            },
        },
        "set_aside": {
            **_REASONED,
            "description": ("Listed requirements no task will deliver, each with why; "
                            "shown to the owner"),
        },
        "not_tested": {
            **_REASONED,
            "description": ("Delivered requirements no other task will test, each with "
                            "why; shown to the owner"),
        },
    },
    "required": ["summary", "recommended_mode", "goal"],
}

DRIFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "on_task": {"type": "boolean"},
        "drift_score": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "0 = squarely on brief, 1 = working on something else entirely",
        },
        "signals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "detail": {"type": "string"},
                    "severity": {"type": "string", "enum": SEVERITIES},
                },
                "required": ["kind", "detail"],
            },
        },
        "summary": {"type": "string"},
        "directive": {
            "type": "string",
            "enum": ["continue", "refocus", "narrow", "deepen", "accept", "reject", "stop"],
        },
        "corrections": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific instructions that put the agent back on brief",
        },
    },
    "required": ["on_task", "drift_score", "directive"],
}

VERIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "usage": _USAGE,
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["pass", "fail", "blocked"]},
                    "evidence": {
                        "type": "string",
                        "description": "First-hand proof: actual command output or quoted code",
                    },
                    "belongs_to": {"type": "string", "description": (
                        "Only when it fails because a later task in the plan builds that part, "
                        "not this one: that task's title, from the plan. Otherwise leave it out")},
                },
                "required": ["criterion_id", "status", "evidence"],
            },
        },
        "summary": {"type": "string"},
        "regressions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["results"],
}

CHECKPOINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "usage": _USAGE,
        "quality": {"type": "number", "minimum": 0, "maximum": 1},
        "scope_fidelity": {
            "type": "number", "minimum": 0, "maximum": 1,
            "description": "Did the work stay inside what was approved?",
        },
        "completeness": {"type": "number", "minimum": 0, "maximum": 1},
        "passed": {"type": "boolean"},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "remediation": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific corrective instructions for the agents that fell short",
        },
        "avoidable_causes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Causes that better briefing or process could have prevented",
        },
        "summary": {"type": "string"},
    },
    "required": ["quality", "scope_fidelity", "completeness", "passed", "summary"],
}

LESSONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "usage": _USAGE,
        "lessons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": [c.value for c in LessonCategory]},
                    "trigger": {"type": "string", "description": "What was observed in this run"},
                    "statement": {"type": "string", "description": "The reusable lesson"},
                    "why": {"type": "string"},
                    "how_to_apply": {
                        "type": "string",
                        "description": "A concrete change to a brief, scope, DoD or route",
                    },
                    "target": {
                        "type": "string",
                        "description": "Role id, 'supervisor', 'dod', or '*' for all",
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["category", "trigger", "statement", "how_to_apply", "target"],
            },
        }
    },
    "required": ["lessons"],
}


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------


EnumT = TypeVar("EnumT", bound=Enum)


def _enum(value: Any, enum_cls: type[EnumT], default: EnumT) -> EnumT:
    """Coerce a model's string to a member of ``enum_cls``, or fall back.

    Generic rather than `Any`, so a caller declaring `-> AgentStatus` is
    actually held to it: with `Any` the three parsers below type-checked
    whatever they returned.
    """
    try:
        return enum_cls(str(value).strip().lower())
    except (ValueError, AttributeError):
        return default


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _strs(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def parse_findings(data: dict[str, Any], agent_id: str, lens: str) -> list[Finding]:
    out: list[Finding] = []
    for raw in data.get("findings") or []:
        if not isinstance(raw, dict) or not str(raw.get("title", "")).strip():
            continue
        out.append(
            Finding(
                agent_id=agent_id,
                lens=lens,
                severity=_enum(raw.get("severity"), Severity, Severity.MEDIUM),
                title=str(raw["title"]).strip(),
                detail=str(raw.get("detail", "")).strip(),
                evidence=_strs(raw.get("evidence")),
                recommendation=str(raw.get("recommendation", "")).strip(),
                confidence=min(1.0, max(0.0, _num(raw.get("confidence"), 0.6))),
                tags=_strs(raw.get("tags")),
                **_location(raw.get("location")),
                cwe=_cwe(raw.get("cwe")),
            )
        )
    return out


def _location(value: Any) -> dict[str, Any]:
    """``path``, ``line_start`` and ``line_end`` from ``path:start-end``, or nothing."""
    match = _LOCATION.fullmatch(str(value or ""))
    if not match:
        return {}
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    if start < 1 or end < start:
        return {}
    return {"path": match.group("path").strip().replace("\\", "/"),
            "line_start": start, "line_end": end}


def _cwe(value: Any) -> str:
    """``CWE-89`` from ``CWE-89``, ``cwe_89`` or ``89``; anything else is empty."""
    text = str(value or "").strip()
    if text.isdigit():
        return f"CWE-{int(text)}"
    match = _CWE.fullmatch(text)
    return f"CWE-{int(match.group('n'))}" if match else ""


def parse_messages(data: dict[str, Any], run_id: str, sender: str) -> list[Message]:
    out: list[Message] = []
    for raw in data.get("messages") or []:
        if not isinstance(raw, dict) or not str(raw.get("content", "")).strip():
            continue
        out.append(
            Message(
                run_id=run_id,
                sender=sender,
                recipient=str(raw.get("recipient", "*")).strip() or "*",
                kind=_enum(raw.get("kind"), MessageKind, MessageKind.NOTE),
                subject=str(raw.get("subject", "")).strip(),
                content=str(raw["content"]).strip(),
                refs=_strs(raw.get("refs")),
            )
        )
    return out


def parse_tool_calls(data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Extract requested tool calls, ignoring malformed entries."""
    out: list[tuple[str, dict[str, Any]]] = []
    for raw in data.get("tool_calls") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("tool", "")).strip()
        if not name:
            continue
        args = raw.get("args")
        out.append((name, args if isinstance(args, dict) else {}))
    return out


def parse_status(value: Any) -> AgentStatus:
    return _enum(value, AgentStatus, AgentStatus.RUNNING)


def parse_goal(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The plan's goal expectations, each with a statement; the rest dropped."""
    out: list[dict[str, Any]] = []
    for raw in data.get("goal") or []:
        if not isinstance(raw, dict) or not str(raw.get("statement", "")).strip():
            continue
        item: dict[str, Any] = {key: str(raw.get(key, "")).strip()
                                for key in ("statement", "delivered_by", "look", "build")}
        item["whole"] = (raw.get("whole") is True
                         or str(raw.get("whole", "")).strip().lower() == "true")
        out.append(item)
    return out


def parse_quoted(data: dict[str, Any]) -> list[dict[str, Any]]:
    """A reader's requirements, each a quote, its source and whether it is the whole
    result's; the rest dropped."""
    out: list[dict[str, Any]] = []
    for raw in data.get("requirements") or []:
        if not isinstance(raw, dict) or not str(raw.get("quote", "")).strip():
            continue
        out.append({"quote": str(raw["quote"]).strip(),
                    "source": str(raw.get("source", "")).strip() or "request",
                    "whole": raw.get("whole") is True
                    or str(raw.get("whole", "")).strip().lower() == "true"})
    return out


_REQUIREMENT_ID = re.compile(r"\s*\[?\s*R?\s*0*(\d{1,4})\s*\]?\s*", re.IGNORECASE)


def requirement_id(value: Any) -> str:
    """``R7`` from ``R7``, ``r07``, ``[R7]`` or ``7``; anything else is empty."""
    match = _REQUIREMENT_ID.fullmatch(str(value or ""))
    return f"R{int(match.group(1))}" if match else ""


def requirement_ids(value: Any) -> list[str]:
    """The requirement ids in a list, normalised, in order, once each."""
    out: list[str] = []
    for item in _strs(value):
        rid = requirement_id(item)
        if rid and rid not in out:
            out.append(rid)
    return out


def parse_reasoned(value: Any) -> dict[str, str]:
    """``set_aside`` or ``not_tested``: requirement id to the reason given."""
    out: dict[str, str] = {}
    for raw in value or []:
        if not isinstance(raw, dict):
            continue
        rid = requirement_id(raw.get("requirement"))
        if rid:
            out[rid] = str(raw.get("reason", "")).strip()
    return out


def parse_acceptance(value: Any) -> list[AcceptanceTests]:
    """A task's acceptance tests: each command, with the requirements it covers."""
    out: list[AcceptanceTests] = []
    for raw in value or []:
        if not isinstance(raw, dict):
            continue
        ids = requirement_ids(raw.get("requirements"))
        if ids:
            out.append(AcceptanceTests(requirements=ids,
                                       command=str(raw.get("command", "")).strip()))
    return out


def acceptance_criterion(tests: AcceptanceTests) -> DoDCriterion:
    """The criterion that runs one group of acceptance tests, for the task writing them."""
    return DoDCriterion(
        statement=f"The acceptance tests for {', '.join(tests.requirements)} pass",
        method=VerifyMethod.TEST, command=tests.command, expect="0", mandatory=True,
    )


def parse_dod(items: Any) -> list[DoDCriterion]:
    out: list[DoDCriterion] = []
    for raw in items or []:
        if not isinstance(raw, dict) or not str(raw.get("statement", "")).strip():
            continue
        method = _enum(raw.get("method"), VerifyMethod, VerifyMethod.INSPECTION)
        out.append(
            DoDCriterion(
                statement=str(raw["statement"]).strip(),
                method=method,
                command=str(raw.get("command", "")).strip(),
                expect=str(raw.get("expect", "")).strip(),
                rubric=str(raw.get("rubric", "")).strip(),
                mandatory=bool(raw.get("mandatory", True)),
            )
        )
    return out


def parse_tasks(data: dict[str, Any], run_id: str, workspace: str = "") -> list[ExecutionTask]:
    """The planned tasks; scope paths made workspace-relative (`relative_patterns`)."""
    from .core.paths import relative_patterns

    out: list[ExecutionTask] = []
    for raw in data.get("tasks") or []:
        if not isinstance(raw, dict) or not str(raw.get("title", "")).strip():
            continue
        title = str(raw["title"]).strip()
        action = str(raw.get("action", "")).strip()
        # Measured on a local model: the change's kind -- "create", "modify" --
        # where the schema asks what will concretely be done. That one word was
        # the implementer's first objective and what its brief said was supposed
        # to happen, and the drift judge read it against the whole request. The
        # title says what the task is.
        if len(action.split()) < 2:
            action = title
        out.append(
            ExecutionTask(
                run_id=run_id,
                title=title,
                action=action,
                motivation=str(raw.get("motivation", "")).strip(),
                rationale_refs=_strs(raw.get("rationale_refs")),
                dod=parse_dod(raw.get("dod")),
                scope=Scope(
                    paths=relative_patterns(_strs(raw.get("scope_paths")), workspace),
                    out_of_scope=_strs(raw.get("out_of_scope")),
                ),
                suggested_role=str(raw.get("suggested_role", "")).strip(),
                depends_on=_strs(raw.get("depends_on")),
                risk=_enum(raw.get("risk"), Severity, Severity.LOW),
                effort=str(raw.get("effort", "medium")).strip().lower(),
                requirements=requirement_ids(raw.get("requirements")),
                acceptance=parse_acceptance(raw.get("acceptance_tests")),
                status=TaskStatus.PROPOSED,
            )
        )
        # Here rather than with the rest of batch L, so a command the harness
        # cannot run, or one that runs a file no task writes, is sent back with
        # every other criterion (`phases.unenforceable_criteria`).
        task = out[-1]
        task.dod += [acceptance_criterion(t) for t in task.acceptance if t.command]
    return out


def parse_drift(data: dict[str, Any], checked_by: str) -> DriftAssessment:
    signals = [
        DriftSignal(
            kind=str(raw.get("kind", "model")),
            severity=_enum(raw.get("severity"), Severity, Severity.MEDIUM),
            detail=str(raw.get("detail", "")),
            score=0.0,
        )
        for raw in (data.get("signals") or [])
        if isinstance(raw, dict)
    ]
    return DriftAssessment(
        on_task=bool(data.get("on_task", True)),
        score=min(1.0, max(0.0, _num(data.get("drift_score"), 0.0))),
        signals=signals,
        summary=str(data.get("summary", "")),
        checked_by=checked_by,
    )


def parse_checkpoint(data: dict[str, Any], run_id: str, iteration: int) -> Checkpoint:
    return Checkpoint(
        run_id=run_id,
        iteration=iteration,
        quality=min(1.0, max(0.0, _num(data.get("quality")))),
        scope_fidelity=min(1.0, max(0.0, _num(data.get("scope_fidelity")))),
        completeness=min(1.0, max(0.0, _num(data.get("completeness")))),
        passed=bool(data.get("passed", False)),
        gaps=_strs(data.get("gaps")),
        remediation=_strs(data.get("remediation")),
        avoidable_causes=_strs(data.get("avoidable_causes")),
        summary=str(data.get("summary", "")),
    )


def parse_lessons(data: dict[str, Any], run_id: str, workspace: str = "") -> list[Lesson]:
    out: list[Lesson] = []
    for raw in data.get("lessons") or []:
        if not isinstance(raw, dict) or not str(raw.get("statement", "")).strip():
            continue
        out.append(
            Lesson(
                run_id=run_id,
                workspace=workspace,
                category=_enum(raw.get("category"), LessonCategory, LessonCategory.PROCESS),
                trigger=str(raw.get("trigger", "")).strip(),
                statement=str(raw["statement"]).strip(),
                why=str(raw.get("why", "")).strip(),
                how_to_apply=str(raw.get("how_to_apply", "")).strip(),
                target=str(raw.get("target", "*")).strip() or "*",
                confidence=min(1.0, max(0.0, _num(raw.get("confidence"), 0.5))),
                created_at=now_iso(),
            )
        )
    return out


def parse_established(data: dict[str, Any], agent: Any) -> list[Fact]:
    """The facts this turn claims to have established, normalised and evidenced.

    A claim with no evidence is dropped rather than recorded weakly. That is the
    rule the briefs already state and the one `_report_verification` already
    applies to a criterion marked passed with nothing behind it: this harness
    does not carry unevidenced assertions forward, because everything downstream
    then has to guess which ones it can trust.
    """
    from .core.blackboard import normalise_fact_key

    out: list[Fact] = []
    for raw in data.get("established") or []:
        if not isinstance(raw, dict):
            continue
        key = normalise_fact_key(raw.get("key", ""))
        statement = str(raw.get("statement", "")).strip()
        evidence = str(raw.get("evidence", "")).strip()
        if not (key and statement and evidence):
            continue
        out.append(
            Fact(
                key=key,
                statement=statement,
                evidence=evidence,
                # Taken as written; resolved against the workspace by
                # `core.facts.anchor_fact` at the point the fact is recorded,
                # which is where the workspace is known. Anything that does not
                # land inside the tree is dropped there rather than here.
                anchor=str(raw.get("anchor", "")).strip(),
                agent_id=getattr(agent, "id", ""),
                role=getattr(agent, "role", ""),
            )
        )
    return out


def parse_mode(value: Any, default: RunMode = RunMode.REPORT) -> RunMode:
    return _enum(value, RunMode, default)


def parse_decision(value: Any) -> Decision:
    return _enum(value, Decision, Decision.APPROVE)
