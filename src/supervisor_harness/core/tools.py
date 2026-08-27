"""Workspace tools for autonomously-driven agents.

In host-delegated mode the host's own tools do this work, under the user's own
permission model. In autonomous mode the harness drives a plain chat model, so
it has to supply the tools itself -- otherwise an agent asked to review a file
can only report, correctly, that it was never shown one.

Tools are requested through the JSON turn contract rather than a provider's
native function-calling API, so the same mechanism works identically across
Ollama, OpenRouter and Anthropic.

Everything here is confined to the workspace. Reads cannot escape it, writes are
additionally confined to the agent's declared scope, and command execution is
off unless policy explicitly enables it.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Policy
from ..models import AgentSpec, Scope

# Directories never worth reading and expensive to walk.
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".supervisor", "node_modules", "__pycache__",
    ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".next", ".nuxt", "target", ".idea", ".vscode",
}
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".tar", ".exe", ".dll", ".so", ".dylib", ".woff", ".woff2", ".ttf", ".mp4",
    ".mp3", ".wav", ".bin", ".sqlite3", ".db", ".pyc",
}

MAX_READ_LINES = 400
MAX_MATCHES = 60
MAX_LIST = 200


@dataclass
class ToolResult:
    tool: str
    ok: bool
    output: str

    def render(self) -> str:
        status = "" if self.ok else " (failed)"
        return f"### {self.tool}{status}\n{self.output}"


class Toolbox:
    """The tools an autonomous agent may use, sandboxed to one workspace."""

    def __init__(self, workspace: Path, policy: Policy) -> None:
        self.workspace = Path(workspace).resolve()
        self.policy = policy

    # -- path safety -------------------------------------------------------

    def _resolve(self, raw: str) -> Path | None:
        """Resolve a path inside the workspace, or ``None`` if it escapes."""
        try:
            candidate = (self.workspace / raw.strip().lstrip("/\\")).resolve()
            candidate.relative_to(self.workspace)
        except (ValueError, OSError):
            return None
        return candidate

    def _walk(self) -> list[Path]:
        out: list[Path] = []
        for path in self.workspace.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in BINARY_SUFFIXES:
                continue
            out.append(path)
        return out

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.workspace).as_posix()

    # -- tools -------------------------------------------------------------

    def list_files(self, pattern: str = "**/*") -> ToolResult:
        matches = [
            self._rel(p) for p in self._walk()
            if fnmatch.fnmatch(self._rel(p), pattern) or pattern in ("", "**/*")
        ]
        matches.sort()
        body = "\n".join(matches[:MAX_LIST]) or "(no files matched)"
        if len(matches) > MAX_LIST:
            body += f"\n... and {len(matches) - MAX_LIST} more"
        return ToolResult("list_files", True, body)

    def read_file(self, path: str, start: int = 1, limit: int = MAX_READ_LINES) -> ToolResult:
        target = self._resolve(path)
        if target is None:
            return ToolResult("read_file", False, f"{path!r} is outside the workspace")
        if not target.is_file():
            return ToolResult("read_file", False, f"{path!r} does not exist")
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return ToolResult("read_file", False, f"could not read {path!r}: {exc}")

        start = max(1, int(start or 1))
        limit = max(1, min(int(limit or MAX_READ_LINES), MAX_READ_LINES))
        window = lines[start - 1 : start - 1 + limit]
        numbered = "\n".join(f"{start + i:>5}  {line}" for i, line in enumerate(window))
        suffix = (
            f"\n... ({len(lines) - start + 1 - len(window)} more lines)"
            if start - 1 + len(window) < len(lines)
            else ""
        )
        return ToolResult("read_file", True, f"{self._rel(target)}\n{numbered}{suffix}")

    def search(self, pattern: str, glob: str = "**/*") -> ToolResult:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return ToolResult("search", False, f"invalid pattern: {exc}")

        hits: list[str] = []
        for path in self._walk():
            rel = self._rel(path)
            if glob not in ("", "**/*") and not fnmatch.fnmatch(rel, glob):
                continue
            try:
                for number, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                ):
                    if regex.search(line):
                        hits.append(f"{rel}:{number}: {line.strip()[:180]}")
                        if len(hits) >= MAX_MATCHES:
                            break
            except OSError:
                continue
            if len(hits) >= MAX_MATCHES:
                break
        return ToolResult("search", True, "\n".join(hits) or "(no matches)")

    def write_file(self, path: str, content: str, scope: Scope | None = None) -> ToolResult:
        target = self._resolve(path)
        if target is None:
            return ToolResult("write_file", False, f"{path!r} is outside the workspace")

        rel = target.relative_to(self.workspace).as_posix()
        if scope is not None:
            if any(fnmatch.fnmatch(rel, pat) for pat in scope.forbidden_paths):
                return ToolResult("write_file", False, f"{rel} is a forbidden path for this agent")
            if scope.paths and not any(
                fnmatch.fnmatch(rel, pat) or rel.startswith(pat.rstrip("*/"))
                for pat in scope.paths
            ):
                return ToolResult(
                    "write_file", False,
                    f"{rel} is outside this agent's scope ({', '.join(scope.paths)})",
                )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult("write_file", False, f"could not write {rel}: {exc}")
        return ToolResult("write_file", True, f"wrote {rel} ({len(content)} bytes)")

    def run_command(self, command: str) -> ToolResult:
        if not self.policy.allow_command_execution:
            return ToolResult(
                "run_command", False,
                "command execution is disabled. Set policy.allow_command_execution "
                "to true in the configuration if the harness should run commands "
                "itself. Report the criterion as blocked rather than guessing.",
            )
        try:
            completed = subprocess.run(  # noqa: S602 - explicitly enabled by policy
                command, shell=True, cwd=str(self.workspace),
                capture_output=True, text=True,
                timeout=self.policy.command_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return ToolResult("run_command", False, f"timed out: {command}")
        except OSError as exc:
            return ToolResult("run_command", False, f"could not run {command!r}: {exc}")

        output = (completed.stdout + completed.stderr).strip()[-3000:]
        return ToolResult(
            "run_command", completed.returncode == 0,
            f"$ {command}\nexit={completed.returncode}\n{output}",
        )

    # -- dispatch ----------------------------------------------------------

    def call(self, name: str, args: dict[str, Any], agent: AgentSpec) -> ToolResult:
        """Run one requested tool, enforcing what this agent is allowed to do."""
        name = (name or "").strip()
        writable = agent.kind.value in ("execution",)

        if name == "list_files":
            return self.list_files(str(args.get("pattern", "**/*")))
        if name == "read_file":
            return self.read_file(
                str(args.get("path", "")),
                int(args.get("start", 1) or 1),
                int(args.get("limit", MAX_READ_LINES) or MAX_READ_LINES),
            )
        if name == "search":
            return self.search(str(args.get("pattern", "")), str(args.get("glob", "**/*")))
        if name == "write_file":
            if not writable:
                return ToolResult(
                    "write_file", False,
                    f"a {agent.kind.value} agent may not modify files; report what "
                    "should change instead",
                )
            return self.write_file(
                str(args.get("path", "")), str(args.get("content", "")), agent.scope
            )
        if name == "run_command":
            return self.run_command(str(args.get("command", "")))
        return ToolResult(name or "unknown", False, f"no such tool: {name!r}")


def available_tools(agent: AgentSpec, policy: Policy) -> list[dict[str, str]]:
    """The tool list to advertise in this agent's brief."""
    tools = [
        {"name": "list_files", "args": "pattern (glob, optional)",
         "does": "list files in the workspace"},
        {"name": "read_file", "args": "path, start (optional), limit (optional)",
         "does": "read a file with line numbers"},
        {"name": "search", "args": "pattern (regex), glob (optional)",
         "does": "search file contents, returning path:line matches"},
    ]
    if agent.kind.value == "execution":
        tools.append({"name": "write_file", "args": "path, content",
                      "does": "write a file, within your scope only"})
    if policy.allow_command_execution:
        tools.append({"name": "run_command", "args": "command",
                      "does": "run a shell command in the workspace"})
    return tools


def render_tools_section(agent: AgentSpec, policy: Policy) -> str:
    """The brief section explaining how to call tools."""
    tools = available_tools(agent, policy)
    lines = [
        "You have no view of the workspace except through these tools. Use them "
        "before asserting anything about the code.",
        "",
    ]
    for tool in tools:
        lines.append(f"- `{tool['name']}({tool['args']})` -- {tool['does']}")
    lines += [
        "",
        "To use them, return your JSON with `tool_calls` populated and `status` set "
        'to `"running"`:',
        "",
        '```json',
        '{"tool_calls": [{"tool": "read_file", "args": {"path": "src/auth/login.py"}}],',
        ' "output": "", "findings": [], "status": "running"}',
        "```",
        "",
        "The results come back and you continue. Tool rounds do not consume your "
        "turn budget, so gather what you need before answering. When you have "
        "enough, return your real answer with `tool_calls` empty.",
    ]
    return "\n".join(lines)


def render_results(results: list[ToolResult]) -> str:
    return (
        "## Tool results\n\n"
        + "\n\n".join(r.render() for r in results)
        + "\n\nContinue. Call more tools if you need them, or give your answer now."
    )
