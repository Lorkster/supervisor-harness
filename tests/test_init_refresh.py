"""`supervisor init` as the update procedure, not just the install one.

The harness ships instructions to its host: a skill for Claude Code, a rule for
Cursor, slash commands for both, and a server registration in `.mcp.json`. They
are code in every sense that matters -- the host reads them on every run, and a
release that changes the protocol changes them with it.

They were installed once and never replaced. So an install done before batch 2
went on telling the host to use `brief` and to report `result={...}`, and
`pip install -U` could not fix it, because the package's copy was fine and the
project's copy was the one being read. That cost three context compactions in a
single run before anyone noticed, and the same drift then bit this repository's
own committed copies (`tests/test_integration_docs.py`).

The distinction this file pins down is between three kinds of file:

* **components** -- the skill, the rule, the commands. The package's, refreshed
  whenever they have fallen behind it.
* **registration** -- the `supervisor` entry in an MCP config. Also the
  package's, but merged into a file that is the user's, so every other server
  survives untouched.
* **settings** -- `supervisor.config.json`. The user's, and never rewritten
  without `--force`.

`--keep-integrations` is the escape hatch for someone who has edited a component
on purpose; it makes their copy stop tracking the package, which is a real
decision and is why it takes a flag.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from supervisor_harness.cli import main

SKILL = Path(".claude/skills/supervise/SKILL.md")
CONFIG = Path("supervisor.config.json")

STALE = "Each packet has agent_id, brief and schema. Report with result={...}.\n"
"""What the skill said before batch 2 -- fields that are empty by default."""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("SUPERVISOR_HOME", raising=False)
    monkeypatch.delenv("SUPERVISOR_WORKSPACE", raising=False)
    return tmp_path


def init(project: Path, *args: str) -> int:
    return main(["init", "--host", "claude", "-w", str(project), *args])


def test_a_component_left_behind_by_a_release_is_brought_up_to_date(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The regression this whole file exists for.

    A project initialised before batch 2 keeps driving the pre-batch-2 protocol
    for ever: nothing fails, the run just costs several times what it should,
    and the only symptom is a context window filling for no visible reason.
    Upgrading the package cannot reach it -- the stale copy is in the project.
    """
    assert init(project) == 0
    (project / SKILL).write_text(STALE, encoding="utf-8")
    capsys.readouterr()

    assert init(project) == 0

    assert "brief_path" in (project / SKILL).read_text(encoding="utf-8")
    assert "updated" in capsys.readouterr().out, (
        "a refresh has to be visible: the user is being told their host will "
        "behave differently after the restart it goes on to ask for"
    )


def test_a_component_that_is_already_current_is_left_alone_and_says_so(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Re-running `init` on a current project must be a no-op, not a rewrite.

    If it reported four files every time, the one line that matters -- something
    actually changed and your host needs restarting -- would be invisible among
    them.
    """
    assert init(project) == 0
    before = (project / SKILL).stat().st_mtime_ns
    capsys.readouterr()

    assert init(project) == 0

    out = capsys.readouterr().out
    assert "Nothing to do" in out
    assert "SKILL.md" not in out
    assert (project / SKILL).stat().st_mtime_ns == before


def test_keep_integrations_leaves_a_component_the_user_has_edited(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Editing the skill is a legitimate thing to do; it just has a cost.

    The flag is how someone says they have taken that on. Their copy then stops
    tracking the package, and no release will correct it for them.
    """
    assert init(project) == 0
    (project / SKILL).write_text("my own notes\n", encoding="utf-8")
    capsys.readouterr()

    assert init(project, "--keep-integrations") == 0

    assert (project / SKILL).read_text(encoding="utf-8") == "my own notes\n"


def test_the_users_settings_are_never_rewritten_by_a_refresh(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one file `init` does not own, and the reason the split exists.

    Routing, turn limits and lens counts are decisions about how someone wants
    their runs to go. Refreshing a component says the package knows better about
    its own protocol; there is no equivalent claim to make about their policy.
    """
    assert init(project) == 0
    (project / CONFIG).write_text(
        json.dumps({"policy": {"default_max_turns": 99}}), encoding="utf-8"
    )
    (project / SKILL).write_text(STALE, encoding="utf-8")
    capsys.readouterr()

    assert init(project) == 0

    settings = json.loads((project / CONFIG).read_text(encoding="utf-8"))
    assert settings["policy"]["default_max_turns"] == 99, (
        "the refresh that fixed the skill also discarded the user's policy"
    )

    # `--force` is how someone asks for the example back. It is the only way,
    # and it is not implied by wanting a current skill.
    assert init(project, "--force") == 0
    replaced = json.loads((project / CONFIG).read_text(encoding="utf-8"))
    assert replaced["policy"]["default_max_turns"] != 99


def test_a_stale_server_registration_is_refreshed_and_the_others_survive(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Registration is the package's; the file it lives in is the user's.

    An entry left as it was points the host at a command that may no longer
    exist -- and unlike a stale skill, that fails at startup with the server
    simply absent, so the skill tells the host to call tools that are not there.
    """
    (project / ".mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "other": {"command": "something-else"},
                "supervisor": {"command": "supervisor-mcp-from-an-old-release"},
            }
        }),
        encoding="utf-8",
    )

    assert init(project) == 0
    capsys.readouterr()

    servers = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert servers["supervisor"]["command"] == "supervisor-mcp"
    assert servers["other"] == {"command": "something-else"}


def test_keep_integrations_leaves_a_server_entry_the_user_has_pointed_elsewhere(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A wrapper script or a pinned interpreter is a deliberate edit too."""
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"supervisor": {"command": "/opt/wrapper.sh"}}}),
        encoding="utf-8",
    )

    assert init(project, "--keep-integrations") == 0
    capsys.readouterr()

    servers = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert servers["supervisor"]["command"] == "/opt/wrapper.sh"


def test_refreshing_never_reports_a_file_it_did_not_write(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Guards the guard: `Nothing to do` has to mean nothing was done.

    A `_copy` that reported every file while writing none, or wrote every file
    while reporting none, would pass most of the checks above. This one holds
    the printed list and the disk to each other.
    """
    assert init(project) == 0
    capsys.readouterr()
    (project / SKILL).write_text(STALE, encoding="utf-8")

    before = {path: path.read_bytes() for path in project.rglob("*") if path.is_file()}

    assert init(project) == 0

    reported = {
        line.strip().split(" (")[0].replace("\\", "/")
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("  ")
    }
    changed = {
        path.relative_to(project).as_posix()
        for path, was in before.items()
        if path.read_bytes() != was
    }

    assert changed == reported
