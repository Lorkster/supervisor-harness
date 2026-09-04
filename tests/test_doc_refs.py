"""The documentation gate, held to the standard it enforces.

`tools/check_doc_refs.py` is a CI gate with no tests, which is the shape
criterion 10 of the quality assessment is about: an entry point nobody exercises
is untested software however careful it looks. It also has the failure mode it
exists to prevent -- a check that passes everything is indistinguishable from a
repository with nothing wrong in it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_doc_refs", ROOT / "tools" / "check_doc_refs.py"
)
assert _spec and _spec.loader
refs = importlib.util.module_from_spec(_spec)
sys.modules["check_doc_refs"] = refs
_spec.loader.exec_module(refs)


def _doc(tmp_path: Path, body: str) -> Path:
    """A document beside a small module it can cite."""
    # Padded, so a stale citation lands outside the tool's two-line slack.
    (tmp_path / "code.py").write_text(
        "# one\n# two\n# three\n# four\n# five\ndef target() -> None:\n    pass\n",
        encoding="utf-8",
    )
    doc = tmp_path / "doc.md"
    doc.write_text(body, encoding="utf-8")
    return doc


def test_a_citation_that_still_points_at_its_symbol_passes(tmp_path: Path) -> None:
    assert refs.check(_doc(tmp_path, "The `target` function ([code.py:6](code.py:6)).")) == []


def test_a_line_number_that_has_moved_is_caught(tmp_path: Path) -> None:
    problems = refs.check(_doc(tmp_path, "The `target` function ([code.py:1](code.py:1))."))
    assert len(problems) == 1
    assert "no longer shows target" in problems[0]


def test_a_citation_past_the_end_of_the_file_is_caught(tmp_path: Path) -> None:
    problems = refs.check(_doc(tmp_path, "The `target` function ([code.py:99](code.py:99))."))
    assert len(problems) == 1
    assert "past the end" in problems[0]


def test_a_link_to_a_file_that_does_not_exist_is_caught(tmp_path: Path) -> None:
    problems = refs.check(_doc(tmp_path, "See [gone.py](gone.py)."))
    assert len(problems) == 1
    assert "missing file" in problems[0]


def test_a_citation_anchored_to_no_symbol_is_caught(tmp_path: Path) -> None:
    """An unanchored line number is exactly what the tool exists to prevent."""
    problems = refs.check(_doc(tmp_path, "See [the code](code.py:6)."))
    assert len(problems) == 1
    assert "names no symbol" in problems[0]


def test_a_bare_line_citation_is_caught(tmp_path: Path) -> None:
    """The form the gate could not see, and the one that had rotted."""
    problems = refs.check(_doc(tmp_path, "`state.facts` is written in code.py:6."))
    assert len(problems) == 1
    assert "bare line citation" in problems[0]


def test_a_bare_citation_inside_a_link_is_not_double_reported(tmp_path: Path) -> None:
    assert refs.check(_doc(tmp_path, "The `target` ([code.py:6](code.py:6)).")) == []


def test_a_file_named_without_a_line_number_is_not_a_citation(tmp_path: Path) -> None:
    """Naming a module in prose has to stay free; only the line number rots."""
    assert refs.check(_doc(tmp_path, "`target` lives in code.py, near the top.")) == []


# -- the layout listing ------------------------------------------------------


def _package(tmp_path: Path, listing: str) -> Path:
    """A miniature repository: two modules, and a README describing them."""
    package = tmp_path / "src" / "supervisor_harness"
    (package / "core").mkdir(parents=True)
    for path in ("__init__.py", "models.py"):
        (package / path).write_text("", encoding="utf-8")
    for path in ("__init__.py", "drift.py"):
        (package / "core" / path).write_text("", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        f"## Layout\n\n```\nsrc/supervisor_harness/\n{listing}```\n", encoding="utf-8"
    )
    return tmp_path


def test_a_layout_listing_that_matches_the_package_passes(tmp_path: Path) -> None:
    root = _package(tmp_path, "  models.py     domain types\n  core/\n    drift.py   heuristics\n")
    assert refs.check_layout(root) == []


def test_a_module_missing_from_the_listing_is_caught(tmp_path: Path) -> None:
    """The defect this check was written for: the split moved code, the README did not."""
    root = _package(tmp_path, "  models.py     domain types\n  core/\n")
    problems = refs.check_layout(root)
    assert len(problems) == 1
    assert "core/drift.py is not in the layout listing" in problems[0]


def test_a_listing_naming_something_that_is_gone_is_caught(tmp_path: Path) -> None:
    root = _package(
        tmp_path,
        "  models.py  domain types\n  core/\n    drift.py  heuristics\n    removed.py  gone\n",
    )
    problems = refs.check_layout(root)
    assert len(problems) == 1
    assert "removed.py, which does not exist" in problems[0]


def test_a_listing_naming_a_directory_that_is_gone_is_caught(tmp_path: Path) -> None:
    root = _package(
        tmp_path,
        "  models.py  domain types\n  gone/    nothing here\n  core/\n    drift.py  heuristics\n",
    )
    problems = refs.check_layout(root)
    assert ["gone/, which is not a directory" in p for p in problems].count(True) == 1


def test_a_readme_with_no_layout_block_is_reported(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("no layout here\n", encoding="utf-8")
    assert "no `src/supervisor_harness/` layout block" in refs.check_layout(tmp_path)[0]


# -- the whole tool ----------------------------------------------------------


def test_the_gate_passes_over_this_repository_as_it_stands() -> None:
    """The end-to-end call CI makes, exercised where a failure is readable."""
    assert refs.main(["check_doc_refs.py"]) == 0


def test_the_gate_fails_and_says_so(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc = _doc(tmp_path, "See [the code](code.py:6).")
    assert refs.main(["check_doc_refs.py", str(doc)]) == 1
    assert "names no symbol" in capsys.readouterr().out
