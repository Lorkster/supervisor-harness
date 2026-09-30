"""Findings as structured records: location and CWE as fields, and a published export.

Prerequisite P3 of security-eval, which scores a run's findings against known
vulnerabilities. It had to recover where a finding was from free-text evidence,
and read findings by folding the harness's internal state. The first is a guess;
the second breaks whenever that state changes shape.

Location and CWE are recorded only as the agent stated them. A malformed value
is left empty, never repaired into something plausible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from supervisor_harness.cli import main
from supervisor_harness.contracts import ANALYSIS_TURN_SCHEMA, parse_findings
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import Finding, RunMode
from supervisor_harness.serde import from_jsonable

from .conftest import FakeProvider


def one(raw: dict[str, object]) -> Finding:
    (finding,) = parse_findings({"findings": [{"title": "t", **raw}]}, "agt", "security")
    return finding


@pytest.mark.parametrize(("location", "expected"), [
    ("src/app/db.py:11-12", ("src/app/db.py", 11, 12)),
    ("`src/app/db.py:11`", ("src/app/db.py", 11, 11)),
    ("src\\app\\db.py:3-4", ("src/app/db.py", 3, 4)),
    ("C:\\repo\\db.py:7", ("C:/repo/db.py", 7, 7)),
    ("somewhere in the db layer", ("", 0, 0)),
    ("see src/app/db.py:11 for the query", ("", 0, 0)),  # prose, not a location
    ("src/app/db.py:12-11", ("", 0, 0)),
    ("src/app/db.py:0", ("", 0, 0)),
    ("", ("", 0, 0)),
])
def test_location_is_taken_as_stated_or_not_at_all(
    location: str, expected: tuple[str, int, int]
) -> None:
    f = one({"location": location})
    assert (f.path, f.line_start, f.line_end) == expected


@pytest.mark.parametrize(("cwe", "expected"), [
    ("CWE-89", "CWE-89"), ("cwe_089", "CWE-89"), ("79", "CWE-79"),
    ("SQL injection", ""), ("", ""), (None, ""),
])
def test_cwe_is_normalised_or_left_empty(cwe: object, expected: str) -> None:
    assert one({"cwe": cwe}).cwe == expected


def test_the_contract_asks_for_both() -> None:
    finding_schema = ANALYSIS_TURN_SCHEMA["properties"]["findings"]["items"]
    assert {"location", "cwe"} <= set(finding_schema["properties"])
    assert "location" not in finding_schema["required"], "optional: not every finding has a place"


def test_a_log_written_before_these_fields_still_reads() -> None:
    old = from_jsonable({"id": "fnd_1", "title": "old", "severity": "high"}, Finding)
    assert (old.path, old.line_start, old.cwe) == ("", 0, "")


class Locating(FakeProvider):
    """The security lens reports its finding with an absolute path, as tools often do."""

    def __init__(self, workspace: Path) -> None:
        super().__init__()
        self.workspace = workspace

    def _analysis(self, request):  # type: ignore[no-untyped-def]
        payload = super()._analysis(request)
        for finding in payload.get("findings", []):
            finding["location"] = f"{self.workspace / 'src' / 'auth' / 'login.py'}:2-3"
            finding["cwe"] = "cwe-307"
        return payload


async def test_a_recorded_finding_is_workspace_relative_and_exported(
    supervisor: Supervisor, workspace: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPERVISOR_HOME", raising=False)
    supervisor.router.register("fake", Locating(workspace))
    response = await supervisor.run("Review src/auth for security problems",
                                    mode=RunMode.REPORT)

    capsys.readouterr()
    assert main(["findings", response.run_id, "--json", "-w", str(workspace)]) == 0
    exported = json.loads(capsys.readouterr().out)

    located = [f for f in exported["findings"] if f["path"]]
    assert located, "the security lens's finding should carry its location"
    for f in located:
        assert f["path"] == "src/auth/login.py", "stored relative to the workspace"
        assert (f["line_start"], f["line_end"], f["cwe"]) == (2, 3, "CWE-307")
        assert f["status"] == "open", "report mode: no task claimed it"
    assert {"id", "lens", "severity", "title", "evidence", "status_reason"} <= set(located[0])


async def test_the_human_listing_shows_place_and_cwe(
    supervisor: Supervisor, workspace: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPERVISOR_HOME", raising=False)
    supervisor.router.register("fake", Locating(workspace))
    response = await supervisor.run("Review src/auth for security problems",
                                    mode=RunMode.REPORT)

    capsys.readouterr()
    assert main(["findings", response.run_id, "-w", str(workspace)]) == 0
    out = capsys.readouterr().out
    assert "src/auth/login.py:2-3  CWE-307" in out


def test_an_unknown_run_is_an_error_not_an_empty_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPERVISOR_HOME", raising=False)
    (tmp_path / ".supervisor" / "runs" / "run_real").mkdir(parents=True)
    (tmp_path / ".supervisor" / "runs" / "run_real" / "events.jsonl").write_text("")
    assert main(["findings", "run_nope", "-w", str(tmp_path)]) == 2
