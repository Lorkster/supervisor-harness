"""What the toolbox, the inspection check, the tool fence and the JSON reader do with hostile input.

Findings 6-12 of the 2026-11 review. Each test failed against the code it was written for.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness.config import Policy
from supervisor_harness.core import dod
from supervisor_harness.core import tools as tools_module
from supervisor_harness.core.dod import verify_inspection
from supervisor_harness.core.external import fence
from supervisor_harness.core.tools import Toolbox, risky_repetition, windows_name_refusal
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    CriterionStatus,
    DoDCriterion,
    Scope,
)
from supervisor_harness.providers.base import extract_json


def _box(tree: Path) -> Toolbox:
    return Toolbox(tree, Policy())


def _agent(kind: AgentKind = AgentKind.EXECUTION, scope: Scope | None = None) -> AgentSpec:
    return AgentSpec(id="a", kind=kind, scope=scope or Scope())


def _make_junction(link: Path, target: Path) -> bool:
    """A directory link that works unprivileged on any platform; whether it was made."""
    try:
        if sys.platform == "win32":
            done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                  capture_output=True, check=False)
            return done.returncode == 0
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        return False
    return True


# -- 6. the search fence --------------------------------------------------------------------


@pytest.mark.parametrize("pattern", [
    "(a?){34}a{34}", "(a|aa)+$", "(a|a)*b", "(?:x|xx)+y", "(a+)+$", "((a|b)c)*", "(ab){2,}",
    "(a{1,5}){10}", "(?P<n>a|ab)+",
])
def test_a_pattern_that_can_backtrack_exponentially_is_refused(tmp_path: Path,
                                                              pattern: str) -> None:
    assert risky_repetition(pattern)
    (tmp_path / "a.txt").write_text("a" * 40 + "!", encoding="utf-8")
    result = _box(tmp_path).search(pattern)
    assert not result.ok and "exponential" in result.output


@pytest.mark.parametrize("pattern", [
    r"def \w+\(", r"(foo|bar)", r"(foo|bar)\d", r"^\s*class ", r"[(a|b)+]", r"a\(b\)+",
    r"\(a+\)", r"(abc)?", r"(a|b)?c", r"x{2,3}", r"foo(bar)", r"(?i)todo|fixme", r"\{1,2\}",
])
def test_ordinary_patterns_are_not_refused(pattern: str) -> None:
    assert not risky_repetition(pattern)


def test_a_slow_pattern_the_scan_cannot_see_is_stopped_at_the_time_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Polynomial, not exponential, so no scan of the pattern's text refuses it: on a line
    # of 2000 'a's it runs for hours. The regex runs in a worker that is killed.
    (tmp_path / "a.txt").write_text("a" * 1900 + "\nneedle\n", encoding="utf-8")
    monkeypatch.setattr(tools_module, "SEARCH_SECONDS", 2.0)
    started = time.monotonic()
    result = _box(tmp_path).search("a*a*a*a*a*a*a*a*b")
    assert time.monotonic() - started < 30
    assert result.ok and "search stopped after" in result.output


def test_a_search_finds_what_it_is_asked_for_with_the_worker(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("one\nTWO needle\nthree\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("caf\u00e9 needle\n", encoding="utf-8")
    out = _box(tmp_path).search("needle").output.splitlines()
    assert "src/a.py:2: TWO needle" in out and "b.txt:1: caf\u00e9 needle" in out


# -- 7. external tool arguments ------------------------------------------------------------


def _fenced(arguments: dict[str, Any], tmp_path: Path, schema: dict[str, Any] | None = None,
            network: bool = False) -> tuple[dict[str, Any], str]:
    tree = tmp_path / "tree"
    tree.mkdir(exist_ok=True)
    return fence(arguments, schema or {"properties": {k: {} for k in arguments}}, tree, [],
                 network=network)


@pytest.mark.parametrize("arguments", [
    {"target": "../../../Windows/win.ini"},
    {"filename": "../../x"},
    {"options": {"path": "C:/Windows/win.ini"}},
    {"options": {"nested": {"anything": "/etc/passwd"}}},
    {"items": [{"name": "..\\..\\secret"}]},
    {"url": "file:///etc/passwd"},
    {"url": "FILE:///C:/Windows/win.ini"},
    {"name": "ok\x00../../etc"},
    {"query": "x", "extra": ["fine", "../../../other"]},
])
def test_a_path_in_any_argument_of_any_name_is_held_to_the_tree(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    placed, why = _fenced(arguments, tmp_path)
    assert why and placed == arguments


@pytest.mark.parametrize("arguments", [
    {"query": "find the login handler"},
    {"pattern": r"def \w+\(.*\)"},
    {"name": "login", "limit": 5, "flag": True, "none": None},
    {"options": {"depth": 3, "label": "src/auth is where it lives"}},
    {"regex": "a..b", "text": "dots... and ellipses.."},
    {"address": "0x401000", "ratio": 1.5},
])
def test_arguments_that_are_not_paths_pass_untouched(tmp_path: Path,
                                                     arguments: dict[str, Any]) -> None:
    placed, why = _fenced(arguments, tmp_path)
    assert not why and placed == arguments


def test_a_path_inside_the_tree_is_made_absolute_and_a_nested_one_too(tmp_path: Path) -> None:
    tree = (tmp_path / "tree")
    tree.mkdir()
    (tree / "a.bin").write_bytes(b"x")
    placed, why = _fenced({"path": "a.bin", "options": {"file": "a.bin"}}, tmp_path)
    assert not why
    assert placed["path"] == str((tree / "a.bin").resolve())
    assert placed["options"]["file"] == str((tree / "a.bin").resolve())


def test_a_network_tool_may_take_an_address_but_not_a_local_file(tmp_path: Path) -> None:
    placed, why = _fenced({"url": "https://example.com/a/../b"}, tmp_path, network=True)
    assert not why and placed == {"url": "https://example.com/a/../b"}
    assert _fenced({"url": "file:///etc/passwd"}, tmp_path, network=True)[1]
    assert _fenced({"url": "x\x00"}, tmp_path, network=True)[1]


# -- 8. inspecting a directory --------------------------------------------------------------


def _inspect(workspace: Path, expect: str) -> Any:
    return verify_inspection(DoDCriterion(id="c", statement="s", expect=expect), workspace)


def test_an_inspection_does_not_read_through_a_link_that_leaves_the_workspace(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    (work / "docs").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("OUTSIDE-CANARY\n", encoding="utf-8")
    (work / "docs" / "own.txt").write_text("inside\n", encoding="utf-8")
    if not _make_junction(work / "docs" / "j", outside):
        pytest.skip("cannot make a directory link here")
    found = _inspect(work, "docs: OUTSIDE-CANARY")
    assert found.status is not CriterionStatus.PASS, found.evidence
    assert _inspect(work, "docs: inside").status is CriterionStatus.PASS


def test_an_inspection_prunes_what_it_never_reads_and_stops_at_its_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "docs" / "node_modules" / "deep").mkdir(parents=True)
    (tmp_path / "docs" / "node_modules" / "deep" / "x.txt").write_text("needle", encoding="utf-8")
    (tmp_path / "docs" / "a.txt").write_text("nothing", encoding="utf-8")
    entered: list[str] = []
    real = os.scandir

    def watching(path: Any = ".") -> Any:
        entered.append(str(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", watching)
    assert _inspect(tmp_path, "docs: needle").status is CriterionStatus.FAIL
    assert not any("node_modules" in e for e in entered), entered


def test_an_inspection_refuses_to_read_a_huge_file_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dod, "MAX_INSPECT_BYTES", 100)
    (tmp_path / "big.log").write_text("needle\n" * 100, encoding="utf-8")
    found = _inspect(tmp_path, "big.log: needle")
    assert found.status is CriterionStatus.BLOCKED and "MB" in found.evidence


# -- 9. the walk prunes ---------------------------------------------------------------------


def test_the_walk_never_enters_a_skipped_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    for skipped in ("node_modules", ".git", ".venv", "__pycache__"):
        (tmp_path / skipped / "inner").mkdir(parents=True)
        (tmp_path / skipped / "inner" / "f.js").write_text("x", encoding="utf-8")
    entered: list[str] = []
    real = os.scandir

    def watching(path: Any = ".") -> Any:
        entered.append(Path(str(path)).name)
        return real(path)

    monkeypatch.setattr(os, "scandir", watching)
    box = _box(tmp_path)
    assert [box._rel(p) for p in box._walk()] == ["src/a.py"]
    assert box.scope_files(Scope()) == ["src/a.py"]
    assert not {"node_modules", ".git", ".venv", "__pycache__", "inner"} & set(entered)


def test_the_walk_lists_files_in_path_order_and_skips_links(tmp_path: Path) -> None:
    (tmp_path / "b").mkdir()
    (tmp_path / "a").mkdir()
    for name in ("b/z.txt", "a/y.txt", "a/x.txt", "m.txt"):
        (tmp_path / name).write_text("t", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("t", encoding="utf-8")
    _make_junction(tmp_path / "link", outside)
    box = _box(tmp_path)
    assert [box._rel(p) for p in box._walk()] == ["a/x.txt", "a/y.txt", "b/z.txt", "m.txt"]


# -- 10. a model's numbers ------------------------------------------------------------------


@pytest.mark.parametrize("start,limit", [
    ("1-20", "all"), ([1], {"n": 3}), (None, None), ("", ""), (True, 2.5), ("ten", "5 lines"),
    (1e400, "1e3"),
])
def test_read_file_survives_arguments_that_are_not_numbers(
    tmp_path: Path, start: Any, limit: Any
) -> None:
    (tmp_path / "f.txt").write_text("\n".join(f"line {n}" for n in range(1, 31)), encoding="utf-8")
    result = _box(tmp_path).call("read_file", {"path": "f.txt", "start": start, "limit": limit},
                                 _agent(AgentKind.ANALYSIS))
    assert result.ok and "line 1" in result.output


def test_a_number_that_was_not_one_is_said_and_a_good_one_is_used(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("\n".join(f"line {n}" for n in range(1, 31)), encoding="utf-8")
    box, agent = _box(tmp_path), _agent(AgentKind.ANALYSIS)
    bad = box.call("read_file", {"path": "f.txt", "start": "1-20"}, agent)
    assert "start='1-20' is not a whole number; used 1" in bad.output
    good = box.call("read_file", {"path": "f.txt", "start": "5", "limit": 2.0}, agent)
    assert "line 5" in good.output and "line 6" in good.output and "line 7" not in good.output
    assert "not a whole number" not in good.output


# -- 11. extract_json -----------------------------------------------------------------------


@pytest.mark.parametrize("shape", ["brackets", "objects", "braces", "open_then_brackets", "huge"])
def test_deeply_nested_or_huge_text_is_no_json_not_an_exception(shape: str) -> None:
    text = {"brackets": "[" * 200_000, "objects": '{"a":' * 100_000, "braces": "{" * 100_000,
            "open_then_brackets": '{"a": ' + "[" * 100_000, "huge": "x" * 5_000_000}[shape]
    assert extract_json(text) is None


def test_ordinary_json_is_still_found() -> None:
    assert extract_json('noise {"a": [1, 2, {"b": 3}]} more') == {"a": [1, 2, {"b": 3}]}
    assert extract_json("```json\n{\"x\": 1}\n```") == {"x": 1}


# -- 12. writing on Windows -----------------------------------------------------------------


@pytest.mark.parametrize("rel", [
    "CON", "aux.txt", "src/Nul.md", "com1", "LPT9.log", "a.txt:ads", "dir:name/x", "name.",
    "trail ", "src/prn .txt",
])
def test_names_windows_would_not_make_a_file_are_refused(rel: str) -> None:
    assert windows_name_refusal(rel) is not None


@pytest.mark.parametrize("rel", ["a.txt", "console.md", "src/conf.py", ".gitignore", "auxiliary",
                                 "com10.txt", "x/../y", "lpt.txt", "nulls.py", "..foo"])
def test_ordinary_names_are_not_refused(rel: str) -> None:
    assert windows_name_refusal(rel) is None


@pytest.mark.parametrize("rel", [".GIT/hooks/pre-commit", ".Git/config", "sub/.HG/x",
                                 ".sUpErVisor/runs/x", ".git./config", "a/.SVN/b"])
def test_the_floor_is_compared_without_regard_to_case(tmp_path: Path, rel: str) -> None:
    result = _box(tmp_path).write_file(rel, "x", Scope())
    assert not result.ok and "no agent may write" in result.output
    assert not (tmp_path / rel.split("/")[0]).exists(), "no directory was made"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows names")
@pytest.mark.parametrize("rel", ["CON", "aux.txt", "a.txt:ads", "name."])
def test_windows_refuses_to_write_those_names(tmp_path: Path, rel: str) -> None:
    result = _box(tmp_path).write_file(rel, "x", Scope())
    assert not result.ok
    assert not any(p.name.lower().startswith(("con", "aux", "a.txt", "name")) for p in
                   tmp_path.iterdir())


def test_text_is_written_exactly_as_given(tmp_path: Path) -> None:
    box = _box(tmp_path)
    assert box.write_file("a.txt", "one\r\ntwo\nthree", Scope()).ok
    assert (tmp_path / "a.txt").read_bytes() == b"one\r\ntwo\nthree"
    assert box.write_file("b.txt", "caf\u00e9\n", Scope()).ok
    assert (tmp_path / "b.txt").read_bytes() == "caf\u00e9\n".encode()


def test_an_edit_is_written_to_a_temporary_file_then_swapped_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "a.txt"
    target.write_bytes(b"alpha\r\nbeta\r\n")
    swapped: list[tuple[str, str]] = []
    real = os.replace

    def replace(src: Any, dst: Any) -> None:
        swapped.append((str(src), str(dst)))
        assert Path(src).read_bytes() == b"alpha\r\nGAMMA\r\n"  # complete before it is visible
        assert target.read_bytes() == b"alpha\r\nbeta\r\n"  # and the old file is still whole
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    assert _box(tmp_path).edit_file("a.txt", "beta", "GAMMA", Scope()).ok
    assert len(swapped) == 1 and swapped[0][1] == str(target.resolve())
    assert target.read_bytes() == b"alpha\r\nGAMMA\r\n"
    assert [p.name for p in tmp_path.iterdir()] == ["a.txt"], "no temporary file is left"


def test_a_failed_edit_leaves_the_file_as_it_was_and_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    def broken(src: Any, dst: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken)
    result = _box(tmp_path).edit_file("a.txt", "beta", "GAMMA", Scope())
    assert not result.ok and "disk full" in result.output
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\n"
    assert [p.name for p in tmp_path.iterdir()] == ["a.txt"]
