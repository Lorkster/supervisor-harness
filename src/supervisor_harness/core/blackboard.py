"""The blackboard: shared context and supervised agent-to-agent messaging.

Agents never talk to each other directly. Every message passes through the
supervisor, which is what makes immediate course correction possible: the
supervisor sees a contradiction between two agents as it is raised, can annotate
or suppress a message, and can turn a warning from one agent into a directive
for another before the recipient wastes a turn.

Delivery is pull-based and tied to turns -- an agent receives its inbox in the
directive that starts its next turn -- so ordering stays deterministic and the
whole conversation replays from the event log.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from ..models import (
    BROADCAST,
    SUPERVISOR,
    AgentSpec,
    Fact,
    Finding,
    Message,
    MessageKind,
    RunState,
    Severity,
)
from .drift import tokens

# Messages the supervisor should look at rather than pass through untouched.
_ESCALATING_KINDS = frozenset({MessageKind.CONTRADICTION, MessageKind.WARNING})


@dataclass
class Routing:
    """What the supervisor decided to do with one message."""

    message: Message
    deliver_to: list[str] = field(default_factory=list)
    escalate: bool = False
    note: str = ""


_SEPARATORS = re.compile(r"[\s_\-/]+")


def normalise_fact_key(raw: str) -> str:
    """A fact key in the one form two agents' claims are compared on.

    Case and separators only. ``Redis``/``redis`` and ``counter store``/
    ``counter-store`` are the same key; ``counter store`` and ``counters`` are
    not, and are deliberately left apart.

    The tempting next step is a similarity merge -- ``jaccard`` is right there
    in `drift.py`. It is not taken, because merging two facts that were never
    the same is much worse than leaving two spellings of one fact apart: the
    first silently destroys a distinction the run may depend on, and a run
    cannot tell from the inside that it has done it. Fragmentation is visible;
    a bad merge is not.
    """
    return _SEPARATORS.sub(" ", str(raw).strip().lower()).strip()


def contested_keys(established: list[Fact]) -> dict[str, list[Fact]]:
    """Keys carrying more than one distinct claim, and the claims.

    Two statements count as one claim only when they are identical once
    whitespace and case are set aside. A rephrasing therefore reads as a
    disagreement, which is the safe direction: a contested key costs a line in a
    brief and asks the supervisor to look, while a missed contest silently picks
    a winner -- and picking silently is what this replaced.
    """
    by_key: dict[str, list[Fact]] = {}
    for fact in established:
        by_key.setdefault(fact.key, []).append(fact)
    return {
        key: facts for key, facts in by_key.items()
        if len({" ".join(f.statement.lower().split()) for f in facts}) > 1
    }


def render_context(
    shared_context: str,
    facts: dict[str, str],
    established: list[Fact] | None = None,
) -> str:
    """Format the run's shared context for inclusion in a brief.

    ``facts`` is what the harness knows and ``established`` is what the run's
    agents have found, and the two are rendered apart on purpose. The first
    needs no evidence and cannot be wrong about itself; the second is one
    agent's reading, and an agent inheriting it should be able to see whose, on
    what, and whether anyone disagreed.
    """
    parts = [shared_context] if shared_context else []
    if facts:
        parts.append(
            "Established facts:\n"
            + "\n".join(f"- {k}: {v}" for k, v in sorted(facts.items()))
        )

    established = established or []
    if established:
        contested = contested_keys(established)
        lines: list[str] = []
        for key in sorted({f.key for f in established}):
            claims = [f for f in established if f.key == key]
            if key in contested:
                lines.append(f"- **{key}** -- agents disagree, treat as open:")
                lines.extend(
                    f"    - {c.statement} ({c.role or c.agent_id}"
                    + (f", evidence: {c.evidence}" if c.evidence else "") + ")"
                    for c in claims
                )
            else:
                first = claims[0]
                lines.append(
                    f"- {key}: {first.statement} ({first.role or first.agent_id}"
                    + (f", evidence: {first.evidence}" if first.evidence else "") + ")"
                )
        parts.append(
            "Established by other agents in this run. Each is one agent's "
            "reading, not a rule: check it against what you can see, and say so "
            "if you find otherwise.\n" + "\n".join(lines)
        )
    return "\n\n".join(parts)


class Blackboard:
    """Shared facts plus the message bus for one run."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id

    # -- routing -----------------------------------------------------------

    def route(self, message: Message, state: RunState) -> Routing:
        """Decide who receives a message, and whether the supervisor must act.

        Unknown recipients are broadened to a broadcast rather than dropped: a
        model that invents an agent id has still noticed something real, and
        silently discarding it is the worse failure.

        The decision is written onto the message, not just returned: only the
        message survives the fold, and :meth:`RunState.pending_messages` reads
        delivery back off ``recipient``. A broadening left in ``deliver_to``
        alone would be discarded the moment the event is appended.
        """
        known = set(state.agents)
        recipient = message.recipient.strip() or BROADCAST
        message.recipient = recipient

        if recipient == SUPERVISOR:
            return Routing(message=message, deliver_to=[], escalate=True,
                           note="addressed to the supervisor")

        if recipient == BROADCAST:
            targets = [a for a in known if a != message.sender]
        elif recipient in known:
            targets = [recipient]
        else:
            targets = [a for a in known if a != message.sender]
            message.supervisor_note = (
                f"originally addressed to unknown agent {recipient!r}; broadcast instead"
            )
            message.recipient = BROADCAST

        escalate = message.kind in _ESCALATING_KINDS
        return Routing(
            message=message,
            deliver_to=targets,
            escalate=escalate,
            note="flagged for supervisor review" if escalate else "",
        )

    # -- inbox -------------------------------------------------------------

    @staticmethod
    def inbox_for(agent_id: str, state: RunState, limit: int = 12) -> list[Message]:
        """Messages addressed to this agent that it has not yet received."""
        return state.pending_messages(agent_id)[-limit:]

    @staticmethod
    def supervisor_inbox(state: RunState) -> list[Message]:
        """Messages the supervisor has not yet acted on."""
        return [
            m for m in state.messages
            if not m.delivered_for(SUPERVISOR)
            and (m.recipient == SUPERVISOR or m.kind in _ESCALATING_KINDS)
        ]

    @staticmethod
    def questions_for_supervisor(agent_id: str, state: RunState) -> list[Message]:
        """This agent's unanswered questions to the supervisor."""
        return [
            m for m in Blackboard.supervisor_inbox(state)
            if m.sender == agent_id and m.recipient == SUPERVISOR
        ]


def _from_the_brief(agent: AgentSpec, relevant: Callable[[str], bool]) -> list[str]:
    """What the agent was already told to do."""
    out: list[str] = []
    for objective in agent.objectives:
        if relevant(objective):
            out.append(f"Your brief already says: {objective}")
    if agent.scope.paths and relevant(" ".join(agent.scope.paths)):
        out.append(f"Your scope is: {', '.join(agent.scope.paths)}")
    if agent.scope.out_of_scope and relevant(" ".join(agent.scope.out_of_scope)):
        out.append(f"Explicitly out of scope: {', '.join(agent.scope.out_of_scope)}")
    return out


def _from_harness_facts(state: RunState, relevant: Callable[[str], bool]) -> list[str]:
    """What the harness itself established: the baseline, the restatement."""
    out: list[str] = []
    for key, value in state.facts.items():
        if relevant(f"{key} {value}"):
            out.append(f"Established for this run -- {key}: {value}")
    return out


def _from_agents_facts(state: RunState, relevant: Callable[[str], bool]) -> list[str]:
    """What other agents established, including where two of them disagree.

    Most of what the record can usefully answer with. Before agents could
    establish anything, a question that matched neither the harness's own
    facts nor a finding fell through to nothing."""
    out: list[str] = []
    contested = contested_keys(state.established)
    for key in sorted({f.key for f in state.established}):
        claims = [f for f in state.established if f.key == key]
        if not relevant(key + " " + " ".join(c.statement for c in claims)):
            continue
        if key in contested:
            out.append(
                f"Agents disagree about {key} -- "
                + "; ".join(f"{c.role or c.agent_id} says {c.statement}" for c in claims)
            )
        else:
            first = claims[0]
            out.append(
                f"{first.role or first.agent_id} established -- {key}: {first.statement}"
            )
    return out


def _from_the_task(
    agent: AgentSpec, state: RunState, relevant: Callable[[str], bool]
) -> list[str]:
    """What this agent's own definition of done demands."""
    out: list[str] = []
    task = state.tasks.get(agent.task_id or "")
    if task is not None:
        for crit in task.dod:
            if relevant(crit.statement):
                out.append(f"The definition of done requires: {crit.statement}")
    return out


def _from_other_findings(
    agent: AgentSpec, state: RunState, relevant: Callable[[str], bool]
) -> list[str]:
    """What another agent has already reported."""
    out: list[str] = []
    for finding in state.findings:
        if finding.agent_id != agent.id and relevant(f"{finding.title} {finding.detail}"):
            out.append(f"Another agent already found: {finding.title}")
    return out


def answer_from_record(question: Message, agent: AgentSpec, state: RunState) -> list[str]:
    """What the run's own record says that bears on this question.

    The supervisor is authoritative about the *run* and ignorant about the
    world. It knows the brief it wrote, the scope it drew, the definition of
    done it approved, the facts the run established and what every other agent
    has found; it does not know the codebase, and it is not a second analyst.

    So it answers by handing back the part of that record which bears on the
    question, rather than by composing a reply. An answer it cannot source is
    not invented -- the caller says plainly that the record does not cover it,
    which is worth more to an agent than a confident guess and is the reason
    this does not need a model call.

    Returns the relevant excerpts, or an empty list when nothing matches.
    """
    asked = tokens(f"{question.subject} {question.content}")
    if not asked:
        return []

    def relevant(text: str) -> bool:
        return bool(asked & tokens(text))

    # One function per source the record can answer from. This was a single
    # body with a cyclomatic complexity of 17 (finding Q-A2); each source is
    # an independent search, and the cap applies to the answer as a whole.
    out: list[str] = [
        *_from_the_brief(agent, relevant),
        *_from_harness_facts(state, relevant),
        *_from_agents_facts(state, relevant),
        *_from_the_task(agent, state, relevant),
        *_from_other_findings(agent, state, relevant),
    ]
    return out[:6]


# --------------------------------------------------------------------------
# Cross-agent analysis
# --------------------------------------------------------------------------


def detect_contradictions(findings: list[Finding], threshold: float = 0.55) -> list[str]:
    """Find pairs of high-confidence findings from different lenses that clash.

    Deliberately conservative: it flags candidates for the supervisor to judge
    rather than asserting a contradiction on its own. The signal is two lenses
    reaching opposite polarity about substantially the same subject.
    """
    from .drift import jaccard, tokens  # local import avoids a cycle at module load

    negators = {"not", "no", "never", "missing", "lacks", "lacking", "absent",
                "without", "fails", "failing", "isn't", "aren't", "doesn't", "cannot"}
    negative_terms = {"unsafe", "insecure", "broken", "vulnerable", "incorrect",
                      "unvalidated", "unprotected", "unhandled", "uncovered"}
    positive_terms = {"safe", "secure", "correct", "present", "handled", "validated",
                      "protected", "covered", "works", "passes", "adequate", "enforced"}

    def polarity(text: str) -> int:
        """Signed sentiment about the subject, with negation taken into account.

        Counting bare keywords is not enough: "the cookie is not secure" contains
        the word "secure" and would otherwise read as a positive claim, which is
        precisely the case this function exists to catch. A positive term within
        two words after a negator is counted as negative instead.
        """
        words = re.findall(r"[a-z']+", text.lower())
        score = 0
        for i, word in enumerate(words):
            window = words[max(0, i - 2):i]
            negated = any(w in negators for w in window)
            if word in positive_terms:
                score += -1 if negated else 1
            elif word in negative_terms:
                score += 1 if negated else -1
            elif word in negators and word in ("missing", "lacks", "lacking", "fails", "failing"):
                score -= 1
        return score

    out: list[str] = []
    for i, a in enumerate(findings):
        for b in findings[i + 1:]:
            if a.lens == b.lens:
                continue
            if a.confidence < threshold or b.confidence < threshold:
                continue
            subject_a = tokens(f"{a.title} {a.detail}")
            subject_b = tokens(f"{b.title} {b.detail}")
            if jaccard(subject_a, subject_b) < 0.35:
                continue
            pa, pb = polarity(f"{a.title} {a.detail}"), polarity(f"{b.title} {b.detail}")
            if pa * pb < 0:
                out.append(
                    f"`{a.lens}` says {a.title!r} while `{b.lens}` says {b.title!r} "
                    f"about the same subject"
                )
    return out


def rank_findings(findings: list[Finding]) -> list[Finding]:
    """Order findings by how much they should influence what happens next."""
    from ..models import SEVERITY_ORDER

    return sorted(
        findings,
        key=lambda f: (SEVERITY_ORDER.get(f.severity, 0), f.confidence),
        reverse=True,
    )


def critical_findings(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity in (Severity.HIGH, Severity.CRITICAL)]
