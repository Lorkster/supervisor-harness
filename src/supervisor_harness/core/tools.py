"""Workspace tools for autonomously-driven agents.

In host-delegated mode the host's own tools do this work, under the user's own
permission model. In autonomous mode the harness drives a plain chat model, so
it has to supply the tools itself -- otherwise an agent asked to review a file
can only report, correctly, that it was never shown one.

Tools are requested through the JSON turn contract rather than a provider's
native function-calling API, so the same mechanism works identically across
Ollama, OpenRouter and Anthropic.

``read_file`` and ``write_file`` take a path from the model and are confined to
the workspace by :meth:`Toolbox._resolve`, which refuses any path resolving
outside it. ``list_files`` and ``search`` take no path at all: they report only
what :meth:`Toolbox._walk` yields, and it descends the workspace alone.
``write_file`` is confined further, to the agent's declared scope.

``run_command`` is the one tool here that is *not* confined to the workspace. It
runs a real shell, whose working directory is the workspace but which can name
any path on the machine, and no code below can stop a program that computes a
path rather than naming one -- ``python -c`` is a check runner and an arbitrary
writer in the same breath. What stands in its way is narrower than a sandbox,
and worth stating exactly:

* it does nothing unless ``policy.allow_command_execution`` is set;
* only execution agents may call it, because a shell writes files and granting
  it to a kind denied ``write_file`` would hand back what that check refused;
* an agent with a declared scope may invoke only the check runners in
  :data:`.dod.VERIFY_EXECUTABLES`, and every argument it passes them that could
  name a path must fall inside the scope, with metacharacters and globs refused
  outright because they name paths no token spells;
* an agent with *no* declared scope has none of that fence -- there is nothing
  to check a path against -- and reaches the whole machine.

Verification agents deliberately have no shell at all: a criterion's command is
run by :func:`.dod.verify_command`, itself shell-free and allow-listed, or by
the host, so the agent judging the work cannot change it.
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
from .dod import (
    VERIFY_EXECUTABLES,
    executable_name,
    shell_split,
    unquoted_metacharacter,
)
from .paths import matches_any, scope_relative

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

# Which agent kinds may change the workspace. Analysis, synthesis and
# verification agents observe; only execution changes things. A shell writes
# files, so the two sets must be the same set: granting run_command to a kind
# denied write_file hands back through the shell exactly what the check refused.
# Verification agents used to hold the shell so they could execute a criterion's
# real check; they now report the command instead, and dod.verify_command runs
# it under the same policy switch -- an agent that can rewrite the code it is
# judging is not an independent verifier.
WRITE_KINDS = frozenset({"execution"})
COMMAND_KINDS = WRITE_KINDS

# A bare name that ends in a file suffix -- ``report.md``, ``setup.cfg``. Such a
# token names a file whether or not one is there yet, so it is fenced even when
# nothing on disk answers to it.
_FILE_SUFFIX = re.compile(r"^[\w+.-]+\.[A-Za-z]\w*$")

# Glob characters. A scoped agent may not use them at all: the shell expands
# them before the command runs, so ``rm -rf *`` names every path in the
# workspace while containing no token that is one of those paths. They are not
# in dod's ``_METACHARACTERS`` because a criterion command is run without a
# shell, where a glob is an ordinary literal argument.
_GLOB_CHARACTERS = "*?["

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
            if matches_any(rel, scope.forbidden_paths):
                return ToolResult("write_file", False, f"{rel} is a forbidden path for this agent")
            if scope.paths and not matches_any(rel, scope.paths):
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

    def _path_candidates(self, tokens: list[str]) -> list[str]:
        """The arguments of a command that could name a file.

        The rule is the inverse of the one it replaced. Asking "does this token
        look like a path?" fails open on every name a shell accepts without a
        separator or a suffix: ``rm -rf infra`` and ``cp Makefile Dockerfile``
        both destroy real files through tokens no such test can recognise. So
        every argument is a candidate unless it is demonstrably not a path.

        The one thing still let through is a bare extensionless word that names
        nothing in the workspace -- ``pytest`` in ``python -m pytest src/auth``
        is a module, not a file, and fencing it would refuse every real command.
        What such a word could name is bounded instead by the executable
        allow-list in :meth:`_scope_refusal`: it is a path only once some program
        creates it, and no program that creates files is reachable here.
        """
        candidates: list[str] = []
        for token in tokens[1:]:
            if token.startswith("-"):
                # A flag names a file only in its --flag=path form.
                _, sep, value = token.partition("=")
                if not sep or not value:
                    continue
                token = value
            if not token:
                continue
            if "/" in token or "\\" in token or _FILE_SUFFIX.match(token):
                candidates.append(token)
                continue
            resolved = self._resolve(token)
            if resolved is not None and resolved.exists():
                candidates.append(token)
        return candidates

    def _scope_refusal(self, command: str, scope: Scope) -> str | None:
        """Why this command escapes the agent's fence, or ``None`` if it does not.

        A shell writes as well as reads, so every path a command names is a
        potential write and is held to the same fence ``write_file`` enforces.
        Without this the fence is decorative: an agent confined to ``src/auth/**``
        is refused a write to ``infra/`` and then reaches it with ``sh -c``.

        Only an agent that actually has a fence is checked. Reading paths out of
        a command can never be complete -- a shell computes names this cannot
        see -- so the three ways of naming a path it cannot follow are closed
        rather than inspected: a metacharacter that chains or redirects, a glob
        that expands to paths no token spells, and any executable outside the
        check runners in :data:`.dod.VERIFY_EXECUTABLES`. That last one is what
        puts ``rm``, ``cp``, ``mkdir`` and ``git checkout`` out of reach
        entirely, rather than relying on the fence to catch their arguments.
        """
        if not scope.paths and not scope.forbidden_paths:
            return None

        metacharacter = unquoted_metacharacter(command)
        if metacharacter is not None:
            return (
                f"a command from a scoped agent may not use the shell metacharacter "
                f"{metacharacter!r}: the paths a chained or redirected command touches "
                "cannot be checked against your scope. Run one plain command at a time"
            )

        glob_character = unquoted_metacharacter(command, _GLOB_CHARACTERS)
        if glob_character is not None:
            return (
                f"a command from a scoped agent may not use the glob character "
                f"{glob_character!r}: the shell expands it to paths the command never "
                "names, so they cannot be checked against your scope. Name each path"
            )

        tokens = shell_split(command)
        if not tokens:
            return "the command is empty once tokenised"

        executable = executable_name(tokens[0])
        if executable not in VERIFY_EXECUTABLES:
            return (
                f"a scoped agent may not run {executable!r}: only the project's own "
                f"check runners are reachable from the shell "
                f"({', '.join(sorted(VERIFY_EXECUTABLES))}). Use write_file to change "
                "a file in your scope, or report the command for the host to run"
            )

        for token in self._path_candidates(tokens):
            rel = scope_relative(token, self.workspace.as_posix())
            if rel is None or rel.startswith("../"):
                return f"{token} is outside the workspace"
            if matches_any(rel, scope.forbidden_paths):
                return f"{rel} is a forbidden path for this agent"
            if scope.paths and not matches_any(rel, scope.paths):
                return (
                    f"{rel} is outside this agent's scope "
                    f"({', '.join(scope.paths)})"
                )
        return None

    def run_command(self, command: str, scope: Scope | None = None) -> ToolResult:
        if not self.policy.allow_command_execution:
            return ToolResult(
                "run_command", False,
                "command execution is disabled. Set policy.allow_command_execution "
                "to true in the configuration if the harness should run commands "
                "itself. Report the criterion as blocked rather than guessing.",
            )
        if scope is not None:
            refusal = self._scope_refusal(command, scope)
            if refusal is not None:
                return ToolResult("run_command", False, refusal)
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
        writable = agent.kind.value in WRITE_KINDS
        may_run = agent.kind.value in COMMAND_KINDS

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
            if not may_run:
                # Without this an agent forbidden from write_file could simply
                # write files through the shell instead -- which is why a
                # verification agent, denied writes over the code it judges, is
                # denied the shell too.
                return ToolResult(
                    "run_command", False,
                    f"a {agent.kind.value} agent may not run commands; report the "
                    "command that should be run, and the harness or the host runs it",
                )
            return self.run_command(str(args.get("command", "")), agent.scope)
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
    if agent.kind.value in WRITE_KINDS:
        tools.append({"name": "write_file", "args": "path, content",
                      "does": "write a file, within your scope only"})
    if policy.allow_command_execution and agent.kind.value in COMMAND_KINDS:
        tools.append({"name": "run_command", "args": "command",
                      "does": "run one shell command in the workspace, naming only "
                              "paths within your scope"})
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
