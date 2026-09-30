"""`supervisor uninstall`: the inverse of `init`, and nothing more.

The harness had no way out. `init` writes into every project it is run in -- a
skill, a rule, slash commands, a server registration merged into a file that is
the user's -- and `pip uninstall` reaches none of it, so removing the package
left each project's host still trying to start an MCP server that no longer
existed. That matters most exactly when the harness becomes a dependency of
something else, and someone wants it gone again.

What these tests pin down is the boundary `install.py` draws: the package's
files go, the user's stay, and a file that might be either is kept and named
rather than guessed at.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from supervisor_harness.cli import main
from supervisor_harness.install import COMPONENTS, CREATED_DIRS

CONFIG = Path("supervisor.config.json")
SKILL = Path(".claude/skills/supervise/SKILL.md")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("SUPERVISOR_HOME", raising=False)
    monkeypatch.delenv("SUPERVISOR_WORKSPACE", raising=False)
    return tmp_path


def cli(project: Path, *args: str) -> int:
    return main([*args, "-w", str(project)])


def as_json(project: Path, capsys: pytest.CaptureFixture[str], *args: str) -> dict[str, object]:
    capsys.readouterr()
    cli(project, *args, "--json")
    parsed: dict[str, object] = json.loads(capsys.readouterr().out)
    return parsed


def files_under(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def test_everything_init_wrote_is_removed_and_only_the_settings_are_left(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The round trip, measured against what `init` *says* it wrote.

    Not against `COMPONENTS`: an uninstall checked against its own table would
    pass however wrong that table was. Asking `init` what it wrote catches a
    component added to install and forgotten by uninstall.
    """
    written = as_json(project, capsys, "init", "--host", "both")["written"]
    assert isinstance(written, list)
    assert len(written) >= 6, written

    assert cli(project, "uninstall") == 0

    assert files_under(project) == {CONFIG.as_posix()}, (
        "only the user's settings survive a plain uninstall; every component, "
        "registration and directory init created is gone"
    )


def test_every_other_mcp_server_survives(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The file is the user's. Only the one entry is the package's."""
    other = {"command": "something-else", "args": ["--flag"]}
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other": other}, "note": "mine"}), encoding="utf-8"
    )
    assert cli(project, "init", "--host", "claude") == 0

    assert cli(project, "uninstall") == 0

    left = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert left == {"mcpServers": {"other": other}, "note": "mine"}


def test_an_mcp_file_that_was_only_the_registration_goes_with_it(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(project, "init", "--host", "cursor") == 0

    assert cli(project, "uninstall") == 0

    assert not (project / ".mcp.json").exists()
    assert not (project / ".cursor" / "mcp.json").exists()


def test_a_component_that_differs_is_kept_and_named_unless_forced(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Edited on purpose, or left by an older release: nothing can tell which."""
    assert cli(project, "init", "--host", "claude") == 0
    (project / SKILL).write_text("my own notes on how we supervise\n", encoding="utf-8")
    capsys.readouterr()

    assert cli(project, "uninstall") == 0
    out = capsys.readouterr().out
    assert (project / SKILL).is_file()
    assert "Kept:" in out
    assert SKILL.as_posix() in out
    assert not (project / ".claude" / "commands" / "supervise.md").exists(), (
        "the untouched component beside it still goes"
    )

    assert cli(project, "uninstall", "--force") == 0
    assert not (project / SKILL).exists()


def test_an_old_copy_is_removable_after_init_brings_it_current(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The route the kept-message recommends has to actually work."""
    assert cli(project, "init", "--host", "claude") == 0
    (project / SKILL).write_text("what an older release shipped\n", encoding="utf-8")

    assert cli(project, "init", "--host", "claude") == 0
    assert cli(project, "uninstall") == 0

    assert not (project / SKILL).exists()


def test_an_edited_registration_is_kept_unless_forced(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    edited = {"command": "supervisor-mcp", "args": [], "env": {"SUPERVISOR_HOME": "/x"}}
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"supervisor": edited}}), encoding="utf-8"
    )

    assert cli(project, "uninstall") == 0
    assert "supervisor" in json.loads((project / ".mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ]

    assert cli(project, "uninstall", "--force") == 0
    assert not (project / ".mcp.json").exists()


def test_an_mcp_file_that_will_not_parse_is_left_exactly_as_it_is(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = '{"mcpServers": {"supervisor": {"command": "supervisor-mcp"},}'
    (project / ".mcp.json").write_text(broken, encoding="utf-8")

    assert cli(project, "uninstall", "--force") == 0

    assert (project / ".mcp.json").read_text(encoding="utf-8") == broken
    assert "does not parse" in capsys.readouterr().out


def test_settings_and_history_stay_without_purge_and_go_with_it(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(project, "init", "--host", "claude") == 0
    store = project / ".supervisor" / "runs" / "run_x"
    store.mkdir(parents=True)
    (store / "events.jsonl").write_text("{}\n", encoding="utf-8")

    assert cli(project, "uninstall") == 0
    assert (project / CONFIG).is_file()
    assert (store / "events.jsonl").is_file()

    assert cli(project, "uninstall", "--purge") == 0
    assert files_under(project) == set()


def test_purge_never_takes_a_store_named_as_the_shared_home(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`SUPERVISOR_HOME` pointed at this project's store makes it every project's."""
    shared = project / ".supervisor"
    (shared / "lessons.jsonl").parent.mkdir(parents=True)
    (shared / "lessons.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("SUPERVISOR_HOME", str(shared))

    assert cli(project, "uninstall", "--purge") == 0

    assert (shared / "lessons.jsonl").is_file()
    out = capsys.readouterr().out
    assert "shared" in out


def test_purge_never_takes_the_user_level_store(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uninstalling from your home directory must not delete `~/.supervisor`.

    It holds the trusted config and, for anyone using a shared home, every
    lesson every project learned.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: project))
    (project / ".supervisor").mkdir()
    (project / ".supervisor" / "config.json").write_text("{}", encoding="utf-8")

    assert cli(project, "uninstall", "--purge") == 0

    assert (project / ".supervisor" / "config.json").is_file()


def test_a_dry_run_names_everything_and_changes_nothing(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(project, "init", "--host", "both") == 0
    before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}

    planned = as_json(project, capsys, "uninstall", "--dry-run", "--purge")

    assert {p: p.read_bytes() for p in project.rglob("*") if p.is_file()} == before
    removed = planned["removed"]
    assert isinstance(removed, list)
    for _host, _shipped, rel in COMPONENTS:
        assert rel in removed
    assert CONFIG.as_posix() in removed


def test_a_dry_run_lists_what_a_real_run_then_removes(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Including the directories, which a dry run never actually empties."""
    assert cli(project, "init", "--host", "both") == 0

    planned = as_json(project, capsys, "uninstall", "--dry-run", "--purge")["removed"]
    done = as_json(project, capsys, "uninstall", "--purge")["removed"]

    assert planned == done
    assert ".claude/" in done


def test_a_directory_holding_anything_of_the_users_is_left(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only a directory left empty goes; `.claude/agents` is not the harness's."""
    agent = project / ".claude" / "agents" / "security.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("a security specialist\n", encoding="utf-8")
    assert cli(project, "init", "--host", "claude") == 0

    assert cli(project, "uninstall") == 0

    assert agent.is_file()
    assert not (project / ".claude" / "skills").exists()
    assert not (project / ".claude" / "commands").exists()


def test_a_removal_that_fails_is_reported_and_fails_the_command(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows a running host can hold a file open. Say so; never skip in silence."""
    assert cli(project, "init", "--host", "claude") == 0

    def locked(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError(13, "in use", str(self))

    monkeypatch.setattr(Path, "unlink", locked)
    capsys.readouterr()

    assert cli(project, "uninstall") == 1

    out = capsys.readouterr().out
    assert "Could not remove" in out
    assert SKILL.as_posix() in out


def test_the_directories_uninstall_may_prune_cover_every_component() -> None:
    """A component placed somewhere new would otherwise leave its directory behind."""
    for _host, _shipped, rel in COMPONENTS:
        assert str(Path(rel).parent.as_posix()) in CREATED_DIRS, rel
