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
from pathlib import Path
from typing import Any

from ..serde import to_dict


@dataclass
class WorkPacket:
    """One unit of work for the host (or the harness) to execute.

    Carried two ways. **Inline** -- ``brief`` and ``schema`` populated -- is
    what the autonomous backend needs, because the harness feeds them straight
    to a provider and there is no second process to read a file. **By
    reference** -- ``brief_path``, ``contract_path``, ``result_path`` and a
    short ``brief_digest`` -- is the default for a host, and exists because the
    orchestrator is not the reader.

    A supervised run makes the orchestrator the message bus: every brief goes
    out through its context, every result comes back through it, and nothing is
    firewalled the way a native sub-agent firewalls its own transcript. The
    supervisor still needs to see all of it; the orchestrator only needs to know
    which file to hand to whom. Measured on an analysis brief, the inline form
    costs about 9,000 characters of which roughly 5,000 is the pretty-printed
    schema -- per agent, per run, through one context.

    :meth:`read_brief` is how a reader gets the text either way, so nothing has
    to branch on which form it was handed.
    """

    run_id: str
    agent_id: str
    kind: str
    title: str
    brief: str
    schema: dict[str, Any]
    turn_index: int = 0
    turns_remaining: int = 0
    host_agent_type: str | None = None
    # Why this agent type was chosen, or why the packet carries none. Always
    # populated: "briefed generically" is a decision the host should be able to
    # see, not an absence it has to infer.
    host_agent_reason: str = ""
    model: str = "host"
    task_id: str | None = None
    # -- the by-reference form ---------------------------------------------
    #: The brief, written out in full. Empty when the packet is inline.
    brief_path: str = ""
    #: A few lines naming the job, so the orchestrator can dispatch and narrate
    #: without holding the brief. Never a substitute for reading the brief.
    brief_digest: str = ""
    #: The JSON schema the answer must match, written once per run per kind.
    contract_path: str = ""
    #: Where the agent writes its answer, for `supervisor_report`'s result_path.
    result_path: str = ""

    @property
    def by_reference(self) -> bool:
        return bool(self.brief_path)

    def read_brief(self) -> str:
        """The full brief, whichever way this packet carries it."""
        if self.brief:
            return self.brief
        if self.brief_path:
            return Path(self.brief_path).read_text(encoding="utf-8")
        return ""

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

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
    #: One line saying where the run is: phase, agents, turns, findings, elapsed.
    #: Stamped where a response reaches a person, so the host can print it
    #: without deciding what to say. See `Reporting.ledger`.
    ledger: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

