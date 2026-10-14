"""Two checks that failed work they should have passed, in Minesweeper run 13.

The unit suite's criterion expected "exit code 0 and output contains at least
'30 passed'"; it was looked for as text, and 51 passing tests failed it on all
three attempts. The page's inspection went to the verifier, whose pass quoted
the page's attributes bare -- `id="status" (12), id="mines-left" (13)` -- and
was thrown out as quoting nothing. A mechanical result is not open to appeal,
so both tasks failed three times over work that was done.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from supervisor_harness.core.dod import _meets, quotes_the_file, verify_command
from supervisor_harness.models import CriterionStatus, DoDCriterion, VerifyMethod

RAN = " RUN  v2.1.9\n tests/engine.test.ts (51 tests) 13ms\n Tests  51 passed (51)\n"


@pytest.mark.parametrize(("expect", "code", "output", "ok"), [
    ("exit code 0 and output contains at least '30 passed'", 0, RAN, True),
    ("exit code 0 and output contains at least '30 passed'", 1, RAN, False),
    ("exit code 0 and output contains at least '60 passed'", 0, RAN, False),
    ("exit 0, and contains 'engine.test.ts'", 0, RAN, True),
    ("exit 0 and contains 'nowhere.ts'", 0, RAN, False),
    ("'30 passed'", 0, RAN, True),                       # a floor, quoted
    ("output contains 'engine.test.ts'", 0, RAN, True),  # the text, not the phrase
    ("output contains 'engine.test.ts'", 1, RAN, False),  # of a command that succeeded
    ("Tests and passed", 0, RAN, False),                 # no code or count: text, whole
    ("0", 0, RAN, True), ("exit code 1", 1, RAN, True),
    ("exit code 1 and contains 'engine.test.ts'", 1, RAN, True),  # the code says the exit
    ("exit code 1 and contains 'engine.test.ts'", 0, RAN, False),
])
def test_an_expectation_is_read_part_by_part(expect: str, code: int, output: str,
                                             ok: bool) -> None:
    assert _meets(expect, code, output)[0] is ok


def test_the_run_13_criterion_passes_the_suite_it_ran(tmp_path: Path) -> None:
    (tmp_path / "suite.py").write_text(f"print({RAN!r})\n", encoding="utf-8")
    crit = DoDCriterion(statement="the unit suite passes", method=VerifyMethod.TEST,
                        command=f"{Path(sys.executable).name} suite.py",
                        expect="exit code 0 and output contains at least '30 passed'")
    outcome = verify_command(crit, tmp_path)
    assert outcome.status is CriterionStatus.PASS, outcome.evidence
    assert "read as at least 30 passing; the run reported 51 passed" in outcome.evidence


PAGE = """<main id="app">
  <span id="status" aria-live="polite"></span>
  <span id="mines-left" title="Mines left">0</span>
  <div id="board" role="grid" aria-label="Minesweeper board"></div>
</main>
"""


def test_code_quoted_bare_is_a_quote(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(PAGE, encoding="utf-8")
    crit = DoDCriterion(statement="every contracted id", method=VerifyMethod.INSPECTION,
                        expect='index.html: id="board", id="status" all present')

    assert quotes_the_file(crit, 'index.html:12-27 -- id="status" (12), id="mines-left" (13), '
                                 'and <div id="board" role="grid" ...> (27).', tmp_path)
    assert not quotes_the_file(crit, "the ids are all present in index.html", tmp_path)
    assert not quotes_the_file(crit, "mines-left and aria-label are there", tmp_path), (
        "a bare name says nothing of what the file does with it")
    assert not quotes_the_file(crit, 'id="scoreboard" (14)', tmp_path), "not in the file"
