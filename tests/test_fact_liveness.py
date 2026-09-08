"""Whether a fact one agent established is still true when another reads it.

Batch 5 of `docs/development-plan.md`. A lens establishes "the counters live in
`src/cache.py`" during `analyzing`; an execution agent inherits it during
`executing`, after other agents have been writing into the same tree, and until
now the brief rendered both cases identically -- untouched, rewritten and gone.

Adapted from NOOA's memory references, which resolve to LIVE or DANGLING and
render a dangling one as the write-time snapshot *clearly stamped as such*. One
state is added, because their targets vanish and ours mostly get edited.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from supervisor_harness.config import HarnessConfig, Policy, default_config
from supervisor_harness.core.blackboard import render_context
from supervisor_harness.core.facts import (
    MAX_ANCHOR_BYTES,
    Liveness,
    anchor_fact,
    anchor_from_evidence,
    liveness,
    resolve_anchor,
)
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.host.detect import HostInfo
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    Backend,
    Budget,
    Fact,
    RunMode,
)
from supervisor_harness.store.runstore import RunStore

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A workspace with something for a fact to be about."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cache.py").write_text("counters = {}\n", encoding="utf-8")
    return tmp_path


def _fact(tree: Path, **kwargs: str) -> Fact:
    fields: dict[str, str] = {
        "key": "counter store",
        "statement": "Counters are a module-level dict",
        "evidence": "src/cache.py:1",
        "role": "security",
    }
    fields.update(kwargs)
    return anchor_fact(Fact(**fields), tree)


# -- resolving an anchor ----------------------------------------------------


def test_an_anchor_is_resolved_by_strict_path_lookup(tree: Path) -> None:
    assert resolve_anchor("src/cache.py", tree) == "src/cache.py"
    assert resolve_anchor("./src/cache.py", tree) == "src/cache.py"
    assert resolve_anchor(str(tree / "src" / "cache.py"), tree) == "src/cache.py"


def test_an_anchor_outside_the_workspace_is_not_an_anchor(tree: Path) -> None:
    """A fact naming a file outside the tree names something this run cannot see.

    Inventing a relative form for it would point the liveness check at a
    different file, which is worse than declining to check.
    """
    assert resolve_anchor("/etc/passwd", tree) == ""
    assert resolve_anchor("C:/Windows/System32/drivers/etc/hosts", tree) == ""


def test_an_anchor_is_never_evaluated(tree: Path) -> None:
    """NOOA's rule, taken verbatim because it applies verbatim.

    A fact is written by a model onto a blackboard other agents read. An anchor
    that could be evaluated would be an injection primitive with an audience, so
    it names a file and nothing else -- these are simply not paths, and become
    no anchor rather than anything that runs.
    """
    for hostile in (
        "__import__('os').system('echo pwned')",
        "{{ 7*7 }}",
        "$(cat /etc/passwd)",
        "`whoami`",
    ):
        assert resolve_anchor(hostile, tree) == "", hostile


def test_an_anchor_is_recovered_from_evidence_when_none_is_given() -> None:
    """Every brief already asks for evidence as "file:line".

    Reading it means the check works on facts produced before anchors existed,
    and on models that ignore the new field.
    """
    assert anchor_from_evidence("src/cache.py:12") == "src/cache.py"
    assert anchor_from_evidence("src/cache.py:12 shows the dict") == "src/cache.py"
    assert anchor_from_evidence("tests/test_x.py") == "tests/test_x.py"


def test_evidence_that_names_no_file_is_not_guessed_at() -> None:
    """A wrong anchor is worse than none: it checks the liveness of a file the

    claim was never about, and reports confidently either way.
    """
    assert anchor_from_evidence("the output of the test run was 100 passed") == ""
    assert anchor_from_evidence("") == ""
    assert anchor_from_evidence("no path here") == ""


def test_recovering_an_anchor_still_goes_through_containment(tree: Path) -> None:
    fact = _fact(tree, evidence="/etc/passwd:1")

    assert fact.anchor == ""


# -- liveness ---------------------------------------------------------------


def test_an_untouched_anchor_is_live(tree: Path) -> None:
    fact = _fact(tree)

    assert fact.anchor == "src/cache.py" and fact.anchor_digest
    assert liveness(fact, tree) is Liveness.LIVE


def test_an_edited_anchor_is_changed(tree: Path) -> None:
    """The case that actually happens: the file is still there and different."""
    fact = _fact(tree)

    (tree / "src" / "cache.py").write_text("import redis\n", encoding="utf-8")

    assert liveness(fact, tree) is Liveness.CHANGED


def test_a_deleted_anchor_is_gone(tree: Path) -> None:
    fact = _fact(tree)

    (tree / "src" / "cache.py").unlink()

    assert liveness(fact, tree) is Liveness.GONE


def test_a_fact_about_nothing_in_particular_is_unanchored(tree: Path) -> None:
    fact = _fact(tree, evidence="the suite reported 100 passed")

    assert fact.anchor == ""
    assert liveness(fact, tree) is Liveness.UNANCHORED


def test_a_fact_recorded_without_a_digest_is_unreadable_not_live(tree: Path) -> None:
    """The safety property. "Not checkable" must never read as "unchanged".

    A fact recorded when its anchor could not be digested -- too large, or
    unreadable at the time -- knows which file it is about and nothing about
    whether that file has moved. Defaulting to LIVE would state the one thing
    this module exists to stop stating without evidence.
    """
    fact = _fact(tree)
    fact.anchor_digest = ""

    assert liveness(fact, tree) is Liveness.UNREADABLE


def test_an_anchor_too_large_to_digest_keeps_its_anchor(tree: Path) -> None:
    big = tree / "src" / "huge.bin"
    big.write_bytes(b"x" * (MAX_ANCHOR_BYTES + 1))

    fact = _fact(tree, evidence="src/huge.bin:1")

    assert fact.anchor == "src/huge.bin", "which file it is about is still useful"
    assert fact.anchor_digest == ""
    assert liveness(fact, tree) is Liveness.UNREADABLE


# -- what a reader sees -----------------------------------------------------


def test_a_live_fact_reads_exactly_as_it_did(tree: Path) -> None:
    """No "(still true)" on every line.

    Annotating the ordinary case trains a reader to skip the line, and the line
    that says otherwise is the only one here that matters.
    """
    rendered = render_context("", {}, [_fact(tree)], workspace=tree)

    assert "Counters are a module-level dict" in rendered
    assert "**Stale:**" not in rendered, "the header explains the marker; no claim wears it"


def test_a_stale_fact_is_stamped_without_being_hidden(tree: Path) -> None:
    """NOOA's semantic: the snapshot is still shown, and labelled as one.

    Dropping the claim would lose what the writer actually found; showing it
    unmarked is what this replaced.
    """
    fact = _fact(tree)
    (tree / "src" / "cache.py").write_text("import redis\n", encoding="utf-8")

    rendered = render_context("", {}, [fact], workspace=tree)

    assert "Counters are a module-level dict" in rendered, "the claim is not dropped"
    assert "**Stale:**" in rendered
    assert "has been edited since" in rendered
    assert "src/cache.py" in rendered


def test_a_missing_file_says_so_differently_from_an_edited_one(tree: Path) -> None:
    fact = _fact(tree)
    (tree / "src" / "cache.py").unlink()

    rendered = render_context("", {}, [fact], workspace=tree)

    assert "no longer exists" in rendered


def test_without_a_workspace_no_liveness_claim_is_made(tree: Path) -> None:
    """Omitted means "not checked", not "nothing has changed".

    A caller with no workspace cannot tell those apart and must not imply one.
    """
    fact = _fact(tree)
    (tree / "src" / "cache.py").unlink()

    assert "**Stale:**" not in render_context("", {}, [fact])


def test_contested_claims_are_stamped_one_by_one(tree: Path) -> None:
    """Two agents disagreeing about a file one of them has since seen change.

    The disagreement and the staleness are different facts about the same key,
    and collapsing either into the other loses the one a reader needs.
    """
    fresh = _fact(tree, statement="Counters are a module-level dict")
    (tree / "src" / "cache.py").write_text("import redis\n", encoding="utf-8")
    later = _fact(tree, statement="Counters are in Redis")

    rendered = render_context("", {}, [fresh, later], workspace=tree)

    assert "agents disagree" in rendered
    assert rendered.count("**Stale:**") == 1, "only the claim whose file moved"
    assert "Counters are in Redis" in rendered


# -- end to end -------------------------------------------------------------


async def test_a_fact_goes_stale_between_the_agent_that_wrote_it_and_the_next(
    tree: Path,
) -> None:
    """The whole point, through the real path.

    The lens records a fact and finishes. Another agent rewrites the file, as
    they do -- the run shares one tree. An agent briefed *after* that sees the
    claim marked as a snapshot rather than as the state of the tree.

    The agent briefed *before* it does not, and that is deliberate: a brief is
    rendered once and reused because it is the fixed text drift is scored
    against, and rewriting it mid-run would move the ruler while measuring. The
    readers this protects -- an executor, a verifier -- are briefed later by
    construction, which is the case asserted here.
    """
    config: HarnessConfig = default_config()
    config.backend = Backend.HOST
    config.routing = {k: "host" for k in config.routing}
    config.policy = Policy(default_max_turns=3, max_analysis_lenses=2)
    supervisor = Supervisor(
        workspace=tree, config=config, store=RunStore(tree / ".supervisor"),
        host=HostInfo(name="claude-code", workspace=str(tree), confidence=1.0),
    )

    started = await supervisor.start(PROMPT, mode=RunMode.EXECUTE)
    plan = started.packets[0]
    await supervisor.report(plan.run_id, plan.agent_id, {
        "restated_goal": "rate limit login", "mode": "execute",
        "lenses": [{"role": "security", "why": "exposure", "objectives": ["Find it"]}],
    })
    analysis = await supervisor.advance(started.run_id)
    lens = analysis.packets[0]
    await supervisor.report(lens.run_id, lens.agent_id, {
        "output": "The counters are a module-level dict in src/cache.py.",
        "established": [{
            "key": "counter store",
            "statement": "Counters are a module-level dict in cache.py",
            "evidence": "src/cache.py:1",
            "anchor": "src/cache.py",
        }],
        "files_examined": ["src/cache.py"],
        "status": "done",
    })

    state = supervisor.store.load_state(started.run_id)
    assert [f.anchor for f in state.established] == ["src/cache.py"]
    assert supervisor.reporting.status(started.run_id)["established"][0]["liveness"] == "live"

    # A peer rewrites the file the claim is about.
    (tree / "src" / "cache.py").write_text(
        "import redis\nCLIENT = redis.Redis()\n", encoding="utf-8"
    )

    assert supervisor.reporting.status(started.run_id)["established"][0]["liveness"] == "changed"

    # The next agent to be briefed inherits the claim, stamped.
    session = supervisor.store.open(started.run_id)
    later = AgentSpec(
        run_id=started.run_id, role="implementer", kind=AgentKind.EXECUTION,
        title="Add the limiter", brief="implement it",
        objectives=["Add a per-IP counter"], budget=Budget(max_turns=3),
    )
    session.state.agents[later.id] = later
    packet = supervisor.packets._agent_packet(session, later)

    brief = packet.read_brief()
    assert "Counters are a module-level dict" in brief, "the claim is still carried"
    assert "**Stale:**" in brief
    assert "has been edited since" in brief
