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
* **every** agent may invoke only the check runners in
  :data:`.dod.VERIFY_EXECUTABLES`, whatever scope it declared, with
  metacharacters and globs refused outright because they name paths no token
  spells;
* those check runners may not be handed their program in the command line
  itself: ``python -c`` and ``node -e`` are refused, because source given as a
  string names its paths only once it is already running;
* every argument that could name a path must fall inside the agent's scope. This
  is the one rule that an empty scope relaxes, and it relaxes it to *the
  workspace* -- the same meaning ``write_file`` gives it -- rather than to the
  machine. Until this was made universal, an agent whose task arrived with no
  scope skipped the three rules above as well and reached any program on the
  machine; the least specified agent in a run held the widest shell in it;
* the command runs without the user's credentials in its environment, and its
  timeout stops the whole process tree (:func:`.dod.run_bounded`), so a check
  runner is handed neither an API key nor an unbounded run;
* two refusals apply for reasons that are not about scope at all: no command may
  change the shared working tree's git state (:func:`tree_wide_git`), and none
  may name a path under the floor (:data:`VCS_DIRS`, :data:`STORE_DIRS`). Both
  are now second locks rather than sole ones, and are kept so that loosening the
  allow-list later cannot silently reopen either.

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
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Policy
from ..models import AgentSpec, Scope
from ..store.runstore import DEFAULT_DIRNAME
from .dod import (
    VERIFY_EXECUTABLES,
    executable_name,
    inline_source_flag,
    run_bounded,
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

# Glob characters. No agent may use them at all: the shell expands
# them before the command runs, so ``rm -rf *`` names every path in the
# workspace while containing no token that is one of those paths. They are not
# in dod's ``_METACHARACTERS`` because a criterion command is run without a
# shell, where a glob is an ordinary literal argument.
_GLOB_CHARACTERS = "*?["

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
#: A read also stops at this many characters, and says where to continue. Lines
#: alone did not bound it: 400 lines of dense code is 16,000 characters, more
#: than a round's results could carry, and the excess was lost without a word.
MAX_READ_CHARS = 10_000
#: What a read returns to an agent whose conversation keeps it (see
#: ``Toolbox.call``): enough for the 483-line translation file whose first page
#: was all one implementer saw, and wrote back.
WHOLE_FILE_LINES = 3_000
WHOLE_FILE_CHARS = 80_000
MAX_MATCHES = 60
MAX_LIST = 200
#: Files larger than this are not read whole: a read refuses them, a search
#: skips them. A model chooses what to open, and one multi-gigabyte log in a
#: repository is enough to exhaust the harness's memory.
MAX_FILE_BYTES = 5_000_000
#: Bounds on ``search``, whose pattern a model writes. Python's ``re`` has no
#: timeout, so a pattern with nested repetition -- ``(a+)+$`` -- can run for
#: hours on one line: the pattern is refused, lines are searched only so far,
#: and the whole search stops after a time budget, saying so.
#: A whole-file write over an existing file of at least this many lines is
#: refused when it would keep fewer than this share of them (see
#: ``Toolbox._truncation_refusal``). A short file is rewritten freely.
TRUNCATION_MIN_LINES = 40
TRUNCATION_KEEP = 0.5
MAX_PATTERN_CHARS = 300
MAX_SEARCH_LINE_CHARS = 2_000
SEARCH_SECONDS = 15.0
# A group containing a quantifier, itself quantified: the shape of
# catastrophic backtracking. A heuristic, so it errs towards refusing.
_NESTED_REPETITION = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*[+*{]")


@dataclass
class ToolResult:
    tool: str
    ok: bool
    output: str
    # The workspace-relative file a successful ``read_file`` opened. The
    # supervisor counts these against the agent's scope before it accepts "done":
    # measured here, not taken from the agent's own list of what it examined.
    path: str = ""

    def render(self) -> str:
        status = "" if self.ok else " (failed)"
        return f"### {self.tool}{status}\n{self.output}"


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
    quoted argument, and an alias can call anything at all. Those are closed
    elsewhere, for every agent, scoped or not: neither ``sh`` nor ``git`` is a
    runner the allow-list in :meth:`Toolbox._scope_refusal` lets any agent
    invoke. (This said unscoped agents reached the whole machine; that stopped
    being true when the allow-list became universal, and a reviewer reading the
    old sentence reported a bypass that no longer exists.)
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
            if path.is_symlink():
                continue
            if not path.is_file():
                continue
            # The parts *inside* the workspace: a workspace that itself sits under
            # a directory called ``build`` or ``target`` used to show no files.
            try:
                inside = path.relative_to(self.workspace).parts
            except ValueError:
                continue    # not under the workspace at all
            if any(part in SKIP_DIRS for part in inside):
                continue
            if path.suffix.lower() in BINARY_SUFFIXES:
                continue
            try:
                if root not in path.resolve().parents:
                    continue
            except OSError:
                continue
            out.append(path)
        return out

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.workspace).as_posix()

    def scope_files(self, scope: Scope) -> list[str]:
        """The files an agent with ``scope`` could read, workspace-relative and sorted.

        What its coverage is counted against: everything ``list_files`` would
        show it, narrowed to its scope paths (an empty scope is the whole
        workspace) and without its forbidden paths.
        """
        out = []
        for path in self._walk():
            rel = self._rel(path)
            if matches_any(rel, scope.forbidden_paths):
                continue
            if scope.paths and not matches_any(rel, scope.paths):
                continue
            out.append(rel)
        return sorted(out)

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

    def read_file(self, path: str, start: int = 1, limit: int = MAX_READ_LINES, *,
                  max_lines: int = MAX_READ_LINES, max_chars: int = MAX_READ_CHARS) -> ToolResult:
        target = self._resolve(path)
        if target is None:
            return ToolResult("read_file", False, f"{path!r} is outside the workspace")
        if not target.is_file():
            return ToolResult("read_file", False, f"{path!r} does not exist")
        try:
            if target.stat().st_size > MAX_FILE_BYTES:
                return ToolResult("read_file", False,
                                  f"{path!r} is over {MAX_FILE_BYTES // 1_000_000} MB; search "
                                  "it for what you need instead")
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return ToolResult("read_file", False, f"could not read {path!r}: {exc}")

        start = max(1, int(start or 1))
        limit = max(1, min(int(limit or max_lines), max_lines))
        numbered: list[str] = []
        size = 0
        for n, line in enumerate(lines[start - 1 : start - 1 + limit], start):
            text = f"{n:>5}  {line}"
            if numbered and size + len(text) + 1 > max_chars:
                break
            numbered.append(text)
            size += len(text) + 1
        shown = len(numbered)
        remaining = len(lines) - (start - 1 + shown)
        suffix = (
            f"\n... ({remaining} more lines; continue with read_file start={start + shown})"
            if remaining > 0
            else ""
        )
        body = "\n".join(numbered)
        rel = self._rel(target)
        return ToolResult("read_file", True, f"{rel}\n{body}{suffix}", path=rel)

    def search(self, pattern: str, glob: str = "**/*") -> ToolResult:
        if len(pattern) > MAX_PATTERN_CHARS:
            return ToolResult("search", False, f"pattern is over {MAX_PATTERN_CHARS} characters")
        if _NESTED_REPETITION.search(pattern):
            return ToolResult("search", False,
                              "the pattern repeats a group that itself repeats, which can "
                              "take exponential time; search for something simpler")
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return ToolResult("search", False, f"invalid pattern: {exc}")

        hits: list[str] = []
        deadline = time.monotonic() + SEARCH_SECONDS
        stopped = False
        for path in self._walk():
            rel = self._rel(path)
            if glob not in ("", "**/*") and not fnmatch.fnmatch(rel, glob):
                continue
            if time.monotonic() > deadline:
                stopped = True
                break
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                for number, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                ):
                    if regex.search(line[:MAX_SEARCH_LINE_CHARS]):
                        hits.append(f"{rel}:{number}: {line.strip()[:180]}")
                        if len(hits) >= MAX_MATCHES:
                            break
            except OSError:
                continue
            if len(hits) >= MAX_MATCHES:
                break
        body = "\n".join(hits) or "(no matches)"
        if stopped:
            body += (f"\n(search stopped after {SEARCH_SECONDS:.0f} s; narrow it with a glob "
                     "to see the rest)")
        return ToolResult("search", True, body)

    def _write_refusal(self, rel: str, scope: Scope | None) -> str | None:
        """Why this agent may not modify ``rel``; None if it may."""
        # Before the scope, and whether or not there is one: the floor is not
        # about this agent's fence, and an agent that declared no scope is
        # exactly the one with nothing else standing in its way.
        floor = self._floor_refusal(rel)
        if floor is not None:
            return floor
        if scope is not None:
            if matches_any(rel, scope.forbidden_paths):
                return f"{rel} is a forbidden path for this agent"
            if scope.paths and not matches_any(rel, scope.paths):
                return f"{rel} is outside this agent's scope ({', '.join(scope.paths)})"
        return None

    def write_file(self, path: str, content: str, scope: Scope | None = None) -> ToolResult:
        target = self._resolve(path)
        if target is None:
            return ToolResult("write_file", False, f"{path!r} is outside the workspace")

        rel = target.relative_to(self.workspace).as_posix()
        refusal = self._write_refusal(rel, scope) or self._truncation_refusal(target, rel, content)
        if refusal is not None:
            return ToolResult("write_file", False, refusal)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult("write_file", False, f"could not write {rel}: {exc}")
        return ToolResult("write_file", True, f"wrote {rel} ({len(content)} bytes)")

    @staticmethod
    def _truncation_refusal(target: Path, rel: str, content: str) -> str | None:
        """A whole-file write that would throw most of an existing file away.

        Measured on a local model: an implementer adding one i18n key read the
        first page of a 483-line `en.json` -- a read stops at a character
        budget and says where to continue -- and wrote back what it had seen
        plus its key. 142 lines were left, thirty tests broke, and the same
        happened to the test file beside it. A write replaces the whole file,
        so anything not in it is deleted; a change to part of a file is
        `edit_file`'s, and this says so instead of doing it.
        """
        try:
            if not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
                return None
            before = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            return None
        after = len(content.splitlines())
        if before < TRUNCATION_MIN_LINES or after >= before * TRUNCATION_KEEP:
            return None
        return (f"refused: this would replace all {before} lines of {rel} with {after}. "
                "write_file replaces the whole file, so every line you did not include "
                "is deleted. To change part of a file, use "
                "edit_file(path, old, new); to see all of it, read on from where "
                "read_file said to continue.")

    def edit_file(self, path: str, old: str, new: str, scope: Scope | None = None) -> ToolResult:
        """Replace the one occurrence of ``old`` in an existing file with ``new``.

        Exactly one: none says the text is not there as written, and more than
        one says how many, so a change never lands somewhere the agent did not
        mean. The file's own line endings are kept -- `read_file` shows lines,
        not the `\\r` at the end of them, so ``old`` is matched either way.
        """
        target = self._resolve(path)
        if target is None:
            return ToolResult("edit_file", False, f"{path!r} is outside the workspace")
        rel = target.relative_to(self.workspace).as_posix()
        refusal = self._write_refusal(rel, scope)
        if refusal is not None:
            return ToolResult("edit_file", False, refusal)
        if not target.is_file():
            return ToolResult("edit_file", False,
                              f"{rel} does not exist; create it with write_file")
        if not old:
            return ToolResult("edit_file", False, "`old` is empty: name the text to replace")
        try:
            with target.open(encoding="utf-8", newline="") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult("edit_file", False, f"could not read {rel}: {exc}")
        if "\r\n" in text:
            old, new = (s.replace("\r\n", "\n").replace("\n", "\r\n") for s in (old, new))
        count = text.count(old)
        if count != 1:
            return ToolResult("edit_file", False, (
                f"`old` does not occur in {rel} as written; read the file and copy the "
                "text exactly" if count == 0 else
                f"`old` occurs {count} times in {rel}; include enough of the "
                "surrounding lines to pick out one"))
        line = text[:text.index(old)].count("\n") + 1
        try:
            with target.open("w", encoding="utf-8", newline="") as handle:
                handle.write(text.replace(old, new, 1))
        except OSError as exc:
            return ToolResult("edit_file", False, f"could not write {rel}: {exc}")
        return ToolResult("edit_file", True, f"edited {rel} at line {line}")

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

        It is no longer the *only* check an unscoped agent gets: the early
        return it used to sit in front of is gone, so the allow-list, the
        metacharacter refusal and the glob refusal now apply to every agent.
        This stays anyway, for the reason above -- the floor is not about a
        scope, and two locks on it means removing one does not open the other.
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

        **Every agent is checked, whether or not it declared a scope.** It used
        to be only those that had one, on the reasoning that there was nothing
        to check a path against -- but three of the four rules below are not
        about a path at all, and skipping them handed the least specified agent
        in a run the widest shell in it. An empty scope now means "any path in
        the workspace", which is what it already meant to ``write_file``, and
        not "any program on the machine".

        Reading paths out of a command can never be complete -- a shell computes
        names this cannot see -- so the four ways of naming a path it cannot
        follow are closed rather than inspected: a metacharacter that chains or
        redirects, a glob that expands to paths no token spells, any executable
        outside the check runners in :data:`.dod.VERIFY_EXECUTABLES`, and a
        program handed to one of those runners as source on the command line.
        The executable rule is what puts ``rm``, ``cp``, ``mkdir`` and
        ``git checkout`` out of reach entirely, rather than relying on the fence
        to catch their arguments.

        It takes ``git status`` and ``git diff`` with them, which is deliberate:
        nothing here consumes a diff -- a turn's ``files_touched`` is the agent's
        own report, and the reviewer role reads files -- and git cannot be
        narrowed to its read-only subcommands by name, because
        ``git -c alias.s='!sh -c ...' s`` runs anything at all. Letting an agent
        see its own change means fencing git's flags too, not adding it here.
        That now costs an unscoped agent ``git status`` as well, which it could
        previously run: the allow-list is the price of the allow-list being
        universal, and reading the tree through git was never something the
        harness consumed. The tree-changing subcommands are still refused a
        second time by :func:`tree_wide_git`, kept so that loosening the
        allow-list later cannot silently reopen the shared tree.

        None of it makes this a sandbox: ``npm test`` still runs whatever
        ``package.json`` says. This module's docstring states what the fence
        does and does not claim.
        """
        floor = self._floor_command_refusal(command)
        if floor is not None:
            return floor

        metacharacter = unquoted_metacharacter(command)
        if metacharacter is not None:
            return (
                f"a command may not use the shell metacharacter "
                f"{metacharacter!r}: the paths a chained or redirected command touches "
                "cannot be checked at all. Run one plain command at a time"
            )

        glob_character = unquoted_metacharacter(command, _GLOB_CHARACTERS)
        if glob_character is not None:
            return (
                f"a command may not use the glob character "
                f"{glob_character!r}: the shell expands it to paths the command never "
                "names, so they cannot be checked. Name each path"
            )

        tokens = shell_split(command)
        if not tokens:
            return "the command is empty once tokenised"

        executable = executable_name(tokens[0])
        if executable not in VERIFY_EXECUTABLES:
            return (
                f"an agent may not run {executable!r}: only the project's own "
                f"check runners are reachable from the shell "
                f"({', '.join(sorted(VERIFY_EXECUTABLES))}). Use write_file to change "
                "a file in your scope, or report the command for the host to run"
            )

        inline = inline_source_flag(tokens)
        if inline is not None:
            return (
                f"an agent may not pass {inline!r} to {executable!r}: a program "
                "supplied as source, rather than as a module or a file, names its "
                "paths while it runs, so none of them can be checked. Run a module or a "
                "file instead (python -m pytest ...), or use "
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
        # An absent scope is an empty one, not an exemption. This used to skip
        # the fence entirely, which made `run_command(cmd)` -- the direct call,
        # not the dispatched one -- the widest hole in the toolbox.
        refusal = self._scope_refusal(command, scope if scope is not None else Scope())
        if refusal is not None:
            return ToolResult("run_command", False, refusal)
        # After the fence, not before it: the executable allow-list refuses git
        # to every agent now, and its message is the more useful one. This stays
        # as the second lock the module docstring describes, so that loosening
        # the allow-list later cannot silently reopen the shared tree.
        tree_refusal = tree_wide_git(command)
        if tree_refusal is not None:
            return ToolResult("run_command", False, tree_refusal)
        # Tokenised and run without a shell, exactly as `dod.verify_command`
        # already runs a criterion's command. Two reasons, and the second is a
        # defect rather than tidiness:
        #
        # The shell bought nothing. Every unquoted metacharacter and glob is
        # refused above, for every agent, so by this point the command is a
        # program and its arguments -- which is what `shell_split` calls "the
        # whole of its meaning".
        #
        # And `shell=True` made `command_timeout_seconds` not a bound. The
        # timeout kills the *shell*; on Windows `cmd /c` is a different process
        # from the program it launched, which keeps running and holds the pipes,
        # so `subprocess.run` blocks until the program finishes and only then
        # reports the timeout. Measured: a 20-second sleep under a 1-second
        # timeout returned TimeoutExpired after 20.1 seconds, and after 1.0
        # without the shell. An agent could hold a run open for as long as it
        # liked while the harness reported that it had stopped it.
        argv = shell_split(command)
        executable = shutil.which(argv[0]) if argv else None
        if executable is None:
            return ToolResult(
                "run_command", False,
                f"could not run {command!r}: {executable_name(argv[0]) if argv else command!r} "
                "is not on PATH",
            )
        argv[0] = executable

        try:
            completed = run_bounded(argv, self.workspace, self.policy.command_timeout_seconds)
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

    def call(self, name: str, args: dict[str, Any], agent: AgentSpec, *,
             whole_files: bool = False) -> ToolResult:
        """Run one requested tool, enforcing what this agent is allowed to do.

        ``whole_files`` is for an agent whose conversation keeps what it read:
        a read then returns a file whole, up to a far larger bound, because a
        model that saw only the first page of a file writes back only that.
        """
        name = (name or "").strip()
        writable = agent.kind.value in WRITE_KINDS
        may_run = agent.kind.value in COMMAND_KINDS

        if name == "list_files":
            return self.list_files(str(args.get("pattern", "**/*")))
        if name == "read_file":
            lines, chars = ((WHOLE_FILE_LINES, WHOLE_FILE_CHARS) if whole_files
                            else (MAX_READ_LINES, MAX_READ_CHARS))
            return self.read_file(
                str(args.get("path", "")),
                int(args.get("start", 1) or 1),
                int(args.get("limit", lines) or lines),
                max_lines=lines, max_chars=chars,
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
        if name == "edit_file":
            if not writable:
                return ToolResult(
                    "edit_file", False,
                    f"a {agent.kind.value} agent may not modify files; report what "
                    "should change instead",
                )
            return self.edit_file(str(args.get("path", "")), str(args.get("old", "")),
                                  str(args.get("new", "")), agent.scope)
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
        tools.append({"name": "edit_file", "args": "path, old, new",
                      "does": "change part of an existing file: replace the one place "
                              "`old` appears, copied exactly from read_file, with `new`. "
                              "Use this for any change to a file that exists"})
        tools.append({"name": "write_file", "args": "path, content",
                      "does": "write a whole file, within your scope only: for a new "
                              "file, or to replace one entirely -- every line you leave "
                              "out is deleted"})
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


def render_results(results: list[ToolResult], limit: int = 0) -> str:
    """One round's results, within ``limit`` characters if one is given.

    Never cut silently. The whole round used to be sliced to a fixed length, so
    an agent that asked for four files saw part of the first and nothing else --
    not the others, not the "more lines" notice, not the instruction to
    continue -- and could not know what it had not seen. Now results are kept
    whole in the order asked for; one that does not fit is clipped (if it is the
    first) or replaced by a line saying it was left out and to ask again.
    """
    head = "## Tool results\n\n"
    tail = "\n\nContinue. Call more tools if you need them, or give your answer now."
    blocks: list[str] = []
    used = len(head) + len(tail)
    for result in results:
        block = result.render()
        room = limit - used - 2 if limit else len(block)
        if len(block) <= room:
            blocks.append(block)
            used += len(block) + 2
        elif not blocks and room > 200:
            note = (f"\n[cut here: a round's results are limited to {limit} characters; "
                    "ask for a smaller part]")
            blocks.append(block[: room - len(note)] + note)
            used = limit
        else:
            blocks.append(f"### {result.tool}\n(left out: this round's results are limited "
                          f"to {limit} characters. Call it again in your next round.)")
            used += len(blocks[-1]) + 2
    return head + "\n\n".join(blocks) + tail
