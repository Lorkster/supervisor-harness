"""What the harness hands back: a work packet, and a response to a call.

These are the vocabulary the layers share. They lived on ``core/supervisor.py``
until the split, at which point `core/reporting.py` needed to construct a
``SupervisorResponse`` and importing it from the module that imports *it* would
have been a cycle -- the same shape as finding Q-A1, one level down.

So they live here, in a module that imports nothing from `core`. Both are
re-exported from ``core.supervisor``, which is where the CLI, the MCP server and
the tests have always imported them from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..serde import to_jsonable


@dataclass
class WorkPacket:
    """One unit of work for the host (or the harness) to execute."""

    run_id: str
    agent_id: str
    kind: str
    title: str
    brief: str
    schema: dict[str, Any]
    turn_index: int = 0
    turns_remaining: int = 0
    host_agent_type: str | None = None
    model: str = "host"
    task_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

@dataclass
class SupervisorResponse:
    """What the caller should do next."""

    run_id: str
    phase: str
    action: str          # dispatch | await_reports | await_approval | complete | failed
    message: str = ""
    packets: list[WorkPacket] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    task_notes: dict[str, list[str]] = field(default_factory=dict)
    directive: dict[str, Any] | None = None
    checkpoint: dict[str, Any] | None = None
    report_markdown: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

