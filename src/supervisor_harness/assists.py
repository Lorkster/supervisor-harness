"""What the harness had to repair before a model's answer could be used.

The harness helps. A planning answer wrapped in prose is dug out of the prose; a
list where an object was required is boxed; a dependency naming a task by title
instead of by an id that did not exist yet is resolved; a rationale reference
naming no finding is dropped. Each of those is the right behaviour -- losing a
complete answer over a transport detail helps nobody -- and each of them was,
until this module existed, **invisible**.

That matters here more than it would elsewhere. This harness's claim is that the
subject of a judgement may not set the terms of it, and a run in which the
harness quietly repaired forty answers before judging them is not the run the
report describes. The repairs are also the sharpest available signal about a
provider: a model that needs its JSON extracted from prose on every turn is a
different routing decision from one that does not, and nothing was measuring it.

So this counts them. Not to stop doing them -- they stay -- but so that
``supervisor status`` and the final report can say how much the harness carried,
and so that a `TOOLING` or `ROUTING` lesson has a number behind it.

## Shape

A recorder is installed for the span of one answer being taken in, and
:func:`record_assist` writes to whichever one is installed. Outside a span it
writes to a recorder that discards, so no call site needs a guard:

    with assisting("analysis.security") as assists:
        payload = extract_json(raw)          # may record, deep inside providers
        ...
    if assists:
        session.emit(EventType.ASSISTS_RECORDED, assists.to_payload())

The context variable is the same reason `runtime/harness_metrics.py` in NOOA
uses one: the recording site is several layers below the code that knows which
stage is being answered, and threading a recorder through `extract_json` would
put a supervision concern into a provider signature.

This module imports nothing from the package, so `providers` can record without
depending on `core`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

#: Every kind of assist, and what each one means when you read it in a report.
#: Named for what the harness *did*, not for what the model got wrong: the log
#: should say "the harness extracted JSON from prose", which is a fact, rather
#: than "the model failed to emit JSON", which is an inference about a response
#: nobody kept.
ASSIST_KINDS: dict[str, str] = {
    "json_from_prose": "the answer was not JSON; a fenced block or a balanced span was extracted",
    "json_non_strict": "the answer parsed only once control characters were allowed in strings",
    "json_list_wrapped": "a JSON list arrived where an object was required and was boxed",
    "result_not_an_object": "a string or prose arrived where the tool takes an object",
    "dependency_by_title": "a task dependency named a title and was resolved to an id",
    "dependency_dropped": "a task dependency named nothing in this run and was dropped",
    "finding_ref_by_title": "a rationale reference named a title and was resolved to an id",
    "finding_ref_dropped": "a rationale reference named no finding and was dropped",
    "plan_unusable": "the planning answer named no usable lens; policy's own selection was used",
}

#: How many examples to keep per kind. Enough to see what shape the trouble
#: takes, few enough that a run with a thousand of them does not carry a
#: thousand strings on its log.
MAX_SAMPLES = 3


@dataclass
class Assists:
    """The repairs made while one answer was taken in."""

    stage: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    samples: dict[str, list[str]] = field(default_factory=dict)

    def record(self, kind: str, detail: str = "") -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if detail:
            kept = self.samples.setdefault(kind, [])
            if len(kept) < MAX_SAMPLES:
                kept.append(detail[:200])

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def __bool__(self) -> bool:
        return self.total > 0

    def summary(self) -> str:
        """One line, ordered by how often each happened."""
        if not self:
            return "none"
        ranked = sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return ", ".join(f"{kind}x{count}" for kind, count in ranked)

    def to_payload(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "total": self.total,
            "counts": dict(self.counts),
            "samples": {k: list(v) for k, v in self.samples.items()},
        }


class _Discarding(Assists):
    """The recorder in force outside a span. Counts nothing, costs nothing.

    A singleton rather than ``None`` so that :func:`record_assist` never has to
    test for one, which is what keeps the call sites -- several layers down, in
    code that has no idea a run is happening -- to a single unconditional line.
    """

    def record(self, kind: str, detail: str = "") -> None:
        return


_DISCARDING = _Discarding()
_current: ContextVar[Assists] = ContextVar("supervisor_assists", default=_DISCARDING)


def current_assists() -> Assists:
    """The recorder in force. Never ``None``."""
    return _current.get()


def record_assist(kind: str, detail: str = "") -> None:
    """Note one repair. A no-op unless a span is open."""
    _current.get().record(kind, detail)


@contextmanager
def assisting(stage: str = "") -> Iterator[Assists]:
    """Collect the repairs made while one answer is taken in."""
    recorder = Assists(stage=stage)
    token = _current.set(recorder)
    try:
        yield recorder
    finally:
        _current.reset(token)


__all__ = [
    "ASSIST_KINDS",
    "MAX_SAMPLES",
    "Assists",
    "assisting",
    "current_assists",
    "record_assist",
]
