"""The shipped host instructions, checked against the protocol they describe.

`supervisor init` installs a skill for Claude Code and a rule for Cursor. Those
files are the harness's instructions to the only party that can actually do
anything: they are read by the host on every run, and they are more specific
than the MCP server's own `INSTRUCTIONS`, so where the two disagree these win.

They went stale, and it cost three context compactions in one run before anyone
noticed. Batch 2 moved packets to `brief_path` / `result_path` and updated
`mcp_server.py` and `docs/protocol.md`; nobody updated the skill. It went on
telling the host that "each packet has `agent_id`, `brief`, `schema`" -- both
now empty by default -- and to report with `result={...}`, which is precisely
the by-value round trip batch 2 existed to remove.

That is the silent-regression shape `docs/quality-standard.md` says a gate is
for: every test passed, the harness was correct, and the product was broken for
every user of it because the documentation the *machine* reads had drifted from
the code.

So this checks the instructions the way `check_doc_refs.py` checks the prose:
against the thing they describe.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from supervisor_harness.core.responses import WorkPacket

INTEGRATIONS = Path(__file__).resolve().parent.parent / "src" / "supervisor_harness" / (
    "integrations"
)

#: Every file `supervisor init` writes that tells a host how to drive a run.
HOST_INSTRUCTIONS = (
    "claude_code/SKILL.md",
    "claude_code/supervise.md",
    "cursor/supervisor.mdc",
    "cursor/supervise.md",
)

#: Packet fields a host cannot drive a run without knowing about. Named here
#: rather than derived from the dataclass, because most fields are internal and
#: the interesting set is the one a *host* has to act on.
MUST_MENTION = ("brief_path", "result_path")

#: Fields that were the whole interface before batch 2 and are now empty unless
#: the harness is configured for a host that cannot read files. A document may
#: still name them -- both forms are real -- but only alongside the paths.
SUPERSEDED = ("brief", "schema")


def _text(name: str) -> str:
    return (INTEGRATIONS / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", HOST_INSTRUCTIONS)
def test_every_shipped_instruction_names_the_fields_a_host_must_use(name: str) -> None:
    """The regression that cost three compactions, as a check.

    A host told to read `brief` gets an empty string, and a host told to report
    `result={...}` puts every agent's whole answer through its own context.
    Neither fails anything: the run completes, more expensively, and the only
    symptom is a context window filling up for no visible reason.
    """
    text = _text(name)

    missing = [field for field in MUST_MENTION if field not in text]

    assert not missing, (
        f"{name} does not mention {missing}. It is shipped by `supervisor init` and "
        "read by the host on every run; a host following it will drive the "
        "pre-batch-2 protocol against a harness that no longer speaks it."
    )


@pytest.mark.parametrize("name", HOST_INSTRUCTIONS)
def test_no_instruction_presents_the_superseded_fields_on_their_own(name: str) -> None:
    """Naming `brief` is fine. Naming it *without* `brief_path` is the bug."""
    text = _text(name)

    for field in SUPERSEDED:
        if field in text:
            assert f"{field}_path" in text or "inline" in text, (
                f"{name} mentions `{field}` with no mention of `{field}_path` or of the "
                "inline form; a host reading it will use a field that is empty by default"
            )


@pytest.mark.parametrize("name", HOST_INSTRUCTIONS)
def test_every_shipped_instruction_says_how_to_find_a_run_already_in_flight(
    name: str,
) -> None:
    """The store hides itself from the host that needs to find it.

    `RunStore._contain` writes a `.gitignore` of `*` into the store, on purpose:
    it holds the user's prompt, their absolute paths and every agent's output,
    and committing it by accident publishes all three. The side effect is that
    the store is invisible to the file listing and search a host reaches for
    first -- so a session asked to resume looked on disk, found nothing, and
    offered to start a second run over the top of a live one.

    The instructions have to say: ask the harness, do not look.
    """
    text = _text(name)

    assert "supervisor_resume" in text or "resume" in text.lower(), name
    assert "supervisor_runs" in text, (
        f"{name} does not tell the host to ask `supervisor_runs` whether a run exists. "
        "Looking on the filesystem cannot find one: the store gitignores itself."
    )


def test_the_fields_the_instructions_must_name_are_really_on_the_packet() -> None:
    """Guards the guard. A renamed field must fail here, not silently pass.

    If `result_path` were renamed, the checks above would go on passing against
    documents describing a field that no longer exists -- the same drift one
    level up.
    """
    fields = {f.name for f in dataclasses.fields(WorkPacket)}

    assert set(MUST_MENTION) <= fields
    assert set(SUPERSEDED) <= fields


def test_the_claude_skill_forbids_the_orchestrator_writing_the_result_file() -> None:
    """The observed failure, named where the failing party will read it.

    A run's transcript showed one actor writing result files for two different
    agents in a single message. A sub-agent cannot do that -- it is told about
    its own `result_path` and knows nothing of its peers' -- so the answers had
    passed through the orchestrator before reaching disk, and the by-reference
    return bought nothing although the files existed.

    Nothing had ever said not to. The agent is told "the file, not your reply";
    the orchestrator was told only to pass the path along.
    """
    text = _text("claude_code/SKILL.md")

    assert "not write the result file yourself" in text
