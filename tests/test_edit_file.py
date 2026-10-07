"""An implementer changes part of a file with `edit_file`, not by rewriting it.

Measured on a local model (go-live run 8, plantsandclimate P3-18): adding one
i18n key, an implementer read the first page of a 483-line `en.json` -- a read
stops at a character budget and says where to continue -- and wrote back what
it had seen plus its key. 142 lines were left; thirty tests that passed on the
baseline failed, and the test file beside it went from 247 lines to 105. The
toolbox had no way to change part of a file: every change was a whole-file
write from memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from supervisor_harness.config import Policy
from supervisor_harness.core.tools import (
    TRUNCATION_MIN_LINES,
    Toolbox,
    available_tools,
)
from supervisor_harness.models import AgentKind, AgentSpec, Scope

KEYS = "".join(f'  "key.{n}": "value {n}",\n' for n in range(480))


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "src" / "i18n").mkdir(parents=True)
    (tmp_path / "src" / "i18n" / "en.json").write_text("{\n" + KEYS + '  "last": "x"\n}\n',
                                                      encoding="utf-8")
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra" / "waf.tf").write_text("rule = 1\n", encoding="utf-8")
    return tmp_path


def _box(tree: Path) -> Toolbox:
    return Toolbox(tree, Policy())


def _implementer(scope: Scope | None = None) -> AgentSpec:
    return AgentSpec(id="agt_1", role="implementer", kind=AgentKind.EXECUTION,
                     scope=scope or Scope(paths=["src/"]))


def _call(tree: Path, name: str, agent: AgentSpec | None = None, **args: str):  # type: ignore[no-untyped-def]
    return _box(tree).call(name, args, agent or _implementer())


# -- edit_file -------------------------------------------------------------------


def test_an_edit_changes_the_one_place_and_keeps_the_rest(tree: Path) -> None:
    result = _call(tree, "edit_file", path="src/i18n/en.json",
                   old='  "last": "x"\n', new='  "last": "x",\n  "offline": "needs connection"\n')
    text = (tree / "src/i18n/en.json").read_text(encoding="utf-8")

    assert result.ok, result.output
    assert '"offline": "needs connection"' in text
    assert text.count('"key.') == 480, "nothing else was touched"
    assert "line 482" in result.output


def test_text_that_is_not_there_or_is_there_twice_is_refused(tree: Path) -> None:
    missing = _call(tree, "edit_file", path="src/i18n/en.json", old='"nope"', new="x")
    twice = _call(tree, "edit_file", path="src/i18n/en.json", old='"value 1', new="x")
    empty = _call(tree, "edit_file", path="src/i18n/en.json", old="", new="x")

    assert not missing.ok and "does not occur" in missing.output
    assert not twice.ok and "occurs" in twice.output and "times" in twice.output
    assert not empty.ok and "empty" in empty.output
    assert (tree / "src/i18n/en.json").read_text(encoding="utf-8").count('"key.') == 480


def test_an_edit_keeps_the_files_own_line_endings(tree: Path) -> None:
    """read_file shows lines, not their `\\r`: `old` is matched either way."""
    crlf = tree / "src" / "crlf.txt"
    crlf.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    result = _call(tree, "edit_file", path="src/crlf.txt", old="alpha\nbeta", new="alpha\nBETA")

    assert result.ok, result.output
    assert crlf.read_bytes() == b"alpha\r\nBETA\r\ngamma\r\n"


def test_an_edit_is_fenced_like_a_write(tree: Path) -> None:
    outside = _call(tree, "edit_file", path="infra/waf.tf", old="rule = 1", new="rule = 0")
    lens = _call(tree, "edit_file", AgentSpec(kind=AgentKind.ANALYSIS, scope=Scope()),
                 path="src/i18n/en.json", old='"last"', new='"first"')
    absent = _call(tree, "edit_file", path="src/i18n/sv.json", old="a", new="b")
    escape = _call(tree, "edit_file", path="../elsewhere.txt", old="a", new="b")

    assert not outside.ok and "outside this agent's scope" in outside.output
    assert (tree / "infra/waf.tf").read_text(encoding="utf-8") == "rule = 1\n"
    assert not lens.ok and "may not modify files" in lens.output
    assert not absent.ok and "write_file" in absent.output
    assert not escape.ok and "outside the workspace" in escape.output


def test_an_implementer_is_offered_it_and_a_lens_is_not() -> None:
    def names(kind: AgentKind) -> list[str]:
        return [t["name"] for t in available_tools(AgentSpec(kind=kind), Policy())]

    assert "edit_file" in names(AgentKind.EXECUTION)
    assert "edit_file" not in names(AgentKind.ANALYSIS)
    assert names(AgentKind.EXECUTION).index("edit_file") < names(AgentKind.EXECUTION).index(
        "write_file"), "offered first: it is the tool for a change to an existing file"


# -- a whole-file write that would throw most of a file away ---------------------


def test_a_write_that_drops_most_of_an_existing_file_is_refused(tree: Path) -> None:
    """The measured shape: the first page written back, plus one key."""
    first_page = "{\n" + "".join(KEYS.splitlines(keepends=True)[:140]) + '  "offline": "x"\n}\n'
    result = _call(tree, "write_file", path="src/i18n/en.json", content=first_page)

    assert not result.ok
    assert "edit_file" in result.output and "deleted" in result.output
    assert (tree / "src/i18n/en.json").read_text(encoding="utf-8").count('"key.') == 480


def test_a_new_file_a_short_file_and_a_full_rewrite_are_written(tree: Path) -> None:
    short = tree / "src" / "short.ts"
    short.write_text("".join(f"line {n}\n" for n in range(TRUNCATION_MIN_LINES - 1)),
                     encoding="utf-8")
    whole = (tree / "src/i18n/en.json").read_text(encoding="utf-8").replace('"x"', '"y"')

    assert _call(tree, "write_file", path="src/new.ts", content="export {}\n").ok
    assert _call(tree, "write_file", path="src/short.ts", content="one line\n").ok
    assert _call(tree, "write_file", path="src/i18n/en.json", content=whole).ok
