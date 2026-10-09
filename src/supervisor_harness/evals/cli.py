"""``supervisor eval``: run role cases, summarise their records, score a run."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from ..config import load_config
from ..store.runstore import RunStore
from .cases import load_cases
from .runner import evaluate, load_records, parse_variant, summarise
from .score import render, scorecard


def cmd_eval_run(args: argparse.Namespace) -> int:
    cases = load_cases([Path(p) for p in args.cases])
    if args.role:
        cases = [c for c in cases if c.role in args.role]
    if args.case:
        cases = [c for c in cases if any(key in c.id for key in args.case)]
    variants = [parse_variant(v) for v in args.variant] or [parse_variant("harness")]
    if args.route:
        variants = [v if v.route else type(v)(**{**v.__dict__, "route": args.route})
                    for v in variants]
    out = Path(args.out)
    base = load_config(Path(args.workspace).resolve())
    print(f"{len(cases)} case(s) x {len(variants)} variant(s) x {args.repeat} -> {out}")
    records = asyncio.run(evaluate(cases, variants, args.repeat, out, base=base,
                                   progress=print))
    print()
    print(summarise(records))
    return 0


def cmd_eval_summary(args: argparse.Namespace) -> int:
    records: list[dict[str, Any]] = []
    for path in args.records:
        records.extend(load_records(Path(path)))
    print(summarise(records))
    return 0


def cmd_eval_score(args: argparse.Namespace) -> int:
    with RunStore.discover(Path(args.workspace).resolve()) as store:
        state = store.load_state(args.run)
    card = scorecard(state, args.accept, args.setup,
                     Path(args.tree).resolve() if args.tree else None)
    if getattr(args, "json", False):
        print(json.dumps(card, indent=2, ensure_ascii=False))
    else:
        print(render(card))
    return 0 if card.get("accepted", True) else 1


def add_eval_commands(sub: Any, common: argparse.ArgumentParser) -> None:
    p = sub.add_parser("eval", parents=[common],
                       help="measure a role on recorded cases, or score a whole run")
    esub = p.add_subparsers(dest="eval_command", required=True)

    r = esub.add_parser("run", parents=[common],
                        help="run role cases under variants and summarise how often each passed")
    r.add_argument("cases", nargs="+", help="case files, or directories of them")
    r.add_argument("--variant", action="append", default=[],
                   help="name:key=value,...  keys: route, think (on/off), sampling "
                        "(model/harness), planner (conversation); repeatable; default is "
                        "how the harness calls each role today")
    r.add_argument("--route", default="", help="model route for variants that set none")
    r.add_argument("--repeat", type=int, default=3, help="runs per case and variant")
    r.add_argument("--role", action="append", default=[], help="only this role (repeatable)")
    r.add_argument("--case", action="append", default=[],
                   help="only cases whose id contains this (repeatable)")
    r.add_argument("--out", default="eval-records.jsonl",
                   help="JSON-lines file the records are appended to")
    r.set_defaults(func=cmd_eval_run)

    s = esub.add_parser("summary", parents=[common], help="summarise record files")
    s.add_argument("records", nargs="+")
    s.set_defaults(func=cmd_eval_summary)

    c = esub.add_parser("score", parents=[common],
                        help="a run's record beside its acceptance commands, run on its tree")
    c.add_argument("run", help="the run id")
    c.add_argument("--accept", action="append", default=[],
                   help="an acceptance command, as you would type it (repeatable)")
    c.add_argument("--setup", action="append", default=[],
                   help="a command to run first, such as `npm ci` (repeatable)")
    c.add_argument("--tree", default="", help="check this tree instead of the run's own")
    c.set_defaults(func=cmd_eval_score)
