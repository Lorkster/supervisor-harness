"""Run cases under variants, several times each, and say how often each passed.

One record per (case, variant, repetition), appended to a JSON-lines file as it
lands, so a long evaluation that is stopped keeps what it measured. The summary
is per role and variant: how many runs passed every check, and how often each
check passed, with the cases behind each so a rate is never read without them.
"""

from __future__ import annotations

import json
import tempfile
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import HarnessConfig
from ..ids import now_iso
from ..providers.router import ModelRouter
from .cases import Case
from .checks import judge
from .fixtures import materialise
from .roles import Variant, run_role


def parse_variant(spec: str) -> Variant:
    """``name:key=value,key=value`` -- keys ``route``, ``think``, ``sampling``."""
    name, _, rest = spec.partition(":")
    fields: dict[str, Any] = {"name": name or "harness"}
    for pair in filter(None, rest.split(",")):
        key, _, value = pair.partition("=")
        key = key.strip()
        if key == "route":
            fields["route"] = value.strip()
        elif key == "think":
            fields["think"] = value.strip().lower() in ("1", "true", "on", "yes")
        elif key == "sampling":
            fields["model_sampling"] = value.strip().lower() in ("model", "1", "true", "on")
        else:
            raise ValueError(f"variant {spec!r}: unknown key {key!r} "
                             "(route, think, sampling)")
    return Variant(**fields)


async def evaluate(
    cases: list[Case], variants: list[Variant], repeat: int, out: Path,
    base: HarnessConfig | None = None, router: ModelRouter | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Every case under every variant, ``repeat`` times; records appended to ``out``."""
    out.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    work = Path(tempfile.mkdtemp(prefix="eval-store-"))
    for case in cases:
        for variant in variants:
            for rep in range(1, repeat + 1):
                with materialise(case, work / "repos") as workspace:
                    output = await run_role(case, workspace, work / "runs", variant,
                                            base=base, router=router)
                    checks = judge(case, output, workspace)
                record = {
                    "case": case.id, "role": case.role, "variant": variant.name,
                    "repetition": rep, "at": now_iso(),
                    "passed": all(c["passed"] for c in checks.values()),
                    "checks": checks, "output": output.summary(),
                }
                records.append(record)
                with out.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record) + "\n")
                if progress is not None:
                    failed = [k for k, v in checks.items() if not v["passed"]]
                    progress(f"{case.id} [{variant.name} #{rep}] "
                             f"{'pass' if record['passed'] else 'FAIL ' + ', '.join(failed)}"
                             f" ({output.seconds:.0f}s)")
    return records


def summarise(records: list[dict[str, Any]]) -> str:
    """A Markdown table per role and variant, then each check's pass rate."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[(record["role"], record["variant"])].append(record)
    lines = ["| role | variant | cases | runs | all checks passed |",
             "|---|---|---|---|---|"]
    for (role, variant), group in sorted(groups.items()):
        passed = sum(r["passed"] for r in group)
        lines.append(f"| {role} | {variant} | {len({r['case'] for r in group})} | "
                     f"{len(group)} | {passed}/{len(group)} ({100 * passed // len(group)}%) |")
    lines += ["", "| role | variant | check | passed | failing cases |", "|---|---|---|---|---|"]
    for (role, variant), group in sorted(groups.items()):
        names = sorted({name for r in group for name in r["checks"]})
        for name in names:
            runs = [r for r in group if name in r["checks"]]
            ok = sum(r["checks"][name]["passed"] for r in runs)
            failing = sorted({r["case"] for r in runs if not r["checks"][name]["passed"]})
            lines.append(f"| {role} | {variant} | {name} | {ok}/{len(runs)} | "
                         f"{', '.join(failing) or '-'} |")
    return "\n".join(lines)


def load_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
