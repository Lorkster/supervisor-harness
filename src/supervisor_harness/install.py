"""What `supervisor init` puts in a project, and how `supervisor uninstall` takes it out.

Both commands read the same table. That is the point of this module: an
uninstall that keeps its own list of what an install wrote is correct on the
day it is written and wrong the first time a release ships a new component.

Three kinds of file, the same split `tests/test_init_refresh.py` pins down for
install:

* **components** -- the skill, the rule, the slash commands. The package's.
  Uninstall removes a component that is byte-identical to the one the package
  ships. One that differs is either an older release's copy or one the user
  edited on purpose, and nothing here can tell those apart -- so it is kept and
  named, and `--force` takes it anyway. Running `supervisor init` first brings
  an old copy current, after which uninstall can remove it without the flag.
* **registration** -- the `supervisor` entry in an MCP config file. The
  package's, inside a file that is the user's. Uninstall removes that one entry
  and leaves every other server exactly as it was. The file itself goes only
  when nothing but an empty server map would be left.
* **settings and history** -- `supervisor.config.json` and the workspace's
  `.supervisor/` store. The user's. Uninstall never touches them; `--purge`
  removes them, and even then never a store another project shares.

The package itself is not something a command inside it can remove reliably --
on Windows the running executable is locked -- so uninstall ends by saying what
to run, rather than attempting it.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import PROJECT_CONFIG
from .store.runstore import DEFAULT_DIRNAME, HOME_ENV

INTEGRATIONS = Path(__file__).parent / "integrations"

#: Every component `init` can install: (host, shipped file, where it goes).
#: Paths are relative to ``INTEGRATIONS`` and to the workspace respectively.
COMPONENTS: tuple[tuple[str, str, str], ...] = (
    ("claude", "claude_code/SKILL.md", ".claude/skills/supervise/SKILL.md"),
    ("claude", "claude_code/supervise.md", ".claude/commands/supervise.md"),
    ("cursor", "cursor/supervisor.mdc", ".cursor/rules/supervisor.mdc"),
    ("cursor", "cursor/supervise.md", ".cursor/commands/supervise.md"),
)

#: The MCP config files `init` registers the server in, and the host for each.
#: ``""`` means every host: Claude Code's file is written on every install.
MCP_FILES: tuple[tuple[str, str], ...] = (
    ("", ".mcp.json"),
    ("cursor", ".cursor/mcp.json"),
)

#: Directories `init` may have created, deepest first. Uninstall removes each
#: only if it is empty afterwards: an empty directory holds nothing to lose,
#: and one holding anything at all is left exactly where it is.
CREATED_DIRS: tuple[str, ...] = (
    ".claude/skills/supervise",
    ".claude/skills",
    ".claude/commands",
    ".claude",
    ".cursor/rules",
    ".cursor/commands",
    ".cursor",
)

SERVER_NAME = "supervisor"


def shipped_server() -> dict[str, Any]:
    """The `supervisor` MCP entry this release registers."""
    template = json.loads((INTEGRATIONS / "mcp.json").read_text(encoding="utf-8"))
    entry: dict[str, Any] = template["mcpServers"][SERVER_NAME]
    return entry


@dataclass
class UninstallReport:
    """What uninstall did, or with ``dry_run`` what it would do."""

    workspace: str
    dry_run: bool = False
    removed: list[str] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)       # (path, why)
    failed: list[tuple[str, str]] = field(default_factory=list)     # (path, error)
    notes: list[str] = field(default_factory=list)
    # What has gone, or on a dry run would have: how a directory is known to
    # end up empty when nothing has actually been removed from it.
    gone: set[Path] = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "dry_run": self.dry_run,
            "removed": self.removed,
            "kept": [{"path": p, "reason": r} for p, r in self.kept],
            "failed": [{"path": p, "error": e} for p, e in self.failed],
            "notes": self.notes,
        }


def uninstall(
    workspace: Path, *, force: bool = False, purge: bool = False, dry_run: bool = False,
) -> UninstallReport:
    """Remove what `init` installed in ``workspace``; see the module docstring."""
    report = UninstallReport(workspace=str(workspace), dry_run=dry_run)

    for _host, shipped, rel in COMPONENTS:
        _remove_component(workspace, INTEGRATIONS / shipped, rel, force, report)

    wanted = shipped_server()
    for _host, rel in MCP_FILES:
        _unregister(workspace, rel, wanted, force, report)

    if purge:
        _purge(workspace, report)

    # Last, and deepest first, so a directory emptied by any step above -- or on
    # a dry run, one that would be -- is seen as empty.
    for rel in CREATED_DIRS:
        path = workspace / rel
        if path.is_dir() and all(child in report.gone for child in path.iterdir()):
            _act(report, rel + "/", path, lambda p=path: p.rmdir())

    return report


def _act(report: UninstallReport, rel: str, path: Path | None, action: Any) -> None:
    """Run one removal, or only record it on a dry run; never raise.

    ``path`` is what disappears from disk, or ``None`` when a file is only
    rewritten.
    """
    if report.dry_run:
        report.removed.append(rel)
        if path is not None:
            report.gone.add(path)
        return
    try:
        action()
    except OSError as exc:
        report.failed.append((rel, str(exc)))
        return
    report.removed.append(rel)
    if path is not None:
        report.gone.add(path)


def _remove_component(
    workspace: Path, src: Path, rel: str, force: bool, report: UninstallReport,
) -> None:
    dst = workspace / rel
    if not dst.is_file():
        return
    if dst.read_bytes() != src.read_bytes() and not force:
        report.kept.append((
            rel,
            "differs from the copy this release ships -- edited, or left by an older "
            "release. `supervisor init` then uninstall removes an old copy; "
            "--force removes it either way",
        ))
        return
    _act(report, rel, dst, dst.unlink)


def _unregister(
    workspace: Path, rel: str, wanted: dict[str, Any], force: bool, report: UninstallReport,
) -> None:
    """Take the `supervisor` entry out of one MCP config file, keeping the rest."""
    path = workspace / rel
    if not path.is_file():
        return
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        report.kept.append((rel, "does not parse as JSON; remove the supervisor entry by hand"))
        return
    servers = existing.get("mcpServers") if isinstance(existing, dict) else None
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return
    if servers[SERVER_NAME] != wanted and not force:
        report.kept.append((
            f"{rel} (the {SERVER_NAME} entry)",
            "differs from the entry this release registers; --force removes it",
        ))
        return

    del servers[SERVER_NAME]
    if not servers and set(existing) == {"mcpServers"}:
        # Nothing of the user's would be left: the file was only ever this entry.
        _act(report, rel, path, path.unlink)
        return

    def rewrite() -> None:
        path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

    _act(report, f"{rel} (the {SERVER_NAME} entry; other servers kept)", None, rewrite)


def _purge(workspace: Path, report: UninstallReport) -> None:
    """Remove the user's settings and this workspace's own store -- never a shared one."""
    config = workspace / PROJECT_CONFIG
    if config.is_file():
        _act(report, PROJECT_CONFIG, config, config.unlink)

    store = workspace / DEFAULT_DIRNAME
    shared = [Path.home() / DEFAULT_DIRNAME]
    env = os.environ.get(HOME_ENV)
    if env:
        shared.append(Path(env).expanduser())
        report.notes.append(
            f"{HOME_ENV} is set, so this project's runs and lessons live in the shared "
            f"store at {Path(env).expanduser()}, which other projects use too. It was "
            "left alone; `supervisor delete` removes individual runs from it."
        )

    if not store.is_dir():
        return
    if any(_same(store, other) for other in shared):
        report.kept.append((
            DEFAULT_DIRNAME + "/",
            "is a shared harness home (your user-level store or SUPERVISOR_HOME), "
            "not this project's own; remove it by hand if you mean to",
        ))
        return
    _act(report, DEFAULT_DIRNAME + "/", store, lambda: shutil.rmtree(store))


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False
