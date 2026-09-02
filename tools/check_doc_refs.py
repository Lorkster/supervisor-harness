"""Check that documentation citing code by line number still points at it.

`docs/reasoning-control-plane.md` explains each dimension of the design by
pointing at the code that implements it, by file and line. That is the useful
form -- a reader can go and look -- and it is also the form that rots silently:
the code moves, the line number does not, and the document goes on asserting
something it no longer shows.

Nothing else in this repository has that failure mode, because everything else
that names code is a test that would fail. This is the guard for the docs.

## What it checks

For every Markdown link whose target is `<path>:<line>`:

* the file exists;
* the line exists;
* and the **symbol the prose names** is on or beside that line.

The third is the one that matters. The convention it relies on is already how
the prose reads -- the symbol is named in backticks just before the link:

    `VCS_DIRS` and `STORE_DIRS` in [core/tools.py:118](../src/.../tools.py:118)

so the checker takes the backticked identifiers shortly before a citation and
requires one of them to appear within a couple of lines of the target. A
citation with no backticked symbol near it cannot be checked this way and is
reported as unanchored rather than passed, because an unanchored line number is
the thing this tool exists to prevent.

Run it over every doc:

    python tools/check_doc_refs.py

or over named files. Exits non-zero on any problem.
"""

from __future__ import annotations

import pathlib
import re
import sys

# [label](target) where target may carry a :line suffix.
LINK = re.compile(r"\[(?P<label>[^\]]*)\]\((?P<target>[^)\s]+?)(?::(?P<line>\d+))?\)")
# `Identifier` or `module.thing` in backticks.
SYMBOL = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)`")
# How far back in the prose to look for the symbol a citation is anchored to.
LOOKBEHIND = 160
# How many lines either side of the cited line count as "still points at it".
SLACK = 2


def check(doc: pathlib.Path) -> list[str]:
    text = doc.read_text(encoding="utf-8")
    problems: list[str] = []

    for match in LINK.finditer(text):
        target, line = match.group("target"), match.group("line")
        if target.startswith(("http://", "https://", "#")):
            continue

        path = (doc.parent / target.split("#", 1)[0]).resolve()
        if not path.exists():
            problems.append(f"{doc}: missing file: {target}")
            continue
        if line is None:
            continue

        lines = path.read_text(encoding="utf-8").splitlines()
        n = int(line)
        if not 1 <= n <= len(lines):
            problems.append(
                f"{doc}: {target}:{n} is past the end of the file ({len(lines)} lines)"
            )
            continue

        anchors = SYMBOL.findall(text[max(0, match.start() - LOOKBEHIND) : match.start()])
        if not anchors:
            problems.append(
                f"{doc}: {target}:{n} names no symbol in backticks nearby, so it "
                f"cannot be checked -- anchor it or drop the line number"
            )
            continue

        window = "\n".join(lines[max(0, n - 1 - SLACK) : n + SLACK])
        # A dotted reference is cited by its last segment (`envelope.attenuate`).
        if not any(a.rsplit(".", 1)[-1] in window for a in anchors):
            problems.append(
                f"{doc}: {target}:{n} no longer shows {' or '.join(anchors)}\n"
                f"    line {n} is: {lines[n - 1].strip()[:100]}"
            )

    return problems


def main(argv: list[str]) -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    docs = [pathlib.Path(a) for a in argv[1:]] or sorted(root.glob("docs/*.md"))
    docs += [root / "README.md"] if not argv[1:] else []

    problems: list[str] = []
    for doc in docs:
        if doc.exists():
            problems.extend(check(doc))

    if problems:
        print("\n".join(problems))
        print(f"\n{len(problems)} stale or unanchored reference(s).")
        return 1

    print(f"{len(docs)} document(s) checked; every code citation resolves.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
