"""Enforcing and observing are different, and only one of them may fail a run.

Batch 8 of `docs/development-plan.md`, and the smallest of them: almost nothing
here is new behaviour. The harness already draws this line everywhere -- the
fence refuses and its refusal propagates; the progress file, the SQLite index
and the lesson-consolidation pass swallow their own failures and say so on the
log. What it did not have was the line *stated*, or any check that a later
change had not crossed it.

NOOA's middleware documentation draws it as a rule and holds it: interception
may block and transform, and its exceptions propagate, because it is control
flow; observation cannot change anything, and its exceptions are isolated. That
is worth adopting as an invariant here rather than as a description, because the
harness's whole claim is that a judgement is made on terms its subject does not
set -- and an observer that could move a verdict, or an enforcer that failed
open, would break that claim without breaking a single existing test.

Two directions, both tested:

* **an enforcing surface fails closed** -- an error inside a check refuses, and
  never returns the permissive answer;
* **an observing surface cannot move a verdict** -- one that raises is isolated,
  and the run reaches the same conclusion it would have reached anyway.

Plus the two hazards NOOA's own documentation names, which apply here for
reasons of our own: enforcement must not recurse into work it triggered, and
anything on a path that can be re-run must be idempotent -- the remediation loop
re-issues packets and re-verifies criteria.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness.config import HarnessConfig, Policy, default_config
from supervisor_harness.core import dod
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.core.tools import COMMAND_KINDS, WRITE_KINDS, Toolbox
from supervisor_harness.host.detect import HostInfo
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    Backend,
    CriterionStatus,
    DoDCriterion,
    RunMode,
    Scope,
    VerifyMethod,
)
from supervisor_harness.store import progress, runstore
from supervisor_harness.store.runstore import RunStore

from .conftest import FakeProvider
from .test_host_delegation import HostSimulator

SRC = Path(__file__).resolve().parents[1] / "src" / "supervisor_harness"
PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"

#: The modules that decide whether something is allowed. An error inside one of
#: these must refuse; there is no error here whose right answer is "go ahead".
ENFORCING_MODULES = ("core/tools.py", "core/dod.py", "core/envelope.py", "core/facts.py")

#: Where a broad `except Exception` or `suppress(Exception)` is allowed, and the
#: observing surface each one protects. A new entry is a deliberate decision to
#: make something unable to fail a run, so it belongs in a list someone has to
#: edit -- not in whichever module happened to need it.
#:
#: Everything here is derived, disposable, or an extra: losing it costs a
#: convenience over the record, never the record.
BROAD_SUPPRESSION_ALLOWED = {
    "cli.py": "rendering a progress line must not fail a command",
    "config.py": "an unrecognised env override is ignored, not fatal",
    "core/consolidate.py": "a reasoner that raises leaves the library untidied, not broken",
    "core/supervisor.py": "the learning pass, the tidy-up, the drift second opinion, "
                          "and one agent's crash -- none may end a run",
    "mcp_server.py": "the ledger is a progress line over the response, not part of it",
    "providers/bedrock.py": "a provider's own failure is reported, not raised",
    "providers/router.py": "provider bugs must not kill a run; health must never raise",
    "serde.py": "a value that will not serialise is rendered, not fatal",
    "store/events.py": "one bad record must not end the replay",
    "store/progress.py": "the progress file is derived and disposable",
    "store/runstore.py": "the SQLite index is derived; reindex repairs it",
}


def _modules() -> dict[str, ast.Module]:
    return {
        path.relative_to(SRC).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(SRC.rglob("*.py"))
    }


# -- an enforcing surface fails closed --------------------------------------


def _permissive_returns(handler: ast.ExceptHandler) -> list[str]:
    """Returns inside an error handler that answer "allowed"."""
    found: list[str] = []
    for node in ast.walk(handler):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        call = node.value
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
            continue
        if call.func.id == "ToolResult":
            ok = call.args[1] if len(call.args) > 1 else None
            if isinstance(ok, ast.Constant) and ok.value is True:
                found.append("ToolResult(..., True, ...)")
        if call.func.id == "VerificationOutcome":
            status = call.args[0] if call.args else None
            if (
                isinstance(status, ast.Attribute)
                and status.attr == "PASS"
            ):
                found.append("VerificationOutcome(CriterionStatus.PASS, ...)")
    return found


def test_no_error_handler_in_an_enforcing_module_answers_allowed() -> None:
    """The invariant, checked structurally because the regression is silent.

    An enforcing check that returns the permissive answer from inside an
    `except` reads exactly like one that decided the operation was fine. Every
    such handler today refuses -- a failed `ToolResult`, a `FAIL` or a
    `BLOCKED` -- and this is what stops the next one being written the other
    way.
    """
    offenders: list[str] = []
    for name, module in _modules().items():
        if name not in ENFORCING_MODULES:
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.ExceptHandler):
                offenders += [f"{name}:{node.lineno} returns {r}" for r in
                              _permissive_returns(node)]

    assert not offenders, "an enforcing surface fails open: " + "; ".join(offenders)


def test_a_write_that_cannot_be_made_is_refused_rather_than_reported_as_done(
    tmp_path: Path,
) -> None:
    """An OSError inside the write is not evidence that the write happened."""
    toolbox = Toolbox(tmp_path, Policy(), tmp_path / ".supervisor")

    # A directory where the file should go: the write cannot succeed.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "taken.py").mkdir()

    result = toolbox.write_file("src/taken.py", "x = 1", Scope(paths=["src/**"]))

    assert result.ok is False
    assert "could not write" in result.output


def test_a_criterion_whose_check_cannot_run_is_never_passed(tmp_path: Path) -> None:
    """`BLOCKED`, not `PASS`. The check did not run, so it proved nothing."""
    criterion = DoDCriterion(
        statement="the suite passes", method=VerifyMethod.TEST,
        command="pytest --this-flag-does-not-exist-and-the-binary-is-absent",
    )

    outcome = dod.verify_command(
        DoDCriterion(**{**criterion.__dict__, "command": "pytest -q"}),
        tmp_path / "not-a-directory-that-exists",
    )

    assert outcome.status is not CriterionStatus.PASS


def test_a_criterion_the_harness_may_not_run_is_delegated_not_assumed() -> None:
    """`None` means "someone else must prove this", not "it is fine".

    The distinction matters because `allow_command_execution` is off by
    default: the common case is that the harness declines to run the check, and
    declining must never be a pass.
    """
    criterion = DoDCriterion(statement="tests pass", method=VerifyMethod.TEST,
                             command="pytest -q")

    assert dod.verify_criterion(
        criterion, Path("."), Policy(), allow_commands=False
    ) is None


# -- an observing surface cannot move a verdict -----------------------------


def test_every_broad_suppression_is_on_a_declared_observing_surface() -> None:
    """A new one is a decision to make something unable to fail a run.

    That is occasionally right and never incidental, so it belongs in a list
    someone edits rather than in whichever module happened to want it. The same
    shape as the guard against synchronous `emit` in an async function, and for
    the same reason: both are silent when reintroduced.
    """
    offenders: list[str] = []
    for name, module in _modules().items():
        for node in ast.walk(module):
            broad = (
                isinstance(node, ast.ExceptHandler)
                and isinstance(node.type, ast.Name)
                and node.type.id in ("Exception", "BaseException")
            ) or (
                isinstance(node, ast.With)
                and any(
                    isinstance(item.context_expr, ast.Call)
                    and getattr(item.context_expr.func, "attr", "") == "suppress"
                    for item in node.items
                )
            )
            if broad and name not in BROAD_SUPPRESSION_ALLOWED:
                offenders.append(f"{name}:{node.lineno}")

    assert not offenders, (
        "broad suppression outside the declared observing surfaces: "
        + ", ".join(offenders)
        + " -- add it to BROAD_SUPPRESSION_ALLOWED with the reason, or narrow it"
    )


@pytest.fixture
def host_config() -> HarnessConfig:
    config = default_config()
    config.backend = Backend.HOST
    config.routing = {k: "host" for k in config.routing}
    config.policy = Policy(default_max_turns=2, execution_max_turns=2,
                           max_analysis_lenses=1)
    return config


async def _verdict(
    workspace: Path, config: HarnessConfig, fake: FakeProvider
) -> tuple[str, str]:
    """Drive a run to the end and return what it concluded."""
    supervisor = Supervisor(
        workspace=workspace, config=config, store=RunStore(workspace / ".supervisor"),
        host=HostInfo(name="claude-code", workspace=str(workspace), confidence=1.0),
    )
    started = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    final = await HostSimulator(supervisor, fake).drive(started)
    state = supervisor.store.load_state(final.run_id)
    task = next(iter(state.tasks.values()), None)
    return final.action, str(task.status) if task else "(no task)"


async def test_a_progress_writer_that_raises_cannot_change_the_verdict(
    tmp_path: Path, host_config: HarnessConfig, fake: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The progress file is a convenience over the record, not part of it.

    Broken from the inside rather than by replacing `append`: the guarantee
    being tested is the module's own -- it promises never to raise, and its
    caller relies on that rather than wrapping it. Substituting the whole
    function would have tested whether `emit` guards a call it is entitled not
    to guard, which is a different question with a different answer.

    Written the wrong way round first, and the difference mattered: `append`
    caught `OSError` alone, so an unserialisable payload raised `TypeError` out
    of a function documented as never raising.
    """
    reference = await _verdict(tmp_path / "clean", host_config, fake)

    def explode(event: Any) -> dict[str, Any]:
        raise TypeError("this payload will not serialise")

    monkeypatch.setattr(progress, "as_line", explode)

    assert await _verdict(tmp_path / "broken", host_config, fake) == reference


def test_the_progress_writer_isolates_every_kind_of_failure(tmp_path: Path) -> None:
    """Not only the disk. An observing surface isolated from *some* of its own

    failures is not isolated: the guarantee is what the caller relies on, and a
    caller cannot know which kind of failure it is about to get.
    """
    from supervisor_harness.store.events import Event, EventType

    class Unserialisable:
        pass

    event = Event(run_id="run_x", type=EventType.NOTE,
                  payload={"text": Unserialisable()})  # type: ignore[dict-item]

    progress.append(tmp_path / "progress.ndjson", [event])   # must not raise


async def test_an_index_that_raises_cannot_change_the_verdict(
    tmp_path: Path, host_config: HarnessConfig, fake: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SQLite index is derived, and `supervisor reindex` rebuilds it."""
    reference = await _verdict(tmp_path / "clean", host_config, fake)

    def explode(self: Any, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the index is corrupt")

    monkeypatch.setattr(runstore.RunIndex, "sync_run", explode)

    assert await _verdict(tmp_path / "broken", host_config, fake) == reference


async def test_a_consolidation_pass_that_raises_cannot_change_the_verdict(
    tmp_path: Path, host_config: HarnessConfig, fake: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that produced verified work does not fail because the tidy-up did."""
    reference = await _verdict(tmp_path / "clean", host_config, fake)

    def explode(self: Any, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the library is locked")

    monkeypatch.setattr(RunStore, "update_lessons", explode)

    assert await _verdict(tmp_path / "broken", host_config, fake) == reference


# -- hazard one: enforcement must not recurse into what it fenced -----------


def test_the_kinds_that_may_write_are_the_kinds_that_may_run_commands() -> None:
    """A shell writes files, so granting one without the other grants both.

    The re-entry hazard in this codebase's own shape: a check that refused
    `write_file` while allowing `run_command` would be handing back through the
    shell exactly what it had just refused.
    """
    assert WRITE_KINDS == COMMAND_KINDS
    assert str(AgentKind.VERIFICATION) not in WRITE_KINDS
    assert str(AgentKind.ANALYSIS) not in WRITE_KINDS


def test_the_harness_running_a_check_is_not_a_door_an_agent_can_open(
    tmp_path: Path,
) -> None:
    """`verify_command` runs under the same switch that fences an agent's shell.

    A verifier used to hold the shell so it could run a criterion's real check.
    It now reports the command and the harness runs it -- under
    `allow_command_execution`, the same policy that decides whether an agent may
    run anything at all. Enforcement does not get a private path around its own
    fence.
    """
    source = inspect.getsource(dod.verify_criterion)

    assert "allow_commands" in source
    toolbox = Toolbox(tmp_path, Policy(allow_command_execution=False),
                      tmp_path / ".supervisor")
    executor = AgentSpec(run_id="run_x", kind=AgentKind.EXECUTION, scope=Scope())
    refusal = toolbox.call("run_command", {"command": "pytest -q"}, executor)

    assert refusal.ok is False


# -- hazard two: anything re-runnable must be idempotent --------------------


def test_verifying_the_same_criterion_twice_reaches_the_same_verdict(
    tmp_path: Path,
) -> None:
    """The remediation loop re-verifies, so a verdict that drifted on a second

    look would make a task's outcome depend on how many rounds it took.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "login.py").write_text("LIMIT = 5\n", encoding="utf-8")
    criterion = DoDCriterion(
        statement="the limiter is present", method=VerifyMethod.INSPECTION,
        command="", expect="LIMIT", evidence="",
    )
    criterion.command = "src/login.py"

    first = dod.verify_inspection(criterion, tmp_path)
    second = dod.verify_inspection(criterion, tmp_path)

    assert first.status is second.status
    assert first.evidence == second.evidence


async def test_the_same_turn_reported_twice_is_recorded_once(
    tmp_path: Path, host_config: HarnessConfig,
) -> None:
    """A host that retries a call it is unsure landed must not double-count it.

    The re-issue path is real -- an unanswered packet is handed out again -- so
    "reported twice" and "worked twice" have to stay distinguishable.
    """
    supervisor = Supervisor(
        workspace=tmp_path, config=host_config, store=RunStore(tmp_path / ".supervisor"),
        host=HostInfo(name="claude-code", workspace=str(tmp_path), confidence=1.0),
    )
    started = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    packet = started.packets[0]
    answer = {
        "restated_goal": "rate limit login", "mode": "execute",
        "lenses": [{"role": "security", "why": "exposure", "objectives": ["Find it"]}],
    }

    await supervisor.report(packet.run_id, packet.agent_id, answer)
    again = await supervisor.report(packet.run_id, packet.agent_id, answer)

    state = supervisor.store.load_state(packet.run_id)
    assert state.turn_counts[packet.agent_id] == 1
    assert again.detail.get("error") == "duplicate_report"


def test_flushing_the_same_repairs_twice_records_them_once() -> None:
    """`_flush_assists` clears what it emitted, so a second flush has nothing.

    A caller that reports several agents inside one span would otherwise
    attribute the first agent's repairs to all of them.
    """
    from supervisor_harness.assists import assisting, current_assists, record_assist

    with assisting("analysis.security") as assists:
        record_assist("json_from_prose", "```json")
        first = assists.to_payload()
        assists.counts.clear()
        assists.samples.clear()
        second = current_assists().to_payload()

    assert first["total"] == 1
    assert second["total"] == 0
