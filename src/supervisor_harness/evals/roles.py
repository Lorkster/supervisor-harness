"""Each role, called the way the harness calls it, on a case's input.

- **planner** -- the synthesis that writes tasks. Input: ``findings`` (recorded
  `Finding` objects; none for a green-field case). Its *first* answer is what is
  judged, before the harness sends anything back: the point is how well the
  model plans, not how well the harness repairs it.
- **review** -- the veto-only reviewer (`Supervisor._review_task`). Input:
  ``task`` and ``plan`` (the other tasks, as the reviewer sees them at approval).
- **verifier** -- the verifier conversation (`Supervisor._converse_verifier`).
  Input: ``task`` with the criteria still to judge, and ``change_summary``;
  the fixture is the tree with the change in it.

A `Variant` says how a role is called: the model route, and for a one-shot role
(the planner, today) thinking and sampling. The conversational roles already
think and use the model's sampling; for them only the route varies.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agents.brief import build_verifier_brief
from ..config import HarnessConfig, load_config
from ..contracts import SYNTHESIS_SCHEMA, parse_tasks
from ..core import phases
from ..core.supervisor import Supervisor
from ..host.detect import HostInfo
from ..models import (
    AgentKind,
    AgentSpec,
    AgentStatus,
    Backend,
    ExecutionTask,
    Finding,
    RunMode,
    RunState,
    Scope,
    TaskStatus,
)
from ..providers.base import ChatMessage, CompletionRequest
from ..providers.router import ModelRouter
from ..serde import from_jsonable, to_jsonable
from ..store.runstore import RunStore
from .cases import Case


@dataclass(frozen=True)
class Variant:
    """How a role is called. ``think`` None leaves the provider's default."""

    name: str = "harness"
    route: str = ""
    think: bool | None = None
    model_sampling: bool = False
    #: The planner only: "conversation" calls it as `Supervisor._converse_synthesis`
    #: does in a run, with read-only tools; anything else, the one-shot call.
    planner: str = ""


@dataclass
class RoleOutput:
    """What a role produced, in the form the checks read."""

    tasks: list[ExecutionTask] = field(default_factory=list)   # planner
    ruling: str = ""            # review: "proceed", a veto name, or "" for no ruling
    why: str = ""
    criteria: list[dict[str, Any]] = field(default_factory=list)   # verifier
    error: str = ""
    seconds: float = 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "tasks": [{"title": t.title, "scope": t.scope.paths,
                       "dod": [c.statement for c in t.dod],
                       # What a criterion's enforceability turns on, to read back.
                       "criteria": [{"method": c.method.value, "expect": c.expect,
                                     "command": c.command} for c in t.dod]}
                      for t in self.tasks],
            "ruling": self.ruling, "why": self.why[:600], "criteria": self.criteria,
            "error": self.error, "seconds": round(self.seconds, 1),
        }


def _supervisor(workspace: Path, store: Path, variant: Variant,
                base: HarnessConfig | None) -> Supervisor:
    config = copy.deepcopy(base) if base is not None else load_config(workspace)
    config.backend = Backend.AUTONOMOUS
    if variant.route:
        config.routing = {"default": variant.route}
    # A role is measured; nothing it does is carried out.
    config.policy.allow_command_execution = False
    host = HostInfo(name="eval", workspace=str(workspace), confidence=1.0)
    return Supervisor(workspace=workspace, config=config, store=RunStore(store),
                      host=host, router=ModelRouter(config, host_name=host.name))


async def run_role(case: Case, workspace: Path, store: Path, variant: Variant,
                   base: HarnessConfig | None = None,
                   router: ModelRouter | None = None) -> RoleOutput:
    """Call the case's role once. A failure is recorded on the output, not raised."""
    sup = _supervisor(workspace, store, variant, base)
    if router is not None:
        sup.router = router
    started = time.monotonic()
    try:
        if case.role == "planner":
            out = await _planner(sup, case, variant)
        elif case.role == "review":
            out = await _review(sup, case)
        else:
            out = await _verifier(sup, case)
    except Exception as exc:  # noqa: BLE001 - one bad call is a result, not a crash
        out = RoleOutput(error=f"{type(exc).__name__}: {exc}")
    out.seconds = time.monotonic() - started
    return out


def _tasks(raw: list[Any]) -> list[ExecutionTask]:
    return [from_jsonable(t, ExecutionTask) for t in raw if isinstance(t, dict)]


async def _planner(sup: Supervisor, case: Case, variant: Variant) -> RoleOutput:
    state = RunState(prompt=case.request, workspace=str(sup.workspace), mode=RunMode.EXECUTE)
    for raw in case.input.get("findings", []):
        finding = from_jsonable(raw, Finding)
        state.findings.append(finding)
    system, user = phases.synthesis_prompt(state, RunMode.EXECUTE)
    if variant.planner == "conversation":
        session = sup.store.create(state)
        plan = await sup._converse_synthesis(session, system, user, send_back=False)
        if plan is None:
            return RoleOutput(error="the planner conversation ended without a plan")
        return RoleOutput(tasks=parse_tasks(plan, state.id, str(sup.workspace)))
    extra: dict[str, Any] = {} if variant.think is None else {"think": variant.think}
    response = await sup.router.complete("synthesis", CompletionRequest(
        system=system, messages=[ChatMessage("user", user)], json_schema=SYNTHESIS_SCHEMA,
        model_sampling=variant.model_sampling, extra=extra, timeout=900.0))
    return RoleOutput(tasks=parse_tasks(response.json(), state.id, str(sup.workspace)))


async def _review(sup: Supervisor, case: Case) -> RoleOutput:
    session = sup.store.create(RunState(prompt=case.request, workspace=str(sup.workspace)))
    (task,) = _tasks([case.input["task"]])
    for other in [task, *_tasks(case.input.get("plan", []))]:
        other.status = TaskStatus.PROPOSED
        session.state.tasks[other.id] = other
    veto = await sup._review_task(session, task)
    if veto is not None:
        return RoleOutput(ruling=veto[0], why=veto[1])
    note = next((n.text for n in reversed(session.state.notes) if "veto review" in n.text), "")
    return RoleOutput(ruling="proceed" if note.endswith(": proceed") else "")


async def _verifier(sup: Supervisor, case: Case) -> RoleOutput:
    session = sup.store.create(RunState(prompt=case.request, workspace=str(sup.workspace)))
    (task,) = _tasks([case.input["task"]])
    task.status = TaskStatus.AWAITING_VERIFICATION
    session.state.tasks[task.id] = task
    # As `phases` builds a verifier: fenced to its task's paths, bound to the
    # verification route, run by the harness rather than a host.
    agent = AgentSpec(kind=AgentKind.VERIFICATION, role="verifier", task_id=task.id,
                      title=f"Verify: {task.title}", status=AgentStatus.RUNNING,
                      scope=Scope(paths=list(task.scope.paths),
                                  forbidden_paths=list(task.scope.forbidden_paths)),
                      binding=sup.config.binding_for("verification"),
                      backend=Backend.AUTONOMOUS)
    session.state.agents[agent.id] = agent
    session.state.briefs[agent.id] = build_verifier_brief(
        session.state, task, str(case.input.get("change_summary", "")))
    await sup._converse_verifier(session, agent)
    judged = session.state.tasks[task.id]
    return RoleOutput(criteria=[
        {"statement": c.statement, "status": str(c.status), "evidence": c.evidence[:600]}
        for c in judged.dod])


def task_json(task: ExecutionTask) -> dict[str, Any]:
    """A task as a case carries it."""
    data = to_jsonable(task)
    return data if isinstance(data, dict) else {}
