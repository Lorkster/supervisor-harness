"""Fail if this tree has ruff findings the base does not, by rule and by file.

Ruff is configured in `pyproject.toml` (see `docs/quality-assessment.md` for how
the rule set was chosen), and 88 findings stand against it. A gate at zero would
be red on arrival and tell nobody anything, and a gate on the *total* is worse
than none: an unchanged count once hid six new findings behind six fixed ones in
this repository, which is why the convention has been to diff by rule and file
rather than to compare totals.

This is that convention, executed rather than remembered. It has been done by
hand on every pull request since, and by hand it will eventually be skipped.

## Both trees are measured with *this* tree's configuration

Each checkout carries its own `[tool.ruff]`, so running ruff separately in each
would compare two different rule sets the moment a pull request changed the
configuration -- reporting every newly-enabled rule as a regression, and, worse,
reporting nothing at all for a pull request that *disabled* a rule. The base is
therefore measured with the configuration from the working tree.

That is what the question deserves: "did this change introduce findings" is
about the change, not about which rules the base happened to enable. A pull
request that turns a rule on is expected to light up findings, and the right
place to account for those is its own description -- not a gate that cannot tell
them from a regression.

Usage:

    python tools/ruff_diff.py                  # against origin/main
    python tools/ruff_diff.py --base HEAD~1

Exit status is 1 when a (file, rule) pair has more findings here than in the
base, and 0 otherwise -- including when findings were *removed*, which needs no
permission.
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
import tempfile
from pathlib import Path

Key = tuple[str, str]


def ruff_findings(tree: Path, config: Path | None = None) -> collections.Counter[Key]:
    """Findings in ``tree``, counted by (path relative to the tree, rule code).

    Paths are normalised to forward slashes so a Windows run and a Linux run
    produce comparable keys -- the gate runs on both.

    ``config`` forces a specific ``pyproject.toml``, so the base checkout can be
    measured with the working tree's rule set rather than its own.
    """
    command = [sys.executable, "-m", "ruff", "check", ".", "--output-format=json"]
    if config is not None:
        command += ["--config", str(config)]
    proc = subprocess.run(
        command, cwd=tree, capture_output=True, text=True, check=False,
    )
    # ruff exits 1 when it finds something, which is the normal case here.
    if proc.returncode not in (0, 1):
        raise SystemExit(f"ruff failed in {tree}:\n{proc.stderr.strip()}")

    counts: collections.Counter[Key] = collections.Counter()
    for item in json.loads(proc.stdout or "[]"):
        raw = Path(item["filename"])
        try:
            rel = raw.resolve().relative_to(tree.resolve()).as_posix()
        except ValueError:
            rel = raw.as_posix()
        counts[(rel, item.get("code") or "?")] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main",
                        help="revision to compare against (default: origin/main)")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
    here = ruff_findings(repo)

    with tempfile.TemporaryDirectory() as tmp:
        worktree = Path(tmp) / "base"
        add = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), args.base],
            cwd=repo, capture_output=True, text=True, check=False,
        )
        if add.returncode != 0:
            print(f"could not check out {args.base}: {add.stderr.strip()}", file=sys.stderr)
            return 2
        try:
            # This tree's configuration, deliberately -- see the module docstring.
            base = ruff_findings(worktree, config=repo / "pyproject.toml")
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                           cwd=repo, capture_output=True, text=True, check=False)

    new = {key: (base[key], here[key]) for key in here if here[key] > base[key]}
    gone = {key: (base[key], here[key]) for key in base if here[key] < base[key]}

    for (path, rule), (was, now) in sorted(gone.items()):
        print(f"  fixed  {path}:{rule}  {was} -> {now}")
    for (path, rule), (was, now) in sorted(new.items()):
        print(f"  NEW    {path}:{rule}  {was} -> {now}")

    print(f"\n{sum(base.values())} findings on {args.base}, {sum(here.values())} here "
          f"(both measured with this tree's ruff configuration).")
    if new:
        print(f"{len(new)} (file, rule) pair(s) got worse. Fix them, or say in the "
              f"pull request why they stand.")
        return 1
    print("No (file, rule) pair got worse.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
