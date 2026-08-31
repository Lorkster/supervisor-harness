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

from typing import Any

from .ids import now_iso
from .models import (
    AgentStatus,
    Checkpoint,
    Decision,
    DoDCriterion,
    DriftAssessment,
    DriftSignal,
    ExecutionTask,
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
VERIFY_METHODS = [m.value for m in VerifyMethod]

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
    },
    "required": ["title", "detail", "severity"],
}

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
                "Exact command for method=command|test. If it selects part of a suite "
                "(-k, -run, -t), name test node ids or set expect to a minimum count: "
                "a filter that matches nothing exits 0 and proves nothing"
            ),
        },
        "expect": {
            "type": "string",
            "description": "What proves it: exit code, output substring, or file state",
        },
        "rubric": {"type": "string", "description": "For method=review: what a pass looks like"},
        "mandatory": {"type": "boolean"},
    },
    "required": ["statement", "method", "mandatory"],
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
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["restated_goal", "mode", "lenses"],
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
                    "scope_paths": {"type": "array", "items": {"type": "string"}},
                    "out_of_scope": {"type": "array", "items": {"type": "string"}},
                    "suggested_role": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "risk": {"type": "string", "enum": SEVERITIES},
                    "effort": {"type": "string", "enum": ["small", "medium", "large"]},
                },
                "required": ["title", "action", "motivation", "rationale_refs", "dod"],
            },
        },
    },
    "required": ["summary", "recommended_mode"],
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


def _enum(value: Any, enum_cls: Any, default: Any) -> Any:
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
            )
        )
    return out


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


def parse_tasks(data: dict[str, Any], run_id: str) -> list[ExecutionTask]:
    out: list[ExecutionTask] = []
    for raw in data.get("tasks") or []:
        if not isinstance(raw, dict) or not str(raw.get("title", "")).strip():
            continue
        out.append(
            ExecutionTask(
                run_id=run_id,
                title=str(raw["title"]).strip(),
                action=str(raw.get("action", "")).strip(),
                motivation=str(raw.get("motivation", "")).strip(),
                rationale_refs=_strs(raw.get("rationale_refs")),
                dod=parse_dod(raw.get("dod")),
                scope=Scope(
                    paths=_strs(raw.get("scope_paths")),
                    out_of_scope=_strs(raw.get("out_of_scope")),
                ),
                suggested_role=str(raw.get("suggested_role", "")).strip(),
                depends_on=_strs(raw.get("depends_on")),
                risk=_enum(raw.get("risk"), Severity, Severity.LOW),
                effort=str(raw.get("effort", "medium")).strip().lower(),
                status=TaskStatus.PROPOSED,
            )
        )
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


def parse_lessons(data: dict[str, Any], run_id: str) -> list[Lesson]:
    out: list[Lesson] = []
    for raw in data.get("lessons") or []:
        if not isinstance(raw, dict) or not str(raw.get("statement", "")).strip():
            continue
        out.append(
            Lesson(
                run_id=run_id,
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


def parse_mode(value: Any, default: RunMode = RunMode.REPORT) -> RunMode:
    return _enum(value, RunMode, default)


def parse_decision(value: Any) -> Decision:
    return _enum(value, Decision, Decision.APPROVE)
