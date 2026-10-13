"""Verification reads a criterion as it was meant, not only as it was spelled.

Minesweeper run 2 built a game that passed both of its project's gates, all 46
hidden logic tests and 26 of 27 hidden browser tests -- and the harness verified
one task of four. Each of the other three failed on how a check was read:

* `tests/e2e: keyboard.press('F')` -- a directory the task filled with six spec
  files -- failed as "tests/e2e does not exist": only files were looked in;
* `npm test` expecting "20 passed" failed on output saying "35 passed";
* a verifier's pass of `dist/index.html: <script type="module">` quoted the file
  with an ellipsis -- `<script type="module" crossorigin>(function(){...}...` --
  and was thrown out as quoting nothing in it.

Three attempts of one task went on the last of them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from supervisor_harness.core.dod import quotes_the_file, verify_command, verify_inspection
from supervisor_harness.models import CriterionStatus, DoDCriterion, VerifyMethod


def _inspect(expect: str) -> DoDCriterion:
    return DoDCriterion(statement="s", method=VerifyMethod.INSPECTION, expect=expect)


@pytest.fixture
def specs(tmp_path: Path) -> Path:
    e2e = tmp_path / "tests" / "e2e"
    e2e.mkdir(parents=True)
    (e2e / "board.spec.ts").write_text("test('grid', async () => {})\n", encoding="utf-8")
    (e2e / "keyboard.spec.ts").write_text("// keys\nawait page.keyboard.press('F')\n",
                                         encoding="utf-8")
    (e2e / "node_modules").mkdir()
    (e2e / "node_modules" / "dep.js").write_text("onerror=alert(1)", encoding="utf-8")
    return tmp_path


# -- an inspection of a directory --------------------------------------------------------


def test_an_inspection_of_a_directory_finds_the_file_that_holds_the_text(specs: Path) -> None:
    outcome = verify_inspection(_inspect("tests/e2e: keyboard.press('F')"), specs)

    assert outcome.status is CriterionStatus.PASS
    assert outcome.evidence.startswith("tests/e2e/keyboard.spec.ts:2 contains")


def test_text_no_file_holds_is_a_wording_miss_for_the_verifier(specs: Path) -> None:
    outcome = verify_inspection(_inspect("tests/e2e: onerror=alert(1)"), specs)

    assert outcome.status is CriterionStatus.FAIL and outcome.wording_only
    assert "no file under tests/e2e" in outcome.evidence, "dependencies are not the project's"


def test_a_directory_with_nothing_in_it_or_no_text_asked(specs: Path) -> None:
    (specs / "empty").mkdir()
    assert verify_inspection(_inspect("empty: x"), specs).status is CriterionStatus.FAIL
    held = verify_inspection(_inspect("tests/e2e:"), specs)
    assert held.status is CriterionStatus.PASS and "holds 2 file(s)" in held.evidence


# -- a verifier's quote, shortened ------------------------------------------------------


def test_a_quote_shortened_with_an_ellipsis_counts_by_its_pieces(tmp_path: Path) -> None:
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text(
        '<head>\n  <script type="module" crossorigin>(function(){const a=1;f()})();'
        "typeof document<\"u\"&&f();</script>\n</head>", encoding="utf-8")
    crit = _inspect('dist/index.html: <script type="module">')

    quoted = ('dist/index.html:2 contains `<script type="module" crossorigin>(function(){...}'
              '...typeof document<"u"&&f();</script>` -- the code is inlined')
    assert quotes_the_file(crit, quoted, tmp_path)
    assert quotes_the_file(crit, "`<script type=\"module\" crossorigin>(function(){…`", tmp_path)
    assert not quotes_the_file(crit, "`<script src=...>`", tmp_path), "pieces too short to count"
    assert not quotes_the_file(crit, "`<script type=\"text/babel\">` is there", tmp_path)


def test_a_quote_from_a_directory_counts_from_any_of_its_files(specs: Path) -> None:
    crit = _inspect("tests/e2e: keyboard.press('Space')")

    assert quotes_the_file(crit, "`await page.keyboard.press('F')`", specs)
    assert not quotes_the_file(crit, "`onerror=alert(1)` sits in a dependency", specs)


# -- a count of passing tests is a floor -------------------------------------------------


def _suite(tmp_path: Path, printed: str, code: int = 0) -> Path:
    (tmp_path / "suite.py").write_text(
        f"import sys\nprint({printed!r})\nsys.exit({code})\n", encoding="utf-8")
    return tmp_path


def _run(tmp_path: Path, expect: str) -> CriterionStatus:
    crit = DoDCriterion(statement="s", method=VerifyMethod.COMMAND, command="python suite.py",
                        expect=expect)
    return verify_command(crit, tmp_path, timeout=60).status


@pytest.mark.parametrize("expect", ["20 passed", "at least 20 tests passed", "20 passing",
                                    "35 passed"])
def test_more_passing_than_asked_for_passes(tmp_path: Path, expect: str) -> None:
    vitest = "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[32m35 passed\x1b[39m\x1b[22m (35)"
    assert _run(_suite(tmp_path, vitest), expect) is CriterionStatus.PASS


def test_fewer_passing_or_a_failing_run_fails(tmp_path: Path) -> None:
    assert _run(_suite(tmp_path, "Tests  35 passed (35)"), "40 passed") is CriterionStatus.FAIL
    assert _run(_suite(tmp_path, "35 passed, 2 failed", 1), "20 passed") is CriterionStatus.FAIL
    assert _run(_suite(tmp_path, "no tests ran"), "20 passed") is CriterionStatus.FAIL


def test_other_text_is_still_text(tmp_path: Path) -> None:
    assert _run(_suite(tmp_path, "all green"), "all green") is CriterionStatus.PASS
    assert _run(_suite(tmp_path, "all green"), "passed") is CriterionStatus.FAIL
