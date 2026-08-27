"""Work out which host the harness is running under.

The harness never *requires* a correct answer -- it degrades to a generic host --
but knowing the host lets it name the right delegation mechanism ("your Task
tool" versus "a Cursor background agent") and read the right agent definitions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

CLAUDE_CODE = "claude-code"
CURSOR = "cursor"
UNKNOWN = "unknown"


@dataclass
class HostInfo:
    name: str = UNKNOWN
    version: str = ""
    workspace: str = ""
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)

    @property
    def delegation_hint(self) -> str:
        """How this host is expected to spawn a subagent."""
        if self.name == CLAUDE_CODE:
            return (
                "Spawn each agent with your Task tool, passing the brief as the prompt "
                "and using the named subagent_type. Run independent agents in parallel "
                "by issuing their Task calls in a single message."
            )
        if self.name == CURSOR:
            return (
                "Spawn each agent as a background agent or a sub-chat, passing the brief "
                "verbatim. Start independent agents together rather than sequentially."
            )
        return (
            "Spawn each agent using whatever sub-agent or parallel-task mechanism you "
            "have, passing the brief verbatim."
        )


def detect_host(workspace: Path | str | None = None) -> HostInfo:
    """Identify the host from environment first, filesystem second."""
    ws = Path(workspace) if workspace else Path.cwd()
    env = os.environ
    evidence: list[str] = []

    # Explicit override always wins; useful in CI and in tests.
    forced = env.get("SUPERVISOR_HOST")
    if forced:
        return HostInfo(name=forced, workspace=str(ws), confidence=1.0,
                        evidence=["SUPERVISOR_HOST"])

    claude_score = 0.0
    for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
        if env.get(var):
            claude_score += 0.5
            evidence.append(f"env:{var}")

    cursor_score = 0.0
    for var in ("CURSOR_TRACE_ID", "CURSOR_AGENT", "CURSOR_WORKSPACE"):
        if env.get(var):
            cursor_score += 0.5
            evidence.append(f"env:{var}")
    if "cursor" in (env.get("TERM_PROGRAM", "") + env.get("__CFBundleIdentifier", "")).lower():
        cursor_score += 0.4
        evidence.append("env:TERM_PROGRAM")

    # Filesystem is weak evidence: a repo can carry config for a host nobody is
    # currently running, so it only breaks ties.
    if (ws / ".claude").is_dir():
        claude_score += 0.2
        evidence.append("dir:.claude")
    if (ws / ".cursor").is_dir():
        cursor_score += 0.2
        evidence.append("dir:.cursor")

    if claude_score == cursor_score == 0:
        return HostInfo(name=UNKNOWN, workspace=str(ws), confidence=0.0, evidence=evidence)

    name = CLAUDE_CODE if claude_score >= cursor_score else CURSOR
    return HostInfo(
        name=name,
        version=env.get("CLAUDE_CODE_VERSION", "") if name == CLAUDE_CODE else "",
        workspace=str(ws),
        confidence=min(1.0, max(claude_score, cursor_score)),
        evidence=evidence,
    )
