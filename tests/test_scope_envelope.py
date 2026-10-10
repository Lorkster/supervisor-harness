"""The run's scope envelope: where a scope comes from, and what bounds it.

Every test here fails against the commit before this file existed, either
because the mechanism was absent or because the behaviour was the opposite.

Several are marked as guards rather than proofs. The distinction matters
because :func:`pattern_within` is sound and not complete: it proves
containment, and a "not contained" answer from it means "not provably
contained". A test that asserts narrowing happened is a proof of the fence; a
test that asserts a *particular* pattern survived is a guard on the current
decision procedure, and would legitimately change if that procedure got
sharper.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from supervisor_harness.config import HarnessConfig
from supervisor_harness.core.envelope import Ceiling, attenuate, establish, what_was_taken
from supervisor_harness.core.paths import (
    NOTHING,
    globs_within,
    minimal_globs,
    narrow_globs,
    path_matches,
    pattern_within,
)
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.core.tools import Toolbox
from supervisor_harness.models import AgentKind, RunMode, Scope, ScopeEnvelope
from supervisor_harness.providers.base import ChatMessage, CompletionRequest
from supervisor_harness.store.events import EventType, fold

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"


async def _reach_approval(supervisor: Supervisor):
    response = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    while response.action not in ("await_approval", "complete", "failed"):
        response = await supervisor.advance(response.run_id)
    assert response.action == "await_approval", response.message
    return response


def _plan_with_envelope(fake, paths: list[str]) -> None:
    """Pin the planning answer to an envelope of ``paths``."""
    plan = fake.answer_for("planning", CompletionRequest(messages=[ChatMessage("user", "")]))
    plan["envelope_paths"] = paths
    fake.overrides["planning"] = plan


# --------------------------------------------------------------------------
# The containment predicate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("inner", "outer"),
    [
        ("src/auth/**", "src/**"),          # directory inside directory
        ("src/auth/login.py", "src/**"),    # concrete path inside directory
        ("src/*", "src/**"),                # the two directory spellings
        ("src", "src/**"),                  # the directory itself
        ("src/auth/**", "src/auth/**"),     # identity
        ("src/auth/**", "**"),              # the universal pattern
        (NOTHING, "src/**"),                # the empty set is inside anything
    ],
)
def test_containment_that_is_proved(inner: str, outer: str) -> None:
    """Each of these is a proof: every path `inner` matches, `outer` matches too."""
    assert pattern_within(inner, outer)


@pytest.mark.parametrize(
    ("inner", "outer"),
    [
        ("srcfoo/**", "src/**"),            # a prefix, but not on a path boundary
        ("src/**", "src/auth/**"),          # strictly wider
        ("src/**", NOTHING),                # nothing contains no non-empty set
        ("docs/**", "src/**"),              # disjoint
    ],
)
def test_containment_that_is_refused_correctly(inner: str, outer: str) -> None:
    """Each of these genuinely does not hold, so the answer is exact, not shy."""
    assert not pattern_within(inner, outer)


@pytest.mark.parametrize(
    ("path", "inner", "outer"),
    [
        ("src/auth/login.py", "src/auth/*.py", "src/**/*.py"),  # two wildcards to compare
        ("src/a.py", "src/*.py", "*.py"),                       # outer is no directory
    ],
)
def test_containment_the_predicate_declines_to_decide(
    path: str, inner: str, outer: str
) -> None:
    """A guard on a documented limit, not a proof of a desired answer.

    Containment holds in both of these -- the path shown matches both patterns
    -- and the predicate says no, because deciding them would mean comparing
    wildcard against wildcard. Refusing is the safe direction, since callers
    narrow on False. A sharper procedure that answered True here would be an
    improvement, and would correctly make this test fail.
    """
    assert path_matches(path, inner) and path_matches(path, outer)
    assert not pattern_within(inner, outer)


def test_an_empty_glob_set_is_the_whole_workspace_on_both_sides() -> None:
    """The meaning `core/tools.py` already gives an empty scope, kept here."""
    assert globs_within(["anything/**"], [])       # everything is inside the workspace
    assert not globs_within([], ["src/**"])        # the workspace is not inside a part
    assert narrow_globs([], ["src/**"]) == ["src/**"]
    assert narrow_globs(["src/**"], []) == ["src/**"]


def test_an_empty_intersection_is_written_as_nothing_not_as_empty() -> None:
    """The trap this whole module is arranged around.

    `[]` means the whole workspace downstream, so an intersection that vanishes
    must not be spelled that way: it would widen the scope it was computed to
    narrow, turning a task proposed outside the envelope into the least fenced
    agent in the run.
    """
    assert narrow_globs(["docs/**"], ["src/**"]) == [NOTHING]
    assert not path_matches("docs/index.md", NOTHING)
    assert not path_matches("src/auth/login.py", NOTHING)
    # Including a file that is genuinely named that.
    assert not path_matches(NOTHING, NOTHING)


def test_nothing_is_a_fence_the_toolbox_honours(tmp_path: Path) -> None:
    """The sentinel is worth nothing unless the enforcer refuses it."""
    box = Toolbox(workspace=tmp_path, policy=HarnessConfig().policy)
    result = box.write_file("docs/index.md", "x", scope=Scope(paths=[NOTHING]))
    assert not result.ok
    assert not (tmp_path / "docs" / "index.md").exists()


def test_narrowing_keeps_the_provable_part_and_drops_the_rest() -> None:
    """A guard on the decision procedure, not a proof of the fence.

    `tests/**` is outside `src/**` and goes; `src/auth/**` is inside and stays.
    Which patterns survive depends on what `pattern_within` can decide, so this
    pins today's answer rather than a necessary one.
    """
    assert narrow_globs(["src/auth/**", "tests/**"], ["src/**"]) == ["src/auth/**"]
    # The intersection may be named by either side, whichever is the narrower.
    assert narrow_globs(["src/**"], ["src/auth/**"]) == ["src/auth/**"]


# --------------------------------------------------------------------------
# The envelope itself
# --------------------------------------------------------------------------


def test_a_plan_may_narrow_the_configured_envelope_but_never_widen_it() -> None:
    configured = ScopeEnvelope(paths=["src/**"], source="configuration")

    narrowed, refusals = establish(configured, ["src/auth/**"], None)
    assert narrowed.paths == ["src/auth/**"]
    assert refusals == []

    widened, refusals = establish(configured, ["src/**", "/etc/**"], None)
    assert widened.paths == ["src/**"]
    assert refusals and "does not contain" in refusals[0]


def test_forbidden_paths_accumulate_rather_than_being_replaced() -> None:
    """Both halves narrow: allowed paths intersect, forbidden paths union."""
    configured = ScopeEnvelope(paths=["src/**"], forbidden_paths=["src/vendor/**"])
    result, _ = establish(configured, ["src/**"], ["src/generated/**"])
    assert result.forbidden_paths == ["src/vendor/**", "src/generated/**"]


async def test_the_envelope_is_recorded_as_an_event_and_survives_a_fold(
    supervisor: Supervisor,
) -> None:
    """It has to be on the log, not only in the snapshot, to survive a resume."""
    response = await _reach_approval(supervisor)
    run_id = response.run_id

    events = supervisor.store.log(run_id).read_all()
    envelope_events = [e for e in events if e.type is EventType.ENVELOPE_SET]
    # One at creation from configuration, one when the plan narrowed it.
    assert len(envelope_events) == 2
    assert [e.payload["envelope"]["source"] for e in envelope_events] == [
        "configuration", "run plan",
    ]

    from_log = fold(events)
    assert from_log.envelope is not None
    assert from_log.envelope.paths == ["src/**", "tests/**"]
    assert from_log.envelope == supervisor.store.load_state(run_id).envelope


async def test_the_envelope_survives_a_resume(supervisor: Supervisor) -> None:
    response = await _reach_approval(supervisor)
    before = supervisor.store.load_state(response.run_id).envelope

    resumed = await supervisor.resume(response.run_id)
    after = supervisor.store.load_state(resumed.run_id).envelope

    assert before is not None
    assert after == before


async def test_status_reports_the_envelope(supervisor: Supervisor) -> None:
    response = await _reach_approval(supervisor)
    status = supervisor.status(response.run_id)

    assert status["envelope"] is not None
    assert status["envelope"]["paths"] == ["src/**", "tests/**"]
    assert status["envelope"]["source"] == "run plan"


# --------------------------------------------------------------------------
# Attenuation
# --------------------------------------------------------------------------


def test_attenuate_narrows_to_every_ceiling_and_says_which_one_bit() -> None:
    scope = Scope(paths=["src/**", "docs/**"])
    narrowed, notes = attenuate(scope, [
        Ceiling("run envelope", ["src/**", "tests/**"], []),
        Ceiling("task scope", ["src/auth/**"], []),
    ])

    assert narrowed.paths == ["src/auth/**"]
    assert len(notes) == 2
    assert "run envelope" in notes[0] and "task scope" in notes[1]
    # The input is not mutated: a caller comparing before with after must be able to.
    assert scope.paths == ["src/**", "docs/**"]


def test_an_unscoped_agent_is_narrowed_to_its_ceiling() -> None:
    """The case that matters most: empty means everything, so it must narrow."""
    narrowed, notes = attenuate(
        Scope(), [Ceiling("run envelope", ["src/**"], ["src/vendor/**"])]
    )
    assert narrowed.paths == ["src/**"]
    assert narrowed.forbidden_paths == ["src/vendor/**"]
    assert notes


def test_an_envelope_naming_a_file_inside_a_directory_takes_nothing() -> None:
    """Baseline 2: `src/`, `e2e/` "narrowed to" `src/`, `e2e/offline.spec.ts`, `e2e/`."""
    _, notes = attenuate(Scope(paths=["src/", "e2e/"]), [
        Ceiling("run envelope", ["src/", "e2e/offline.spec.ts", "e2e/", "scripts/verify.ps1"], [])])

    assert what_was_taken(notes) == []
    assert minimal_globs(["e2e/offline.spec.ts", "src/", "e2e/", "e2e/x.ts"]) == ["src/", "e2e/"]


async def test_a_task_is_not_held_for_a_fence_it_never_reached(
    supervisor: Supervisor, fake,
) -> None:
    """The run itself: a plan that forbids README.md holds none of its tasks for it."""
    plan = fake.answer_for("planning", CompletionRequest(messages=[ChatMessage("user", "")]))
    plan["envelope_paths"] = ["src/**", "tests/**", "README.md"]
    plan["envelope_forbidden_paths"] = ["README.md"]
    fake.overrides["planning"] = plan

    response = await _reach_approval(supervisor)
    state = supervisor.store.load_state(response.run_id)

    (task,) = state.tasks.values()
    assert "README.md" in task.scope.forbidden_paths, "the fence is handed down"
    assert task.clamped == [], task.clamped


def test_a_fence_handed_down_is_a_narrowing_only_where_the_task_named_it() -> None:
    """Baseline 2: every task held for the owner over README.md, which none of them named."""
    fence = Ceiling("run envelope", ["src/", "tests/", "README.md", "tools/"],
                    ["README.md", "tools/check_doc_refs.py"])

    narrowed, notes = attenuate(Scope(paths=["src/core/timing.py", "tests/", "tools/"]), [fence])
    assert narrowed.forbidden_paths == ["README.md", "tools/check_doc_refs.py"]
    assert notes and what_was_taken(notes) == [], "the fence is noted, nothing was taken"

    _, notes = attenuate(Scope(paths=["src/", "README.md"]), [fence])
    assert what_was_taken(notes) and "README.md" in what_was_taken(notes)[0]


async def test_configuration_bounds_the_run_even_when_the_plan_asks_for_more(
    supervisor: Supervisor, config, fake,
) -> None:
    """The floor under the envelope is the user's, not a model's.

    Without this the envelope's only author is the planning model, and a model
    drawing its own ceiling is the gap this work exists to close, moved one
    stage earlier rather than shut.
    """
    config.policy.scope_envelope = ["src/auth/**"]
    _plan_with_envelope(fake, ["src/**", "tests/**"])

    response = await _reach_approval(supervisor)
    state = supervisor.store.load_state(response.run_id)

    assert state.envelope is not None
    assert state.envelope.paths == ["src/auth/**"]
    assert any("does not contain" in n.text for n in state.notes)


async def test_an_analysis_lens_scoped_outside_the_envelope_is_narrowed_at_spawn(
    supervisor: Supervisor, fake,
) -> None:
    """The backstop in `_spawn`, on the one scope no other clamp touches.

    A lens scope comes straight from the planning model and passes through no
    per-task gate, so attenuation at spawn is the only thing between what the
    model proposed and the fence the agent actually runs behind.
    """
    plan = fake.answer_for("planning", CompletionRequest(messages=[ChatMessage("user", "")]))
    plan["envelope_paths"] = ["src/auth/**"]
    plan["lenses"][1]["scope_paths"] = ["src/**", "docs/**"]
    fake.overrides["planning"] = plan

    response = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    state = supervisor.store.load_state(response.run_id)

    lens = next(
        a for a in state.agents.values()
        if a.kind is AgentKind.ANALYSIS and a.role == plan["lenses"][1]["role"]
    )
    assert lens.scope.paths == ["src/auth/**"]
    assert any(
        n.actor == lens.id and "scope narrowed to the run envelope" in n.text
        for n in state.notes
    )


async def test_a_task_proposed_outside_the_envelope_runs_against_the_intersection(
    supervisor: Supervisor, fake,
) -> None:
    """The definition-of-done case, end to end and visible to the user.

    Outside the owner's own envelope too: within it, a task gets what the plan
    left out (`test_a_task_gets_what_the_plan_left_out_within_the_owners_grant`).
    """
    supervisor.config.policy.scope_envelope = ["src/**"]
    _plan_with_envelope(fake, ["src/auth/**"])

    response = await _reach_approval(supervisor)
    task = response.tasks[0]

    # Proposed as ["src/auth/**", "tests/**"]; tests/ is outside the envelope.
    assert task["scope"]["paths"] == ["src/auth/**"]
    notes = " ".join(response.task_notes[task["id"]])
    assert "scope narrowed to the run envelope" in notes
    assert "tests/**" in notes
    # And the envelope is in front of the user at the moment they decide.
    assert response.detail["envelope"]["paths"] == ["src/auth/**"]

    state = supervisor.store.load_state(response.run_id)
    assert state.tasks[task["id"]].scope.paths == ["src/auth/**"]


async def test_a_task_gets_what_the_plan_left_out_within_the_owners_grant(
    supervisor: Supervisor, fake,
) -> None:
    """The plan narrows the owner's envelope, and guesses short.

    Measured in four go-live runs: a Playwright task went to the owner because
    the plan's envelope left out `e2e/`, though the owner had granted the whole
    workspace. Within the grant the run widens, and says so; beyond it, it
    does not (`test_a_task_proposed_outside_the_envelope_runs_against_the_intersection`).
    """
    _plan_with_envelope(fake, ["src/auth/**"])

    response = await _reach_approval(supervisor)
    task = response.tasks[0]
    state = supervisor.store.load_state(response.run_id)

    assert task["scope"]["paths"] == ["src/auth/**", "tests/**"]
    assert state.envelope is not None and state.envelope.paths == ["src/auth/**", "tests/**"]
    assert state.envelope.source.endswith("widened within the owner's grant")
    assert any("widened within the owner's grant" in n.text and "tests/**" in n.text
               for n in state.notes)


async def test_approval_cannot_widen_the_envelope(supervisor: Supervisor, fake) -> None:
    """The recorded answer to the fourth question, enforced.

    A `scope_paths` modification is a decision about one task. Letting it move
    a run-level bound would make the bound only as strong as the most
    permissive task anyone approved.
    """
    supervisor.config.policy.scope_envelope = ["src/**"]
    _plan_with_envelope(fake, ["src/auth/**"])

    response = await _reach_approval(supervisor)
    task_id = response.tasks[0]["id"]

    await supervisor.approve(response.run_id, [{
        "task_id": task_id,
        "decision": "modify",
        "modifications": {"scope_paths": ["src/**", "docs/**"]},
    }])

    state = supervisor.store.load_state(response.run_id)
    assert state.tasks[task_id].scope.paths == ["src/auth/**"]
    assert state.envelope is not None
    assert state.envelope.paths == ["src/auth/**"]
    assert any("scope narrowed to the run envelope" in n.text for n in state.notes)


async def test_a_verifier_is_not_given_a_wider_scope_than_the_task_it_judges(
    supervisor: Supervisor,
) -> None:
    """Before this it was given no scope at all, which reads as the workspace.

    Two mechanisms hold this now -- the builder copies the task's paths, and
    attenuation at spawn would narrow an empty scope to the same ceiling -- so
    this test goes red only when both are removed. That is defence in depth
    working, not a vacuous test: removing either one alone leaves the property
    true, and removing both leaves it false.
    """
    await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = supervisor.store.load_state(supervisor.store.latest_run_id())

    verifiers = [a for a in state.agents.values() if a.kind is AgentKind.VERIFICATION]
    assert verifiers
    for verifier in verifiers:
        task = state.tasks[verifier.task_id]
        assert verifier.scope.paths, "an empty scope is the whole workspace"
        assert globs_within(verifier.scope.paths, task.scope.paths)
        assert globs_within(verifier.scope.paths, state.envelope.paths)


async def test_every_agent_in_a_run_is_within_the_envelope(supervisor: Supervisor) -> None:
    """The property the envelope exists to establish, over a whole run."""
    await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = supervisor.store.load_state(supervisor.store.latest_run_id())

    assert state.envelope is not None
    for agent in state.agents.values():
        assert globs_within(agent.scope.paths, state.envelope.paths), (
            f"{agent.id} ({agent.role}) is scoped to {agent.scope.paths}, "
            f"outside the run envelope {state.envelope.paths}"
        )


async def test_an_absolute_path_at_approval_means_the_same_file_relative(
    supervisor: Supervisor, fake
) -> None:
    """A scope written as an absolute path matched nothing and was clamped to it.

    Scope comparisons are against workspace-relative paths; an absolute edit
    is placed in the workspace first (`relative_patterns`), as the planner's are.
    """
    _plan_with_envelope(fake, ["src/auth/**"])
    response = await _reach_approval(supervisor)
    task_id = response.tasks[0]["id"]
    absolute = str(supervisor.workspace / "src" / "auth" / "login.py")

    await supervisor.approve(response.run_id, [{
        "task_id": task_id, "decision": "modify",
        "modifications": {"scope_paths": [absolute]},
    }])

    state = supervisor.store.load_state(response.run_id)
    assert state.tasks[task_id].scope.paths == ["src/auth/login.py"]


def test_widening_stays_inside_the_owners_grant_and_its_forbidden_paths() -> None:
    from supervisor_harness.core.envelope import Ceiling, widen_within
    from supervisor_harness.models import ScopeEnvelope

    run = ScopeEnvelope(paths=["src/auth/**"], forbidden_paths=["docs/adr/**"])
    grant = Ceiling("owner's grant", ["src/**", "e2e/**", "docs/**"], ["src/secrets/**"])

    envelope, granted, refused = widen_within(run, grant, [
        "e2e/offline.spec.ts",      # left out by the plan, inside the grant: widened
        "src/auth/login.py",        # already inside: granted, nothing to widen
        "infra/waf.tf",             # beyond the grant
        "src/secrets/key.py",       # the grant forbids it
        "docs/adr/0001.md",         # the plan forbids it
    ])

    assert granted == ["e2e/offline.spec.ts", "src/auth/login.py"]
    assert refused == ["infra/waf.tf", "src/secrets/key.py", "docs/adr/0001.md"]
    assert envelope.paths == ["src/auth/**", "e2e/offline.spec.ts"]
    assert envelope.forbidden_paths == ["docs/adr/**"], "nothing forbidden is lifted"

    whole = ScopeEnvelope(paths=[])
    assert widen_within(whole, Ceiling("grant", [], []), ["any/file.py"])[0].paths == [], (
        "an envelope of the whole workspace has nothing to widen")
