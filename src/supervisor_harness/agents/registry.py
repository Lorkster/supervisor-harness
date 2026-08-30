"""Discover the agents actually available for a run.

Three sources, in descending priority when matching a role to a runner:

1. Agents the host declares at call time (Claude Code's subagent types, Cursor's
   modes) -- these are authoritative because the host can really spawn them.
2. Agent definition files in the workspace (``.claude/agents``, ``.cursor``).
3. The harness's own built-in roles, which always work because they are just
   briefs the host executes generically.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..host.detect import CLAUDE_CODE, CURSOR, HostInfo
from ..models import AgentKind
from .roles import ALL_ROLES, ROLES_BY_ID, Role

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class AvailableAgent:
    """One thing that can actually be given a brief."""

    id: str
    name: str
    source: str            # "host" | "claude-code-file" | "cursor-file" | "builtin"
    description: str = ""
    tools: list[str] = field(default_factory=list)
    model: str = ""
    kind: AgentKind | None = None
    path: str = ""

    @property
    def spawnable(self) -> bool:
        """Host-declared agents can be spawned by name; files are hints."""
        return self.source == "host"


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse the small YAML subset used by agent definition files.

    Deliberately not a YAML parser: these files use flat ``key: value`` pairs
    plus occasional inline lists, and taking a YAML dependency for that is not
    a trade worth making.
    """
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text
    meta: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip().strip("'\"")
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
        elif "," in value and key in ("tools", "allowed-tools"):
            meta[key] = [v.strip() for v in value.split(",") if v.strip()]
        else:
            meta[key] = value
    return meta, text[match.end():]


def _load_agent_files(directory: Path, source: str) -> list[AvailableAgent]:
    if not directory.is_dir():
        return []
    agents: list[AvailableAgent] = []
    for path in sorted(directory.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = _parse_frontmatter(text)
        name = str(meta.get("name") or path.stem)
        tools = meta.get("tools") or meta.get("allowed-tools") or []
        agents.append(
            AvailableAgent(
                id=name,
                name=name,
                source=source,
                description=str(meta.get("description") or body.strip()[:200]),
                tools=tools if isinstance(tools, list) else [str(tools)],
                model=str(meta.get("model", "")),
                path=str(path),
            )
        )
    return agents


def discover_host_agent_files(workspace: Path, host: HostInfo) -> list[AvailableAgent]:
    """Read agent definitions the workspace (or user profile) declares."""
    found: list[AvailableAgent] = []

    if host.name in (CLAUDE_CODE, "unknown"):
        found += _load_agent_files(workspace / ".claude" / "agents", "claude-code-file")
        found += _load_agent_files(Path.home() / ".claude" / "agents", "claude-code-file")

    if host.name in (CURSOR, "unknown"):
        found += _load_agent_files(workspace / ".cursor" / "agents", "cursor-file")
        modes = workspace / ".cursor" / "modes.json"
        if modes.is_file():
            try:
                data = json.loads(modes.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            entries = data.get("modes", data) if isinstance(data, dict) else data
            if isinstance(entries, list):
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    name = str(entry.get("name") or entry.get("id") or "").strip()
                    if name:
                        found.append(
                            AvailableAgent(
                                id=name, name=name, source="cursor-file",
                                description=str(entry.get("description", "")),
                                model=str(entry.get("model", "")), path=str(modes),
                            )
                        )

    # Deduplicate by name, keeping the first (workspace beats user profile).
    seen: set[str] = set()
    unique: list[AvailableAgent] = []
    for agent in found:
        key = agent.name.lower()
        if key not in seen:
            seen.add(key)
            unique.append(agent)
    return unique


def builtin_agents() -> list[AvailableAgent]:
    return [
        AvailableAgent(
            id=role.id, name=role.title, source="builtin",
            description=role.summary, kind=role.kind,
        )
        for role in ALL_ROLES
    ]


def _declared_agent(entry: Any) -> AvailableAgent | None:
    """One entry of a host's declared agent list, or ``None`` if it says nothing.

    The list is a description of what the caller can spawn, and it arrives from
    a model, a CLI flag or a hand-written config, so it is normalised rather
    than trusted to a shape. A bare name is the shape people actually write --
    ``["general-purpose"]`` reads as obviously meant, and it used to raise
    ``AttributeError: 'str' object has no attribute 'get'`` from inside the
    comprehension, which named neither the flag nor the entry at fault.

    An entry that is neither a name nor an object describes no agent at all, so
    it is dropped: a declaration is not a security boundary -- it only decides
    which role binds to which sub-agent type -- and failing a whole run over one
    malformed entry costs more than ignoring it.
    """
    if isinstance(entry, str):
        name = entry.strip()
        return (
            AvailableAgent(id=name, name=name, source="host")
            if name
            else None
        )
    if not isinstance(entry, dict):
        return None

    # A nameless entry is dropped for the same reason. The name is what the
    # host is later asked to spawn -- ``host_agent_type`` on the packet -- so
    # the old fallback to the literal "agent" bound a role to a sub-agent type
    # no host has.
    name = str(entry.get("name") or entry.get("id") or "").strip()
    if not name:
        return None

    tools = entry.get("tools") or []
    return AvailableAgent(
        id=name,
        name=name,
        source="host",
        description=str(entry.get("description", "")),
        tools=[str(t) for t in tools] if isinstance(tools, list) else [],
        model=str(entry.get("model", "")),
    )


class AgentRegistry:
    """What can run, and which of it best fits a given role."""

    def __init__(
        self,
        workspace: Path,
        host: HostInfo,
        host_declared: list[dict[str, Any]] | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.host = host
        self.host_agents: list[AvailableAgent] = [
            agent
            for entry in (host_declared or [])
            if (agent := _declared_agent(entry)) is not None
        ]
        self.file_agents = discover_host_agent_files(self.workspace, host)
        self.builtins = builtin_agents()

    def all(self) -> list[AvailableAgent]:
        return [*self.host_agents, *self.file_agents, *self.builtins]

    def spawnable_names(self) -> list[str]:
        return [a.name for a in self.host_agents]

    # -- matching ----------------------------------------------------------

    def match(self, role: Role | str) -> AvailableAgent | None:
        """Best host-spawnable agent for a role, or None to brief generically.

        Preference order: an explicit hint from the role, then a name or
        description overlap, then the host's own general-purpose agent.
        """
        role_obj = ROLES_BY_ID.get(role) if isinstance(role, str) else role
        if role_obj is None:
            return None

        candidates = self.host_agents or self.file_agents
        if not candidates:
            return None

        by_name = {a.name.lower(): a for a in candidates}

        for hint in role_obj.host_agent_hints:
            hit = by_name.get(hint.lower())
            if hit is not None:
                return hit

        needles = {role_obj.id, *role_obj.id.split("-")}
        for agent in candidates:
            haystack = f"{agent.name} {agent.description}".lower()
            if any(n in haystack for n in needles if len(n) > 3):
                return agent

        for fallback in ("general-purpose", "claude", "agent"):
            hit = by_name.get(fallback)
            if hit is not None:
                return hit
        return None

    def describe(self) -> dict[str, Any]:
        return {
            "host": self.host.name,
            "host_declared": [a.name for a in self.host_agents],
            "from_files": [{"name": a.name, "source": a.source} for a in self.file_agents],
            "builtin_roles": [a.id for a in self.builtins],
        }
