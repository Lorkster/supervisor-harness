"""A small MCP server over stdio, standing in for an analysis tool server like rea.

Run by `tests/test_external_tools.py`: ``python fake_tool_server.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("fake-tools")
READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


@server.tool(annotations=READ, description="Count the lines of a local text file.")
def count_lines(input_path: str) -> str:
    return f"{len(Path(input_path).read_text(encoding='utf-8').splitlines())} lines in {input_path}"


@server.tool(annotations=WRITE, description="Delete a local file.")
def delete_file(input_path: str) -> str:
    Path(input_path).unlink()
    return "deleted"


@server.tool(annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False),
             description="Record a finding in this server's session; changes nothing else.")
def record_finding(text: str) -> str:
    return f"recorded: {text}"


@server.tool(annotations=READ, description="This server's tools and what each may change.")
def catalog() -> str:
    def row(name: str, **changes: bool) -> dict[str, object]:
        return {"name": name, "available": True,
                "effects": {"mutatesSession": True, "writesFilesystem": False, **changes}}
    return json.dumps({"tool_availability": [
        row("count_lines"), row("record_finding"), row("catalog"),
        row("delete_file", writesFilesystem=True, mayDiscardData=True)]})


if __name__ == "__main__":
    server.run("stdio")
