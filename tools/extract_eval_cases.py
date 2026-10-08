"""Turn a recorded run into batch F cases: a planner case, review cases, verifier cases.

    python tools/extract_eval_cases.py RUN_STATE.json --repo '${PLANTSANDCLIMATE}' \
        --name p3-18-run18 --out evals/cases/p3-18 [--request-file prompt.txt]
        [--verifier-tree PATH_TO_CLONE]

- **planner**: the run's request and the findings its lenses recorded, on the
  run's base commit -- what the synthesis saw.
- **review**: each proposed task, with the rest of the plan, as the reviewer saw
  it at approval.
- **verifier** (with --verifier-tree): each task the run verified, its review
  criteria reset to unjudged, and the run's change as a patch on the base.

The cases carry the generic checks of their role. Expectations a person can
stand behind -- this task should be vetoed -- are added by hand afterwards, with
``labelled_by`` saying who judged them and on what.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

REVIEW_METHOD = "review"


def _write(out: Path, name: str, case: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.json").write_text(json.dumps(case, indent=2, ensure_ascii=False) + "\n",
                                      encoding="utf-8")


def _slug(task: dict[str, Any]) -> str:
    words = "".join(ch.lower() if ch.isalnum() else " " for ch in task["title"]).split()
    # The id's tail too: titles share their first words ("Add offline
    # catch-and-degrade to ..." three times in one run).
    return "-".join(words[:6]) + "-" + str(task["id"])[-4:].lower()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("state", type=Path)
    ap.add_argument("--repo", required=True, help="the fixture repo, e.g. '${NAME}' or a URL")
    ap.add_argument("--name", required=True, help="prefix for the case ids")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--request-file", type=Path,
                    help="use this request instead of the run's own")
    ap.add_argument("--verifier-tree", type=Path,
                    help="a clone holding the run's commit, to diff it against the base")
    args = ap.parse_args()

    state = json.loads(args.state.read_text(encoding="utf-8"))
    request = (args.request_file.read_text(encoding="utf-8").strip() if args.request_file
               else state["prompt"])
    worktree = state.get("worktree") or {}
    base = worktree.get("base", "")
    fixture = {"kind": "git", "repo": args.repo, "commit": base}
    tasks = list(state["tasks"].values())

    _write(args.out, f"{args.name}-planner", {
        "id": f"{args.name}-planner", "role": "planner", "fixture": fixture,
        "request": request, "input": {"findings": state.get("findings", [])},
        "labelled_by": "",
    })
    for task in tasks:
        plan = [t for t in tasks if t["id"] != task["id"]]
        name = f"{args.name}-review-{_slug(task)}"
        _write(args.out, name, {"id": name, "role": "review", "fixture": fixture,
                                "request": request, "input": {"task": task, "plan": plan},
                                "labelled_by": ""})

    if args.verifier_tree and worktree.get("commit"):
        diff = subprocess.run(["git", "-C", str(args.verifier_tree), "diff", "--binary",
                               base, worktree["commit"]],
                              capture_output=True, text=True, check=True).stdout
        patch = f"{args.name}.patch"
        (args.out / patch).write_text(diff, encoding="utf-8", newline="\n")
        for task in tasks:
            if task["status"] != "verified":
                continue
            judged = [dict(c, status="unverified", evidence="", verified_by="")
                      for c in task["dod"] if c["method"] == REVIEW_METHOD]
            if not judged:
                continue
            name = f"{args.name}-verifier-{_slug(task)}"
            _write(args.out, name, {
                "id": name, "role": "verifier", "fixture": dict(fixture, patch=patch),
                "request": request,
                "input": {"task": dict(task, dod=judged), "change_summary": ""},
                "labelled_by": "",
            })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
