"""Built-in roles: the analysis lenses and the execution specialities.

A role is a reusable charter -- what this agent is for, what it must look at,
and crucially what it must *not* wander into. The out-of-scope list is not
decoration: the drift heuristics read it directly.

Which lenses apply to a given task is decided by :func:`select_lenses`, so a
copy-editing request does not get a database-migration review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import AgentKind


@dataclass
class Role:
    """A briefable speciality."""

    id: str
    title: str
    kind: AgentKind
    summary: str
    charter: str
    objectives: list[str] = field(default_factory=list)
    focus_questions: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    # Relevance floor before keyword evidence. Only the near-universal lenses
    # (architecture, technical) sit at 1.0; specialists must earn their place.
    base_weight: float = 0.0
    host_agent_hints: list[str] = field(default_factory=list)

    @property
    def stage(self) -> str:
        prefix = {
            AgentKind.ANALYSIS: "analysis",
            AgentKind.EXECUTION: "execution",
            AgentKind.VERIFICATION: "verification",
            AgentKind.SYNTHESIS: "synthesis",
        }[self.kind]
        return f"{prefix}.{self.id}"


# --------------------------------------------------------------------------
# Analysis lenses
# --------------------------------------------------------------------------

ANALYSIS_ROLES: list[Role] = [
    Role(
        id="architecture",
        title="Architecture",
        kind=AgentKind.ANALYSIS,
        summary="Structure, boundaries, coupling and the shape of the change.",
        charter=(
            "Assess how this task fits the system's structure. Identify the components "
            "and boundaries it touches, the coupling it introduces or removes, and "
            "whether the proposed shape will still hold once the obvious next two "
            "requirements arrive. Name concrete alternatives and say which you would pick."
        ),
        objectives=[
            "Map the components, boundaries and data flows the task touches",
            "Identify structural risks: coupling, hidden dependencies, layering violations",
            "Propose the shape of the change and at least one credible alternative",
        ],
        focus_questions=[
            "What existing abstractions should this reuse rather than duplicate?",
            "Which boundary should own this behaviour, and why that one?",
            "What breaks if this needs to scale or be replaced later?",
        ],
        out_of_scope=["line-level style critique", "writing the implementation", "unrelated subsystems"],
        keywords=["design", "architecture", "refactor", "structure", "system", "service",
                  "module", "integration", "migrate", "scale", "pattern", "rewrite",
                  "boundary", "coupling", "abstraction", "decompose"],
        base_weight=1.0,
        host_agent_hints=["Plan", "architect", "general-purpose"],
    ),
    Role(
        id="security",
        title="Security",
        kind=AgentKind.ANALYSIS,
        summary="Trust boundaries, authn/authz, input handling, secrets and data exposure.",
        charter=(
            "Find the ways this task could be exploited or could leak data. Work from "
            "trust boundaries inward: who can reach this, what they control, and what "
            "they gain. Report only issues you can tie to a concrete attack path or a "
            "concrete exposure -- not generic hardening advice."
        ),
        objectives=[
            "Identify trust boundaries and the untrusted input crossing them",
            "Check authentication, authorisation and the failure modes of both",
            "Check secret handling, data exposure, injection paths and dependency risk",
        ],
        focus_questions=[
            "What is the attack path, concretely, from an untrusted input to impact?",
            "What happens on the failure path -- does it fail closed?",
            "Is any secret, token or personal data logged, cached or returned?",
        ],
        out_of_scope=["performance tuning", "code style", "speculative threats with no reachable path"],
        keywords=["auth", "authentication", "authorization", "login", "password", "token",
                  "secret", "credential", "encrypt", "permission", "session", "api", "endpoint",
                  "user", "input", "upload", "sql", "injection", "cors", "cookie", "oauth",
                  "security", "vulnerability", "payment", "pii", "personal data"],
        base_weight=0.45,
        host_agent_hints=["security", "security-review", "general-purpose"],
    ),
    Role(
        id="technical",
        title="Technical feasibility",
        kind=AgentKind.ANALYSIS,
        summary="Implementation reality: complexity, edge cases, unknowns, effort.",
        charter=(
            "Work out what actually has to change and where it will hurt. Read the "
            "relevant code before asserting anything about it. Surface the edge cases "
            "and unknowns that would make an estimate wrong, and be explicit about what "
            "you could not determine."
        ),
        objectives=[
            "Determine concretely which files, functions and interfaces must change",
            "Enumerate edge cases, failure modes and backwards-compatibility concerns",
            "State the unknowns that materially affect effort or approach",
        ],
        focus_questions=[
            "What does the existing code actually do here, as opposed to what it claims?",
            "Which edge case is most likely to be missed by a straightforward implementation?",
            "What must be true for this to work, that nobody has verified yet?",
        ],
        out_of_scope=["business justification", "visual design", "rewriting unrelated code"],
        keywords=["implement", "fix", "bug", "build", "add", "change", "update", "support",
                  "integrate", "upgrade", "port", "feature", "api", "library", "dependency"],
        base_weight=1.0,
        host_agent_hints=["Explore", "general-purpose"],
    ),
    Role(
        id="quality",
        title="Code quality and testability",
        kind=AgentKind.ANALYSIS,
        summary="Maintainability, test coverage, observability and review burden.",
        charter=(
            "Judge whether the change can be verified and lived with. Identify what "
            "tests must exist for this to be provably correct, which existing tests "
            "will need to change, and where the code would become hard to reason about."
        ),
        objectives=[
            "Define what test coverage would prove this task correct",
            "Identify maintainability risks: duplication, unclear naming, oversized units",
            "Check that failures would be observable in logs, metrics or errors",
        ],
        focus_questions=[
            "What is the smallest test that would fail if this were implemented wrongly?",
            "Which existing tests will this change break, and is that breakage correct?",
            "How would someone diagnose this in production at 3am?",
        ],
        out_of_scope=["architecture redesign", "security analysis", "cosmetic preferences"],
        keywords=["test", "coverage", "quality", "refactor", "maintain", "lint", "ci",
                  "regression", "debug", "logging", "observability", "monitor"],
        base_weight=0.6,
        host_agent_hints=["code-review", "general-purpose"],
    ),
    Role(
        id="data",
        title="Data and persistence",
        kind=AgentKind.ANALYSIS,
        summary="Schema, migrations, integrity, retention and personal data.",
        charter=(
            "Examine what this task does to stored data. Cover schema changes and their "
            "migration path, integrity constraints, and the lifecycle of any personal "
            "data involved. A migration without a rollback story is a finding."
        ),
        objectives=[
            "Identify schema or storage changes and their migration and rollback path",
            "Check integrity constraints, indexes and query patterns affected",
            "Flag personal or regulated data, its retention and its access path",
        ],
        focus_questions=[
            "Can this migration be rolled back with data already written under the new shape?",
            "What happens to in-flight readers and writers during the change?",
            "Is any personal data being newly stored, copied or exported?",
        ],
        out_of_scope=["frontend concerns", "unrelated tables", "infrastructure provisioning"],
        keywords=["database", "schema", "migration", "migrate", "sql", "table", "index", "query",
                  "postgres", "mysql", "sqlite", "mongo", "orm", "model", "persist",
                  "storage", "data", "record", "retention", "gdpr", "pii"],
        base_weight=0.0,
        host_agent_hints=["general-purpose"],
    ),
    Role(
        id="performance",
        title="Performance",
        kind=AgentKind.ANALYSIS,
        summary="Hot paths, algorithmic cost, resource use and caching.",
        charter=(
            "Identify where this task creates or removes cost. Be specific about the "
            "path, the input size that makes it matter, and the measurement that would "
            "confirm it. Do not report micro-optimisations with no measured impact."
        ),
        objectives=[
            "Locate the hot paths this change affects and their complexity",
            "Identify N+1 patterns, unbounded growth and blocking work on latency paths",
            "State how any claimed improvement or regression would be measured",
        ],
        focus_questions=[
            "At what input size does this become a problem?",
            "What is the blocking or serialising step on the critical path?",
            "What measurement would falsify this concern?",
        ],
        out_of_scope=["style", "security review", "unmeasurable micro-optimisation"],
        keywords=["performance", "slow", "latency", "throughput", "optimize", "optimise",
                  "cache", "load", "memory", "cpu", "concurrency", "async", "parallel",
                  "benchmark", "n+1", "bottleneck", "under load", "too slow", "times out",
                  "hang", "freeze", "sluggish", "degrade", "spike", "seconds to load"],
        base_weight=0.0,
        host_agent_hints=["general-purpose"],
    ),
    Role(
        id="operations",
        title="Operations and delivery",
        kind=AgentKind.ANALYSIS,
        summary="Deployment, configuration, rollback, monitoring and failure modes.",
        charter=(
            "Work out how this reaches production and what happens when it misbehaves "
            "there. Cover configuration, feature flagging, the rollback path, and the "
            "signal that would tell an operator something is wrong."
        ),
        objectives=[
            "Describe the deployment and rollback path for this change",
            "Identify new configuration, secrets or infrastructure required",
            "Define the signal that reveals failure in production",
        ],
        focus_questions=[
            "How is this turned off quickly if it goes wrong?",
            "What new configuration must exist before this can start successfully?",
            "Which dependency's outage takes this down with it?",
        ],
        out_of_scope=["code style", "product strategy", "unrelated infrastructure"],
        keywords=["deploy", "release", "downtime", "ci", "cd", "pipeline", "docker", "kubernetes",
                  "infra", "infrastructure", "config", "environment", "rollback", "flag",
                  "monitor", "alert", "ops", "production", "terraform", "cloud"],
        base_weight=0.0,
        host_agent_hints=["general-purpose"],
    ),
    Role(
        id="ux",
        title="User experience",
        kind=AgentKind.ANALYSIS,
        summary="User-visible behaviour, flows, accessibility and wording.",
        charter=(
            "Assess what the user actually experiences. Cover the flow including its "
            "error and empty states, accessibility, and the wording of anything the "
            "user reads. Judge against user goals, not aesthetic preference."
        ),
        objectives=[
            "Trace the user flow including error, empty and loading states",
            "Check accessibility: keyboard, contrast, labels, target size",
            "Review user-facing wording for clarity and honesty",
        ],
        focus_questions=[
            "What does the user see when this fails?",
            "Can this be completed without a mouse and with a screen reader?",
            "Does the wording tell the user what to do next?",
        ],
        out_of_scope=["backend architecture", "database design", "build tooling"],
        keywords=["ui", "ux", "user", "interface", "frontend", "design", "form", "page",
                  "screen", "button", "accessibility", "a11y", "copy", "wording", "css",
                  "layout", "component", "react", "vue", "svelte"],
        base_weight=0.0,
        host_agent_hints=["design", "general-purpose"],
    ),
    Role(
        id="risk",
        title="Risk and compliance",
        kind=AgentKind.ANALYSIS,
        summary="Licensing, regulatory exposure, contractual and reputational risk.",
        charter=(
            "Identify obligations and exposures this task creates: licence "
            "compatibility, regulatory duties, contractual commitments and irreversible "
            "actions. Only raise items with a concrete basis, and name that basis."
        ),
        objectives=[
            "Check licence compatibility of anything newly introduced",
            "Identify regulatory or contractual obligations triggered by the change",
            "Flag irreversible or externally visible actions needing explicit sign-off",
        ],
        focus_questions=[
            "What obligation does this create that did not exist before?",
            "Which part of this cannot be undone once shipped?",
            "Whose approval is genuinely required, and for what specifically?",
        ],
        out_of_scope=["implementation detail", "performance", "speculative legal opinion"],
        keywords=["licence", "license", "gdpr", "hipaa", "pci", "compliance", "legal",
                  "regulation", "audit", "contract", "terms", "privacy", "consent",
                  "retention", "third-party", "vendor"],
        base_weight=0.0,
        host_agent_hints=["general-purpose"],
    ),
    Role(
        id="research",
        title="Prior art and context",
        kind=AgentKind.ANALYSIS,
        summary="Existing solutions, project conventions and documented constraints.",
        charter=(
            "Establish what already exists before anything new is proposed. Search the "
            "codebase for prior solutions to this problem, read the project's own "
            "conventions and documentation, and report what constrains the answer."
        ),
        objectives=[
            "Find existing code, utilities or patterns that already solve part of this",
            "Extract the project's stated conventions and constraints",
            "Identify the documented decisions this task must respect or explicitly revisit",
        ],
        focus_questions=[
            "Has this already been solved somewhere in this repository?",
            "What convention does this project follow that a newcomer would violate?",
            "Which past decision does this contradict?",
        ],
        out_of_scope=["making the decision", "writing code", "opinions without evidence"],
        keywords=["research", "investigate", "explore", "understand", "how does", "why does",
                  "existing", "convention", "documentation", "prior", "compare", "evaluate",
                  "options", "alternatives"],
        base_weight=0.45,
        host_agent_hints=["Explore", "general-purpose"],
    ),
]


# --------------------------------------------------------------------------
# Execution and verification roles
# --------------------------------------------------------------------------

EXECUTION_ROLES: list[Role] = [
    Role(
        id="implementer",
        title="Implementer",
        kind=AgentKind.EXECUTION,
        summary="Makes the change, following existing conventions.",
        charter=(
            "Implement exactly the approved task -- no more. Match the surrounding "
            "code's conventions, naming and structure. If you discover the task cannot "
            "be done as specified, stop and report rather than substituting your own "
            "plan. You are writing into a tree other agents are writing into at the "
            "same time: change your own files, and never the tree's git state."
        ),
        objectives=["Implement the approved action", "Match existing conventions",
                    "Leave the workspace in a working state"],
        out_of_scope=["unrequested refactors", "unrelated files", "changing the task's intent",
                      "tree-wide git state operations (stash, checkout, clean, reset, rebase)"],
        keywords=["implement", "add", "build", "create", "write", "fix", "change"],
        host_agent_hints=["general-purpose", "claude"],
    ),
    Role(
        id="test-engineer",
        title="Test engineer",
        kind=AgentKind.EXECUTION,
        summary="Writes the tests that prove the task's definition of done.",
        charter=(
            "Write tests that would fail if the behaviour were wrong. Cover the stated "
            "criteria, the error paths and the edge cases named in analysis. Tests that "
            "assert implementation details rather than behaviour are not acceptable."
        ),
        objectives=["Cover each verifiable criterion with a test",
                    "Cover error and edge-case paths", "Keep tests deterministic"],
        out_of_scope=["changing production code to make tests pass", "unrelated test files",
                      "tree-wide git state operations (stash, checkout, clean, reset, rebase)"],
        keywords=["test", "coverage", "spec", "pytest", "jest", "unit", "integration"],
        host_agent_hints=["general-purpose", "claude"],
    ),
    Role(
        id="security-engineer",
        title="Security engineer",
        kind=AgentKind.EXECUTION,
        summary="Applies the security-relevant part of the change.",
        charter=(
            "Implement the security-relevant work with the failure path as the first "
            "concern: it must fail closed. Do not add security theatre; every control "
            "must map to a finding or a stated criterion."
        ),
        objectives=["Implement the control", "Ensure it fails closed",
                    "Add a test that proves the control actually blocks"],
        out_of_scope=["unrelated hardening", "performance work", "cosmetic changes",
                      "tree-wide git state operations (stash, checkout, clean, reset, rebase)"],
        keywords=["security", "auth", "validate", "sanitize", "permission", "encrypt"],
        host_agent_hints=["general-purpose", "claude"],
    ),
    Role(
        id="documenter",
        title="Documenter",
        kind=AgentKind.EXECUTION,
        summary="Updates documentation to match what was actually built.",
        charter=(
            "Document what the code now does, verified by reading it. Do not describe "
            "intended behaviour you have not confirmed. Match the existing document's "
            "voice and structure."
        ),
        objectives=["Update affected documentation", "Verify each claim against the code"],
        out_of_scope=["changing code", "rewriting unrelated documents",
                      "tree-wide git state operations (stash, checkout, clean, reset, rebase)"],
        keywords=["document", "readme", "docs", "changelog", "comment"],
        host_agent_hints=["general-purpose", "claude"],
    ),
]

VERIFICATION_ROLES: list[Role] = [
    Role(
        id="verifier",
        title="Verifier",
        kind=AgentKind.VERIFICATION,
        summary="Proves each definition-of-done criterion, or proves it unmet.",
        charter=(
            "Verify each criterion independently and adversarially. Run the stated "
            "command and report its real output. Never mark a criterion passed on the "
            "implementer's assurance -- only on evidence you produced yourself. "
            "Reporting an honest failure is a successful verification. You are not "
            "alone in the working tree: judge a whole-repository check against the "
            "run's baseline commit plus this task's diff, and never manipulate the "
            "tree's git state to get yourself a cleaner measurement."
        ),
        objectives=["Verify every mandatory criterion with first-hand evidence",
                    "Record the exact evidence for each verdict",
                    "Report unmet criteria plainly"],
        out_of_scope=["fixing the code", "renegotiating the criteria", "partial credit",
                      "tree-wide git state operations (stash, checkout, clean, reset, rebase)"],
        keywords=["verify", "check", "prove", "validate"],
        host_agent_hints=["general-purpose", "claude"],
    ),
    Role(
        id="reviewer",
        title="Reviewer",
        kind=AgentKind.VERIFICATION,
        summary="Reviews the delivered change for correctness and quality.",
        charter=(
            "Review the actual diff for correctness bugs, missed edge cases, and quality "
            "or security regressions. Anchor every finding to a file and line, and state "
            "the concrete failure it would cause."
        ),
        objectives=["Find correctness defects in the delivered change",
                    "Check the change stayed inside the approved scope",
                    "Confirm code quality and security bars are met"],
        out_of_scope=["redesigning the solution", "style preferences without a rule",
                      "tree-wide git state operations (stash, checkout, clean, reset, rebase)"],
        keywords=["review", "audit", "inspect"],
        host_agent_hints=["code-review", "general-purpose", "claude"],
    ),
]

ALL_ROLES: list[Role] = ANALYSIS_ROLES + EXECUTION_ROLES + VERIFICATION_ROLES
ROLES_BY_ID: dict[str, Role] = {r.id: r for r in ALL_ROLES}


def get_role(role_id: str) -> Role | None:
    return ROLES_BY_ID.get(role_id)


# --------------------------------------------------------------------------
# Lens selection
# --------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9+#.-]+")
_CONJUNCTIONS = (" and ", " then ", " also ", " as well as ", " plus ", ";", " while ")


def _normalise(words: set[str]) -> set[str]:
    """Add naive singular forms so 'tokens' matches the keyword 'token'."""
    out = set(words)
    for word in words:
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            out.add(word[:-1])
    return out


def task_complexity(prompt: str) -> float:
    """Rough 0..1 sense of how much task there is here.

    A one-line typo fix should not summon six analysis lenses; a multi-clause
    feature request should. Length and clause count are crude but stable, and
    the planning model can override the outcome when it disagrees.
    """
    text = prompt.lower().strip()
    words = len(_WORD.findall(text))
    size = min(1.0, max(0.0, (words - 4) / 24))
    clauses = sum(text.count(c) for c in _CONJUNCTIONS)
    return min(1.0, size + 0.12 * min(clauses, 3))


def score_lenses(prompt: str, context_hints: list[str] | None = None) -> list[tuple[Role, float]]:
    """Score each analysis lens for relevance to this task.

    Deliberately transparent and deterministic, so a run never depends on a
    model call just to decide which questions to ask::

        score = base_weight * complexity_factor + keyword_signal

    The signal divides hits by the square root of the keyword-list length, so a
    role cannot buy relevance by listing more keywords than its neighbours.
    """
    haystack = " ".join([prompt.lower(), *(h.lower() for h in context_hints or [])])
    words = _normalise(set(_WORD.findall(haystack)))
    factor = 0.4 + 0.6 * task_complexity(prompt)

    scored: list[tuple[Role, float]] = []
    for role in ANALYSIS_ROLES:
        hits = sum(
            1 for kw in role.keywords
            if ((kw in haystack) if " " in kw else (kw in words))
        )
        signal = (hits / max(1.0, len(role.keywords) ** 0.5)) * 1.7
        scored.append((role, round(role.base_weight * factor + signal, 3)))

    scored.sort(key=lambda pair: (pair[1], pair[0].base_weight), reverse=True)
    return scored


def select_lenses(
    prompt: str,
    *,
    minimum: int = 2,
    maximum: int = 6,
    require: list[str] | None = None,
    context_hints: list[str] | None = None,
    threshold: float = 0.55,
) -> list[Role]:
    """Choose the analysis lenses that fit this task.

    ``require`` always wins -- policy uses it to force a security lens onto
    anything that touches code, regardless of how the prompt is worded.
    """
    scored = score_lenses(prompt, context_hints)
    chosen: list[Role] = [ROLES_BY_ID[r] for r in (require or []) if r in ROLES_BY_ID]

    # Bigger tasks earn a wider net.
    floor = max(minimum, 3 if task_complexity(prompt) >= 0.35 else minimum)

    for role, score in scored:
        if len(chosen) >= maximum:
            break
        if any(c.id == role.id for c in chosen):
            continue
        if score >= threshold or len(chosen) < floor:
            chosen.append(role)

    return chosen[:maximum]


def role_for_task(title: str, action: str, hint: str = "") -> Role:
    """Pick the execution role that best fits an approved task."""
    if hint and hint in ROLES_BY_ID and ROLES_BY_ID[hint].kind is AgentKind.EXECUTION:
        return ROLES_BY_ID[hint]

    haystack = f"{title} {action}".lower()
    words = set(_WORD.findall(haystack))
    best, best_score = ROLES_BY_ID["implementer"], 0.0
    for role in EXECUTION_ROLES:
        score = sum(1 for k in role.keywords if k in words)
        if score > best_score:
            best, best_score = role, score
    return best
