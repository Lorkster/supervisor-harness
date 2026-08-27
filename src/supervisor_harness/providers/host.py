"""Host provider: delegate the turn to Claude Code or Cursor.

This is the default backend. The harness stays in charge of *what* should happen
and *whether it was acceptable*, while the host agent -- which already holds the
repository context, the tool permissions and the user's trust -- does the work.

``complete`` never returns; it raises :class:`DelegationRequired` carrying the
packet the host must act on. The MCP layer turns that into a tool result, and
the host reports back through ``supervisor_report``.
"""

from __future__ import annotations

from typing import Any

from .base import CompletionRequest, CompletionResponse, DelegationRequired, Provider


class HostProvider(Provider):
    name = "host"

    def __init__(self, host_name: str = "unknown") -> None:
        self.host_name = host_name

    async def available(self) -> bool:
        return True

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise DelegationRequired(
            {
                "host": self.host_name,
                "system": request.system,
                "messages": [{"role": m.role, "content": m.content} for m in request.messages],
                "json_schema": request.json_schema,
                "instructions": (
                    "Execute this turn yourself using your own tools, then call "
                    "supervisor_report with the result."
                ),
            }
        )

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "host": self.host_name, "delegated": True}


__all__ = ["HostProvider", "DelegationRequired"]
