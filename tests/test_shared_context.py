"""What one agent establishes becomes part of what the run knows.

The control-plane assessment called this dimension "partial", on the grounds
that the shared context was free text and nothing checked that two agents meant
the same thing by a term. Both true, and both downstream of the real gap:
`state.facts` was written in exactly two places, both of them the harness's own
keys, and no schema anywhere let an agent contribute one. Two agents could not
disagree about a term because neither could state one.

`docs/shared-context-spec.md` is the design this implements, including the three
choices that were open when it was written.
"""

from __future__ import annotations

from supervisor_harness.contracts import parse_established
from supervisor_harness.core.blackboard import (
    answer_from_record,
    contested_keys,
    normalise_fact_key,
    render_context,
)
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    Fact,
    Message,
    MessageKind,
    RunMode,
    RunState,
)
from supervisor_harness.store.events import Event, EventType, fold

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"


async def _completed(supervisor: Supervisor):
    response = await supervisor.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    return supervisor.store.load_state(response.run_id)


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------


def test_a_key_is_normalised_on_case_and_separators_only() -> None:
    """The decision recorded in the spec, and the line it deliberately holds."""
    same = {"counter store", "Counter-Store", "counter_store", "  COUNTER   STORE  ",
            "counter/store"}
    assert len({normalise_fact_key(k) for k in same}) == 1

    # And what it deliberately does not merge. `jaccard` is right there in
    # drift.py and is not used: merging two facts that were never the same
    # destroys a distinction silently, and a run cannot tell from the inside
    # that it has done it. Fragmentation is visible; a bad merge is not.
    assert normalise_fact_key("counter store") != normalise_fact_key("counters")
    assert normalise_fact_key("login entrypoint") != normalise_fact_key("login")


# --------------------------------------------------------------------------
# Establishing
# --------------------------------------------------------------------------


def test_a_claim_with_no_evidence_is_dropped() -> None:
    """The rule every brief already states, applied where it is now checkable.

    `_report_verification` already records a criterion marked passed with no
    evidence as failed. A fact asserted with nothing behind it is the same
    thing: carrying it forward weakly would leave everything downstream
    guessing which established facts it can trust.
    """
    agent = AgentSpec(id="agt_1", role="technical")
    parsed = parse_established(
        {"established": [
            {"key": "good", "statement": "s", "evidence": "src/x.py:1"},
            {"key": "no evidence", "statement": "trust me", "evidence": ""},
            {"key": "", "statement": "unkeyed", "evidence": "src/x.py:1"},
            {"key": "no statement", "statement": "", "evidence": "src/x.py:1"},
            "not even a dict",
        ]},
        agent,
    )
    assert [f.key for f in parsed] == ["good"]
    assert parsed[0].agent_id == "agt_1" and parsed[0].role == "technical"


async def test_only_analysis_agents_establish_facts(supervisor: Supervisor) -> None:
    """The first of the spec's open choices, decided and enforced.

    An execution agent reports what it changed, which its turn and its findings
    already carry. A verifier writing into the record it judges against is the
    conflict of interest batch 7 was about, wearing different clothes.
    """
    state = await _completed(supervisor)
    assert state.established, "no facts were established at all"

    by_agent = {f.agent_id for f in state.established}
    kinds = {state.agents[aid].kind for aid in by_agent if aid in state.agents}
    assert kinds == {AgentKind.ANALYSIS}


async def test_an_established_fact_reaches_a_later_agents_brief(
    supervisor: Supervisor,
) -> None:
    """The point of the whole exercise.

    Not a *peer's* brief: analysis lenses are briefed together, and a brief is
    rendered once as a stable anchor for drift scoring. The agent that inherits
    is the one spawned afterwards, which is the execution agent.
    """
    state = await _completed(supervisor)
    execution = [a for a in state.agents.values() if a.kind is AgentKind.EXECUTION]
    assert execution

    brief = state.briefs[execution[0].id]
    assert "Established by other agents in this run" in brief
    assert "counter store" in brief
    assert "src/cache.py:8" in brief, "the evidence travels with the claim"
    # And it is offered as evidence, not as law.
    assert "not a rule" in brief


# --------------------------------------------------------------------------
# Disagreement
# --------------------------------------------------------------------------


def test_two_claims_on_one_key_are_both_kept() -> None:
    """The fold used to be `facts[key] = value`: last writer wins, silently."""
    facts = [
        Fact(key="counter store", statement="Redis", role="technical"),
        Fact(key="counter store", statement="an in-process dict", role="security"),
    ]
    contested = contested_keys(facts)
    assert list(contested) == ["counter store"]
    assert len(contested["counter store"]) == 2


def test_the_same_claim_twice_is_not_a_disagreement() -> None:
    """Case and whitespace are set aside; anything else is a difference."""
    same = [
        Fact(key="k", statement="The limiter keys on account", role="a"),
        Fact(key="k", statement="the  limiter keys on   account", role="b"),
    ]
    assert contested_keys(same) == {}

    # A rephrasing does read as a disagreement. That is the safe direction and
    # it is a guard on the choice, not a claim that the two differ in substance:
    # a contested key costs a line in a brief and asks the supervisor to look,
    # while a missed contest silently picks a winner.
    rephrased = [
        Fact(key="k", statement="The limiter keys on account", role="a"),
        Fact(key="k", statement="Account is the limiter's key", role="b"),
    ]
    assert list(contested_keys(rephrased)) == ["k"]


async def test_a_contested_key_is_visible_everywhere_it_matters(
    supervisor: Supervisor,
) -> None:
    """The two fake lenses key `login entrypoint` differently on purpose."""
    state = await _completed(supervisor)

    contested = contested_keys(state.established)
    assert "login entrypoint" in contested

    # In the report's conflicts, beside the ones inferred from findings.
    assert any("disagree about login entrypoint" in c for c in state.report.conflicts)

    # In status.
    status = supervisor.status(state.id)
    assert "login entrypoint" in status["contested_facts"]
    assert len(status["established"]) == len(state.established)

    # And in the brief of the agent that inherits it, marked as open.
    execution = [a for a in state.agents.values() if a.kind is AgentKind.EXECUTION]
    brief = state.briefs[execution[0].id]
    assert "agents disagree, treat as open" in brief


def test_the_synthesis_model_cannot_suppress_a_disagreement() -> None:
    """`build_report` keeps model-supplied conflicts *and* appends contested keys.

    The branch that computes conflicts from findings only runs when the model
    offered none. Contested facts are appended either way, because a model that
    did not notice two agents contradicting each other is exactly the case that
    needs the harness to say so.
    """
    from supervisor_harness.core.phases import build_report

    state = RunState(id="run_A", prompt="p")
    state.established = [
        Fact(key="store", statement="Redis", role="technical"),
        Fact(key="store", statement="Postgres", role="data"),
    ]
    report = build_report(state, {"summary": "s", "conflicts": ["something else"]})

    assert "something else" in report.conflicts
    assert any("disagree about store" in c for c in report.conflicts)


# --------------------------------------------------------------------------
# Reaching the record
# --------------------------------------------------------------------------


def test_the_supervisor_answers_a_question_from_an_established_fact() -> None:
    """Before this, the record had almost nothing to answer from.

    `answer_from_record` reads the brief, the scope, `state.facts` and the
    findings. Two of those four were the baseline commit and the planner's
    restatement, so a question that matched neither fell through to the findings
    or to nothing at all.
    """
    state = RunState(id="run_A", prompt="p")
    state.established = [
        Fact(key="counter store", statement="the Redis client in src/cache.py",
             evidence="src/cache.py:8", role="technical"),
    ]
    agent = AgentSpec(id="agt_2", role="security")
    question = Message(sender="agt_2", recipient="supervisor", kind=MessageKind.QUESTION,
                       subject="Which counter store should I use?", content="")

    answers = answer_from_record(question, agent, state)
    assert any("counter store" in a and "Redis" in a for a in answers)


def test_a_question_about_a_contested_key_is_answered_with_the_disagreement() -> None:
    """Handing back one side would be the silent pick, one layer up."""
    state = RunState(id="run_A", prompt="p")
    state.established = [
        Fact(key="counter store", statement="Redis", role="technical"),
        Fact(key="counter store", statement="an in-process dict", role="security"),
    ]
    question = Message(sender="agt_3", recipient="supervisor", kind=MessageKind.QUESTION,
                       subject="What is the counter store?", content="")

    answers = answer_from_record(question, AgentSpec(id="agt_3"), state)
    joined = " ".join(answers)
    assert "disagree" in joined
    assert "Redis" in joined and "in-process dict" in joined


# --------------------------------------------------------------------------
# Open questions, and the log
# --------------------------------------------------------------------------


async def test_an_agents_open_questions_are_no_longer_discarded(
    supervisor: Supervisor,
) -> None:
    """Asked for since the contract was written, and read by nothing.

    `contracts.py` requested `open_questions` from every analysis agent,
    `AgentTurn` had no field for it, `supervisor.py` never mentioned it, and
    `build_report` read the key only from the *synthesis* payload. Every
    analysis turn spent tokens on it and the harness dropped it on arrival.
    """
    state = await _completed(supervisor)

    asked = [q for turn in state.turns for q in turn.open_questions]
    assert "Is there a WAF in front of this?" in asked
    assert "Is there a WAF in front of this?" in supervisor.status(state.id)["open_questions"]


def test_established_facts_survive_a_replay_and_are_not_doubled() -> None:
    """Event-sourced like everything else, and idempotent under a re-fold."""
    from supervisor_harness.serde import to_jsonable

    fact = Fact(key="store", statement="Redis", evidence="src/cache.py:8", agent_id="a")
    events = [
        Event(seq=1, type=EventType.FACT_ESTABLISHED, payload={"fact": to_jsonable(fact)}),
        Event(seq=2, type=EventType.FACT_ESTABLISHED, payload={"fact": to_jsonable(fact)}),
    ]
    assert len(fold(events).established) == 1

    other = Fact(key="store", statement="Redis", evidence="src/cache.py:8", agent_id="b")
    events.append(
        Event(seq=3, type=EventType.FACT_ESTABLISHED, payload={"fact": to_jsonable(other)})
    )
    assert len(fold(events).established) == 2, "a second agent agreeing is still a record"


async def test_a_run_folds_back_to_the_same_established_record(
    supervisor: Supervisor,
) -> None:
    state = await _completed(supervisor)
    replayed = fold(supervisor.store.log(state.id).read_all())

    assert [(f.key, f.statement, f.agent_id) for f in replayed.established] == [
        (f.key, f.statement, f.agent_id) for f in state.established
    ]


def test_the_harness_facts_and_the_agents_facts_are_rendered_apart() -> None:
    """They differ in provenance, in trust, and in whether conflict is possible.

    `facts` holds the baseline commit and the planner's restatement: no author,
    no evidence, nothing to contest. An agent's claim has all three, and a
    reader inheriting one should be able to see whose it is and what backs it.
    """
    rendered = render_context(
        "Python service.",
        {"baseline commit": "abc123"},
        [Fact(key="store", statement="Redis", evidence="src/cache.py:8", role="technical")],
    )
    assert "Established facts:" in rendered
    assert "baseline commit: abc123" in rendered
    assert "Established by other agents in this run" in rendered
    assert "(technical, evidence: src/cache.py:8)" in rendered
