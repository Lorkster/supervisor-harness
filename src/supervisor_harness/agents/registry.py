"""Discover the agents actually available for a run.

Three sources, in descending priority when matching a role to a runner:

1. Agents the host declares at call time (Claude Code's subagent types, Cursor's
   modes) -- these are authoritative because the host said so itself.
2. Agent definition files in the workspace (``.claude/agents``, ``.cursor``).
3. The harness's own built-in roles, which always work because they are just
   briefs the host executes generically.

## Which of those can actually be spawned by name

Not the same question as which is most authoritative, and conflating the two is
what made this module quietly useless. Spawnability used to be derived --
``source == "host"`` -- so an agent read from a file was never spawnable, and
the five sites in `core/phases.py` that ask for a match discarded every one they
got. A workspace could define a security specialist, have it discovered, have it
*matched* to the security lens, and still run that lens as a generic agent, with
nothing anywhere saying so.

The premise was wrong for the host it mattered most for. A definition in
``.claude/agents`` is exactly what Claude Code spawns by name through
``subagent_type``: those files are not hints, they *are* the spawnable set.
Cursor modes are the case that motivated the caution and they keep it.

So spawnability is decided where the host is known -- at construction, in
:func:`discover_host_agent_files` and :func:`_declared_agent` -- and carried as
a field rather than inferred later from a source string that does not know which
host is asking.
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
    # Whether *this host* can be asked for this agent by name. Set at
    # construction by whoever knows the host; see the module docstring for why
    # it is not derived from ``source``.
    spawnable: bool = False

    @property
    def origin(self) -> str:
        """Where this came from, for a packet's binding reason."""
        return self.path or self.source


@dataclass(frozen=True)
class AgentBinding:
    """Which sub-agent type runs a role, and why that one.

    The reason exists because the failure this batch fixes was silent: a match
    was found and thrown away, and no packet, log line or report said that it
    had been. A binding that names its own cause turns "no local agent was used"
    from something you have to read the source to discover into something the
    packet tells you.
    """

    agent: AvailableAgent | None = None
    reason: str = ""

    @property
    def name(self) -> str | None:
        """The sub-agent type to spawn, or ``None`` to brief generically."""
        return self.agent.name if self.agent is not None else None


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


def _load_agent_files(
    directory: Path, source: str, *, spawnable: bool = False
) -> list[AvailableAgent]:
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
                spawnable=spawnable,
            )
        )
    return agents


def discover_host_agent_files(workspace: Path, host: HostInfo) -> list[AvailableAgent]:
    """Read agent definitions the workspace (or user profile) declares.

    A ``.claude/agents`` definition is spawnable **only when this really is
    Claude Code**, which is the host that spawns them by name. The files are
    still read under an unknown host -- they are the best description available
    of what the workspace expects -- but they are not claimed as spawnable,
    because naming a sub-agent type to a host that has no such mechanism fails
    the dispatch rather than degrading it.

    Cursor definitions are never spawnable by name. Cursor modes are selected by
    the user rather than addressed by a caller, so a mode name in
    ``host_agent_type`` would be an instruction no host can carry out.
    """
    found: list[AvailableAgent] = []

    if host.name in (CLAUDE_CODE, "unknown"):
        claude_spawnable = host.name == CLAUDE_CODE
        found += _load_agent_files(
            workspace / ".claude" / "agents", "claude-code-file", spawnable=claude_spawnable
        )
        found += _load_agent_files(
            Path.home() / ".claude" / "agents", "claude-code-file", spawnable=claude_spawnable
        )

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
            AvailableAgent(id=name, name=name, source="host", spawnable=True)
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
        spawnable=True,
    )


_TOKENS = re.compile(r"[a-z0-9]+")


def _hint_matches(hint: str, name: str) -> bool:
    """Whether a role's host-agent hint names this agent, loosely.

    Hints are written as bare slugs -- ``"security"``, ``"code-review"`` -- and
    agents are named by whoever wrote the definition file: "Security
    Specialist", "Code Reviewer". Exact-name lookup, which is all this used to
    do, matches neither, so every curated hint in `roles.py` was effectively
    dead and binding fell through to the substring pass beneath it.

    Every token of the hint must *prefix* some token of the name. A prefix
    rather than a substring, so ``security`` reaches "Security Specialist" but
    not "Insecurity Auditor"; every token rather than any, so ``code-review``
    does not bind the first agent with "code" in its name.
    """
    hint_tokens = _TOKENS.findall(hint.lower())
    name_tokens = _TOKENS.findall(name.lower())
    if not hint_tokens or not name_tokens:
        return False
    return all(
        any(token.startswith(part) for token in name_tokens) for part in hint_tokens
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
        """Every agent this host can be asked for by name, in priority order."""
        return [a.name for a in (*self.host_agents, *self.file_agents) if a.spawnable]

    # -- matching ----------------------------------------------------------

    def match(self, role: Role | str) -> AvailableAgent | None:
        """Best available agent for a role, or None if nothing fits.

        Says nothing about whether the host can *spawn* it -- callers building a
        packet want :meth:`bind`, which answers that and says why.
        """
        return self._best(role)[0]

    def bind(self, role: Role | str) -> AgentBinding:
        """The sub-agent type that should run this role, and why.

        Returns a binding whose ``name`` is ``None`` whenever the role must be
        briefed generically, but whose ``reason`` is never empty. The three ways
        to get ``None`` are worth telling apart, and before this they were
        indistinguishable at every call site: nothing was available, something
        was available and nothing matched, or something matched and this host
        cannot spawn it.
        """
        agent, why = self._best(role)
        if agent is None:
            return AgentBinding(None, why)
        if not agent.spawnable:
            return AgentBinding(
                None,
                f"{agent.name} ({agent.origin}) matches -- {why} -- but the "
                f"{self.host.name} host cannot spawn it by name; briefed generically",
            )
        return AgentBinding(agent, f"{agent.name} ({agent.origin}): {why}")

    def _best(self, role: Role | str) -> tuple[AvailableAgent | None, str]:
        """The best candidate for a role and the rule that chose it.

        Preference order: an explicit hint from the role, then a name or
        description overlap, then the host's own general-purpose agent.
        """
        role_obj = ROLES_BY_ID.get(role) if isinstance(role, str) else role
        if role_obj is None:
            return None, f"no such role {role!r}"

        candidates = self.host_agents or self.file_agents
        if not candidates:
            return None, (
                "no agent types declared by the host and no agent definition "
                "files found; briefed generically"
            )

        by_name = {a.name.lower(): a for a in candidates}

        # Each hint is tried in full before the next one, exact name first and
        # then loosely. Trying every hint exactly before trying any of them
        # loosely would let a late hint like "general-purpose" -- which most
        # roles carry as their last resort -- beat an earlier hint that matches
        # a real specialist under a longer name.
        for hint in role_obj.host_agent_hints:
            hit = by_name.get(hint.lower())
            if hit is not None:
                return hit, f"role hint {hint!r} matched the agent's name"
            for agent in candidates:
                if _hint_matches(hint, agent.name):
                    return agent, f"role hint {hint!r} matched {agent.name!r}"

        # Whole words, for the reason the hint pass uses prefixes: a bare
        # substring test binds the security lens to an "Insecurity Auditor",
        # and a role bound to the wrong specialist is worse than one briefed
        # generically, because it looks deliberate.
        needles = {n for n in (role_obj.id, *role_obj.id.split("-")) if len(n) > 3}
        for agent in candidates:
            haystack = f"{agent.name} {agent.description}".lower()
            hit_needle = next(
                (n for n in needles if re.search(rf"\b{re.escape(n)}\b", haystack)), None
            )
            if hit_needle is not None:
                return agent, f"role id {hit_needle!r} appears in its name or description"

        for fallback in ("general-purpose", "claude", "agent"):
            hit = by_name.get(fallback)
            if hit is not None:
                return hit, "nothing specialised matched; the host's general-purpose agent"
        return None, (
            f"none of the {len(candidates)} available agent(s) matched this role; "
            "briefed generically"
        )

    def describe(self) -> dict[str, Any]:
        return {
            "host": self.host.name,
            "host_declared": [a.name for a in self.host_agents],
            "from_files": [
                {"name": a.name, "source": a.source, "spawnable": a.spawnable}
                for a in self.file_agents
            ],
            "spawnable": self.spawnable_names(),
            "builtin_roles": [a.id for a in self.builtins],
        }
