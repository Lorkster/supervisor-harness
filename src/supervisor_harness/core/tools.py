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
``write_file`` is confined further, to the agent's declared scope, and further
again by a floor that holds whether or not the agent declared one: no agent
writes into a version control directory or into the harness's own store. See
:data:`VCS_DIRS`. An agent whose task arrived with no scope used to be confined
by nothing but the workspace, which put ``.git/hooks/pre-commit`` -- a shell
script the user runs at their next commit -- inside the reach of a tool that is
handed out with no policy switch at all.

``run_command`` is the one tool here that is *not* confined to the workspace. It
runs a real shell, whose working directory is the workspace but which can name
any path on the machine, and no code below can stop a program that computes a
path rather than naming one. What stands in its way is narrower than a sandbox,
and worth stating exactly:

* it does nothing unless ``policy.allow_command_execution`` is set;
* only execution agents may call it, because a shell writes files and granting
  it to a kind denied ``write_file`` would hand back what that check refused;
* an agent with a declared scope may invoke only the check runners in
  :data:`.dod.VERIFY_EXECUTABLES`, and every argument it passes them that could
  name a path must fall inside the scope, with metacharacters and globs refused
  outright because they name paths no token spells;
* those check runners may not be handed their program in the command line
  itself: ``python -c`` and ``node -e`` are refused for a scoped agent, because
  source given as a string names its paths only once it is already running;
* an agent with *no* declared scope has none of that fence -- there is nothing
  to check a path against -- and reaches the whole machine;
* two refusals apply to every agent, fenced or not, because neither is about
  this agent's scope: no command may change the shared working tree's git state
  (:func:`tree_wide_git`), and none may name a path under the floor
  (:data:`VCS_DIRS`, :data:`STORE_DIRS`). The floor is complete for a scoped
  agent, whose commands cannot name a path any other way; for an unscoped one it
  catches the command that names what it means and nothing more, since the
  checks that close the other three ways sit behind the same early return.

What that leaves open is worth stating just as exactly, because it is a property
of the design rather than a gap in it: a check runner runs whatever the project
tells it to. ``npm test`` runs a line of ``package.json``, ``make`` runs the
Makefile, ``python -m pip`` and ``npx`` fetch and run code that was never in the
workspace, and any of them can write wherever the harness can. The fence keeps a
*drifting* agent inside its scope, which is what it is for; it does not contain a
hostile one, and a workspace whose own build scripts cannot be trusted needs a
sandbox rather than an allow-list.

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
from ..store.runstore import DEFAULT_DIRNAME
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

# Directories no agent may write into, whatever its scope says. This is the one
# part of the fence that does not come from the model, and it is a floor rather
# than a default: an agent whose task declared no scope is held to it too.
#
# Before it existed, ``Scope.paths`` defaulting to ``[]`` meant *unrestricted* --
# ``if scope.paths and not matches_any(...)`` -- so a task the synthesis model
# gave no scope produced an execution agent that could write anywhere in the
# workspace, including both of these.
#
# A file under a version control directory is executed by the tool itself:
# ``.git/hooks/pre-commit`` runs at the user's next commit, and ``.git/config``
# names a pager, an alias or an fsmonitor that is a shell command. A write there
# is code execution reached without the shell, so ``allow_command_execution``
# never sees it, and it outlives the run. The harness's own store holds the event
# log every claim in the run is judged against; an agent that can rewrite it can
# report whatever it likes about itself.
#
# Deliberately not ``SKIP_DIRS``: that list is about what is expensive or
# pointless to *read*, and half of it -- ``dist``, ``build``, ``node_modules`` --
# is written by ordinary work.
VCS_DIRS = frozenset({".git", ".hg", ".svn"})
STORE_DIRS = frozenset({DEFAULT_DIRNAME})

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

# Interpreters on the check-runner list that will also run a program handed to
# them in the command line itself, and the flags by which they do it. They earn
# their place on that list by running the project's tests -- ``python -m pytest``
# -- but ``python -c "open('../../x','w').write('')"`` is the same binary
# carrying source the fence has never seen, naming its paths at runtime where no
# argument check can reach them. A scoped agent is refused these flags; ``-m``
# and a script path stay open. Each entry is (short source flags, long source
# flags, short flags whose value is the next token, short flags after which the
# arguments stop being the interpreter's own).
_Interpreter = tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]
_INLINE_SOURCE: dict[str, _Interpreter] = {
    "python": (frozenset("c"), frozenset(), frozenset("WX"), frozenset("m")),
    "python3": (frozenset("c"), frozenset(), frozenset("WX"), frozenset("m")),
    "py": (frozenset("c"), frozenset(), frozenset("WX"), frozenset("m")),
    "node": (frozenset("ep"), frozenset({"--eval", "--print"}), frozenset("r"),
             frozenset()),
}

# git subcommands that change tracked files in the working tree or move HEAD.
# This is the one refusal here that is not about a path, because a path scope
# cannot express it: the agents in a run share a single tree, separated only by
# their scopes, and none of these is separated by a scope. ``git stash`` stashes
# every file at once, including the ones another agent is part-way through
# writing, and the ``git stash pop`` that follows puts them back into a tree that
# has moved underneath them. A real run had an agent stash and pop the whole tree
# to get itself a clean lint baseline while eight others were working in it;
# nothing was lost, and nothing but timing prevented it. ``commit`` is here for
# the same reason rather than for its own danger: a commit of this tree records
# several agents' unfinished work as one change.
GIT_TREE_SUBCOMMANDS = frozenset({
    "stash", "checkout", "switch", "restore", "clean", "reset", "rebase",
    "merge", "revert", "cherry-pick", "apply", "am", "pull", "commit",
})

# git's own options, which come before the subcommand. These take a value, so
# the token after them is not the subcommand: ``git -C /tmp stash``.
_GIT_VALUE_OPTIONS = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
    "--config-env", "--super-prefix",
})

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


def _inline_source_flag(tokens: list[str]) -> str | None:
    """The flag by which this command carries its own source, or ``None``.

    Only the interpreter's own leading options are read. After ``-m module`` or
    a script path the arguments belong to the program being run, where ``-c`` is
    pytest's config file rather than Python's source, and refusing it there would
    fence a legitimate check.
    """
    entry = _INLINE_SOURCE.get(executable_name(tokens[0]))
    if entry is None:
        return None
    source, long_source, takes_value, terminal = entry

    skip = False
    for token in tokens[1:]:
        if skip:  # the value of the flag before it, not a flag itself.
            skip = False
            continue
        if token == "-":
            return "-"  # the program is read from standard input.
        if token == "--" or not token.startswith("-"):
            return None  # the interpreter's own options have ended.
        if token.startswith("--"):
            name = token.partition("=")[0]
            if name in long_source:
                return name
            continue
        for index, char in enumerate(token[1:]):
            if char in source:
                return f"-{char}"
            if char in terminal:
                return None
            if char in takes_value:
                # ``-W ignore`` and ``-Wignore`` are the same flag.
                skip = index == len(token) - 2
                break
    return None


def _git_subcommand(rest: list[str]) -> str:
    """The subcommand of a ``git`` invocation, given the tokens after ``git``."""
    skip = False
    for token in rest:
        if skip:
            skip = False
            continue
        if token.startswith("-"):
            if token in _GIT_VALUE_OPTIONS:
                skip = True
            continue
        return token.lower()
    return ""


def tree_wide_git(command: str) -> str | None:
    """Why this command must not touch the shared working tree, or ``None``.

    Applied to every command an agent runs, whether or not it has a path scope,
    because the hazard is the shared tree rather than the agent's fence: an
    unscoped agent is no more entitled to stash a peer's half-written file than a
    scoped one. For a scoped agent this is a second lock on a door the executable
    allow-list in :meth:`Toolbox._scope_refusal` already bolts -- ``git`` is not a
    check runner -- and it stays here so that removing one does not silently open
    the other.

    What it reads is the command's tokens, so it sees ``git stash``,
    ``git -C . stash`` and ``make && git reset --hard`` alike. What it cannot see
    is a name that is not spelled: ``sh -c 'git stash'`` passes git inside a
    quoted argument, and an alias can call anything at all. For a scoped agent
    those are closed elsewhere -- neither ``sh`` nor ``git`` is a runner it may
    invoke. For an unscoped agent, which by design reaches the whole machine,
    this is a guardrail against the plausible mistake and not a fence.
    """
    tokens = shell_split(command)
    for index, token in enumerate(tokens):
        if executable_name(token) != "git":
            continue
        subcommand = _git_subcommand(tokens[index + 1:])
        if subcommand in GIT_TREE_SUBCOMMANDS:
            return (
                f"no agent may run `git {subcommand}` here: it acts on the whole "
                "working tree, which you share with the other agents in this run, "
                "and your path scope does not constrain it -- it can destroy or "
                "capture work another agent is part-way through writing. If you "
                "need a clean tree to measure something, you cannot have one: "
                "report what you observed against the run's baseline commit instead"
            )
    return None


class Toolbox:
    """The tools an autonomous agent may use, sandboxed to one workspace."""

    def __init__(
        self, workspace: Path, policy: Policy, store_root: Path | None = None
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.policy = policy
        self._store_prefix = self._relative_store(store_root)

    def _relative_store(self, store_root: Path | None) -> str | None:
        """Where the harness's store sits inside the workspace, if it does.

        A store outside the workspace needs no fence: :meth:`_resolve` already
        refuses every path that leaves it. One inside it is reachable, and this
        is what names it. ``.supervisor`` is fenced by name as well, in
        :data:`STORE_DIRS`, so the default holds even for a toolbox built without
        a store -- as every test that constructs one directly does. This prefix
        is for the store that has been moved, by ``SUPERVISOR_HOME`` pointing
        somewhere inside the tree being worked on.

        A store root that *is* the workspace is a misconfiguration rather than a
        location to fence: taking it literally would refuse every write in the
        run. It is left to the name check like any other tree.
        """
        if store_root is None:
            return None
        try:
            prefix = Path(store_root).resolve().relative_to(self.workspace).as_posix()
        except (ValueError, OSError):
            return None
        return prefix if prefix not in ("", ".") else None

    # -- path safety -------------------------------------------------------

    def _floor_refusal(self, rel: str) -> str | None:
        """Why no agent may write this workspace-relative path, or ``None``.

        Every segment is checked rather than the first, so a submodule's
        ``vendor/lib/.git`` is refused by the same rule as the repository's own,
        and so is a nested store. The refusal names the directory that caused it,
        because an agent told only that a path is forbidden tends to try a
        neighbouring one.
        """
        for part in rel.split("/"):
            if part in VCS_DIRS:
                return (
                    f"{rel} is inside {part!r}, which no agent may write whatever "
                    f"its scope says: {part}/hooks and {part}/config are run by the "
                    "tool itself, so writing there executes code rather than "
                    "changing the project. Change a file in the project instead"
                )
            if part in STORE_DIRS:
                return (
                    f"{rel} is inside the harness's own store ({part!r}), which no "
                    "agent may write: it holds the event log this run is judged "
                    "against. Report what you found instead of writing it there"
                )
        if self._store_prefix is not None and (
            rel == self._store_prefix or rel.startswith(self._store_prefix + "/")
        ):
            return (
                f"{rel} is inside the harness's own store "
                f"({self._store_prefix!r}), which no agent may write: it holds the "
                "event log this run is judged against. Report what you found "
                "instead of writing it there"
            )
        return None

    def _resolve(self, raw: str) -> Path | None:
        """Resolve a path inside the workspace, or ``None`` if it escapes."""
        try:
            candidate = (self.workspace / raw.strip().lstrip("/\\")).resolve()
            candidate.relative_to(self.workspace)
        except (ValueError, OSError):
            return None
        return candidate

    def _walk(self) -> list[Path]:
        """Every readable file genuinely inside the workspace.

        ``list_files`` and ``search`` take no path from the model, so they were
        treated as needing no containment check -- their reach is whatever this
        yields. It yielded more than the workspace. ``is_file()`` follows
        symlinks, so a link committed to a repository was walked as the file it
        points at, and ``search`` read that file's contents and printed matching
        lines. ``read_file`` refuses the identical path through ``_resolve``,
        which is what made the gap easy to miss: the same file was out of reach
        by name and in reach by pattern.

        Two checks, because one link is not the only shape. A symlinked *file* is
        skipped outright -- a link is not the file it points at, and nothing here
        wants to follow one. A file reached through a symlinked *directory* has
        no link in its own path, so it is caught by resolving it and asking
        whether it is still underneath the workspace.
        """
        root = self.workspace.resolve()
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

        # Before the scope, and whether or not there is one: the floor is not
        # about this agent's fence, and an agent that declared no scope is
        # exactly the one with nothing else standing in its way.
        floor = self._floor_refusal(rel)
        if floor is not None:
            return ToolResult("write_file", False, floor)

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

    def _floor_command_refusal(self, command: str) -> str | None:
        """Why this command names a path under the floor, or ``None``.

        Applied to every agent, like :func:`tree_wide_git` and for the same
        reason: what it refuses is not a property of this agent's scope. For a
        scoped agent it is a second lock on a door the executable allow-list
        already bolts -- no program that writes a file is reachable from that
        shell at all -- and it stays here so that removing one does not silently
        open the other.

        For an *unscoped* agent it is the only path check there is, and it is not
        a complete one: the allow-list, the metacharacter refusal and the glob
        refusal all sit behind the early return below, so a command that computes
        a path rather than naming it still reaches the floor. Closing that means
        deciding whether an unscoped agent should hold a shell at all, which is a
        policy question rather than this defect. What it does close is the naive
        form -- an agent that names the path it means.
        """
        for token in self._path_candidates(shell_split(command)):
            rel = scope_relative(token, self.workspace.as_posix())
            if rel is None or rel.startswith("../"):
                continue
            refusal = self._floor_refusal(rel)
            if refusal is not None:
                return refusal
        return None

    def _scope_refusal(self, command: str, scope: Scope) -> str | None:
        """Why this command escapes the agent's fence, or ``None`` if it does not.

        A shell writes as well as reads, so every path a command names is a
        potential write and is held to the same fence ``write_file`` enforces.
        Without this the fence is decorative: an agent confined to ``src/auth/**``
        is refused a write to ``infra/`` and then reaches it with ``sh -c``.

        Only an agent that actually has a fence is checked. Reading paths out of
        a command can never be complete -- a shell computes names this cannot
        see -- so the four ways of naming a path it cannot follow are closed
        rather than inspected: a metacharacter that chains or redirects, a glob
        that expands to paths no token spells, any executable outside the check
        runners in :data:`.dod.VERIFY_EXECUTABLES`, and a program handed to one
        of those runners as source on the command line. The executable rule is
        what puts ``rm``, ``cp``, ``mkdir`` and ``git checkout`` out of reach
        entirely, rather than relying on the fence to catch their arguments.

        It takes ``git status`` and ``git diff`` with them, which is deliberate:
        nothing here consumes a diff -- a turn's ``files_touched`` is the agent's
        own report, and the reviewer role reads files -- and git cannot be
        narrowed to its read-only subcommands by name, because
        ``git -c alias.s='!sh -c ...' s`` runs anything at all. Letting a scoped
        agent see its own change means fencing git's flags too, not adding it
        here. The tree-changing subcommands are refused a second time, for every
        agent rather than only a fenced one, by :func:`tree_wide_git`: what makes
        them dangerous is the tree being shared, which is true whether or not
        this agent has a scope to violate.

        None of it makes this a sandbox: ``npm test`` still runs whatever
        ``package.json`` says. This module's docstring states what the fence
        does and does not claim.
        """
        floor = self._floor_command_refusal(command)
        if floor is not None:
            return floor

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

        inline = _inline_source_flag(tokens)
        if inline is not None:
            return (
                f"a scoped agent may not pass {inline!r} to {executable!r}: a program "
                "supplied as source, rather than as a module or a file, names its "
                "paths while it runs, so none of them can be checked against your "
                "scope. Run a module or a file instead (python -m pytest ...), or use "
                "write_file to change a file"
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
        # After the scope fence, not before it: a scoped agent is refused git by
        # the executable allow-list already, and the more specific refusal is the
        # more useful one. This one exists for the agent that has no fence.
        tree_refusal = tree_wide_git(command)
        if tree_refusal is not None:
            return ToolResult("run_command", False, tree_refusal)
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
                      "does": "run one of the project's check runners (pytest, npm, "
                              "make, ...) in the workspace, naming only paths inside "
                              "your scope, and not carrying its own program inline "
                              "(no `python -c`, no `node -e`). Never git: the working "
                              "tree is shared with the other agents in this run"})
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
