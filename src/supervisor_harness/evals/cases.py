"""A case: one role, one workspace, one input, and how its output is judged.

Cases are JSON files, so a person can read, label and add them without code:

    {
      "id": "p3-18-review-plantdetail",
      "role": "review",                      # planner | review | verifier
      "fixture": {"kind": "git", "repo": "${PLANTSANDCLIMATE}", "commit": "cfdb95c"},
      "request": "Do task P3-18 ...",
      "input": {...},                        # what the role is given; see roles.py
      "checks": ["ruled", {"check": "verdict_in", "verdicts": ["off_request"]}],
      "labelled_by": "who judged the expectations, and on what"
    }

A fixture is the workspace the role works in, and is one of:

- ``git``: a repository (path or URL; ``${VAR}`` is expanded from the
  environment, so a case does not carry one machine's paths) at a commit;
- ``files``: the files to write, ``{}`` for an empty directory -- a green-field
  project, where the request is all there is;
- ``dir``: a directory, relative to the case file, copied as it is.

Any fixture may name a ``patch``, a diff relative to the case file applied on
top -- how a verifier case carries the change it judges.

``checks`` lists check names (see checks.py), or objects naming a check and its
parameters. A case that lists none is judged by its role's generic checks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROLES = ("planner", "review", "verifier")
FIXTURE_KINDS = ("git", "files", "dir")


@dataclass
class Fixture:
    kind: str
    repo: str = ""
    commit: str = ""
    files: dict[str, str] = field(default_factory=dict)
    path: str = ""
    #: A diff applied on top, relative to the case file: the change a verifier
    #: judges, carried with the case rather than as a commit only one clone has.
    patch: str = ""


@dataclass
class Case:
    id: str
    role: str
    fixture: Fixture
    request: str
    input: dict[str, Any] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)
    labelled_by: str = ""
    source: Path | None = None

    @property
    def greenfield(self) -> bool:
        """A workspace with nothing in it: every path a plan names is one it creates."""
        return self.fixture.kind == "files" and not self.fixture.files


class CaseError(ValueError):
    """A case file that cannot be used, and why."""


def parse_case(raw: dict[str, Any], source: Path | None = None) -> Case:
    where = f" ({source})" if source else ""
    role = str(raw.get("role", ""))
    if role not in ROLES:
        raise CaseError(f"case {raw.get('id', '?')!r}{where}: role must be one of {ROLES}")
    fx = raw.get("fixture") or {}
    kind = str(fx.get("kind", ""))
    if kind not in FIXTURE_KINDS:
        raise CaseError(f"case {raw.get('id', '?')!r}{where}: fixture kind must be one of "
                        f"{FIXTURE_KINDS}")
    if kind == "git" and not (fx.get("repo") and fx.get("commit")):
        raise CaseError(f"case {raw.get('id', '?')!r}{where}: a git fixture needs repo and "
                        "commit")
    checks = [c if isinstance(c, dict) else {"check": str(c)} for c in raw.get("checks", [])]
    return Case(
        id=str(raw.get("id") or (source.stem if source else "case")),
        role=role,
        fixture=Fixture(kind=kind, repo=str(fx.get("repo", "")),
                        commit=str(fx.get("commit", "")),
                        files={str(k): str(v) for k, v in (fx.get("files") or {}).items()},
                        path=str(fx.get("path", "")), patch=str(fx.get("patch", ""))),
        request=str(raw.get("request", "")),
        input=dict(raw.get("input") or {}),
        checks=checks,
        labelled_by=str(raw.get("labelled_by", "")),
        source=source,
    )


def load_cases(paths: list[Path]) -> list[Case]:
    """Every case in ``paths``: files, or directories searched for ``*.json``."""
    files: list[Path] = []
    for path in paths:
        files.extend(sorted(path.rglob("*.json")) if path.is_dir() else [path])
    return [parse_case(json.loads(f.read_text(encoding="utf-8")), f) for f in files]
