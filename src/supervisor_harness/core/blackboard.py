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
from dataclasses import dataclass, field

from ..models import (
    BROADCAST,
    SUPERVISOR,
    AgentSpec,
    Finding,
    Message,
    MessageKind,
    RunState,
    Severity,
)

# Messages the supervisor should look at rather than pass through untouched.
_ESCALATING_KINDS = frozenset({MessageKind.CONTRADICTION, MessageKind.WARNING})


@dataclass
class Routing:
    """What the supervisor decided to do with one message."""

    message: Message
    deliver_to: list[str] = field(default_factory=list)
    escalate: bool = False
    note: str = ""


class Blackboard:
    """Shared facts plus the message bus for one run."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.shared_context: str = ""
        self.facts: dict[str, str] = {}

    # -- shared context ----------------------------------------------------

    def set_context(self, text: str) -> None:
        self.shared_context = text.strip()

    def record_fact(self, key: str, value: str) -> None:
        """A fact every agent may rely on, established once."""
        self.facts[key] = value.strip()

    def context_for(self, agent: AgentSpec) -> str:
        """The shared context as one agent should see it."""
        parts = [self.shared_context] if self.shared_context else []
        if self.facts:
            parts.append(
                "Established facts:\n"
                + "\n".join(f"- {k}: {v}" for k, v in sorted(self.facts.items()))
            )
        return "\n\n".join(parts)

    # -- routing -----------------------------------------------------------

    def route(self, message: Message, state: RunState) -> Routing:
        """Decide who receives a message, and whether the supervisor must act.

        Unknown recipients are broadened to a broadcast rather than dropped: a
        model that invents an agent id has still noticed something real, and
        silently discarding it is the worse failure.
        """
        known = set(state.agents)
        recipient = message.recipient.strip() or BROADCAST

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
        """Undelivered messages addressed to this agent, oldest first."""
        pending = [
            m for m in state.messages
            if not m.delivered
            and m.sender != agent_id
            and m.recipient in (agent_id, BROADCAST)
        ]
        return pending[-limit:]

    @staticmethod
    def supervisor_inbox(state: RunState) -> list[Message]:
        return [
            m for m in state.messages
            if not m.delivered
            and (m.recipient == SUPERVISOR or m.kind in _ESCALATING_KINDS)
        ]


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
