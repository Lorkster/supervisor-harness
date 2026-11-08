"""Configuration: providers, per-stage model routing and supervision policy.

Layering, later wins::

    built-in defaults
    ~/.supervisor/config.json          user-wide
    $SUPERVISOR_HOME/config.json       when SUPERVISOR_HOME is set
    <workspace>/.supervisor/config.json
    <workspace>/supervisor.config.json  checked into the project
    environment overrides

Model routing is per *stage*, so an architecture review can run on a strong
hosted model while drift checks run locally on Ollama.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Backend, ModelBinding
from .serde import from_jsonable, to_jsonable

CONFIG_BASENAME = "config.json"
PROJECT_CONFIG = "supervisor.config.json"


@dataclass
class ToolServer:
    """An MCP server whose tools the run's agents may use (`core/external.py`).

    Only the owner's own config may name one: the harness runs ``command``.
    ``allow`` is ``read-only`` -- tools that change nothing outside the
    server's session, read from ``effects_tool``'s catalog where the server
    publishes one -- or ``all``. ``read_roots`` are folders beyond the run's
    tree that a tool's paths may name.
    """

    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    allow: str = "read-only"
    effects_tool: str = ""
    read_roots: list[str] = field(default_factory=list)
    start_seconds: float = 120.0
    call_seconds: float = 900.0


@dataclass
class ProviderConfig:
    """One configured model source."""

    type: str = "openrouter"      # openrouter | ollama | anthropic | bedrock | host
    enabled: bool = True
    base_url: str = ""
    api_key_env: str = ""
    api_key: str = ""             # discouraged; prefer api_key_env
    default_model: str = ""
    # AWS region and profile, for `type: "bedrock"`. Both fall back to the
    # standard environment variables when unset, so a machine already configured
    # for Bedrock needs neither set here.
    region: str = ""
    profile: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    def resolved_key(self) -> str:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "")
        return ""


@dataclass
class Policy:
    """How hard the supervisor pushes back."""

    max_parallel_agents: int = 4
    default_max_turns: int = 6
    execution_max_turns: int = 10

    # Drift control
    # Heuristics assess every turn. This sets the cadence of the *model*
    # spot-check that catches quiet drift: one on every 3 x N-th turn. 0 turns
    # the spot-check off.
    drift_check_every: int = 1
    drift_threshold: float = 0.45       # score above which a correction is issued
    drift_hard_threshold: float = 0.8   # score above which the agent is stopped
    model_drift_check: bool = True      # escalate to a model when heuristics fire

    # Abandoning a host agent. A host-run agent has no failure path of its own:
    # it reports through the caller, so a crashed or cancelled subagent is
    # silent rather than failed, and its packet is re-emitted on every advance.
    # These bound the silence. The dispatch count is what a host actually
    # exercises, since the supervisor only wakes when the host calls it; the
    # wall-clock bound is off by default because a host agent may legitimately
    # take a long time and the host can always abandon it explicitly.
    max_unreported_dispatches: int = 3   # packets with no report in between; 0 disables
    agent_timeout_seconds: float = 0.0   # since the first unanswered packet; 0 disables

    # Quality gates
    checkpoint_threshold: float = 0.75  # mean of quality/scope/completeness to pass
    max_checkpoint_iterations: int = 3
    max_task_attempts: int = 3

    # Mandatory quality bars, applied where the task admits them
    require_tests: bool = True
    require_security_review: bool = True
    require_code_quality: bool = True
    # A task whose point is a fence or a guard needs the case it must reject,
    # and a task that touches locking, retries or I/O needs to say it still
    # terminates. Both are conditional on the task's own subject matter, so
    # neither fires on work that cannot fail that way.
    require_negative_test: bool = True
    require_liveness_review: bool = True
    # The tests a task adds must fail on the baseline commit and pass with the
    # change, proven by the harness running them (`core/fails_before.py`).
    # Applied to code tasks that change behaviour, in a git workspace whose
    # tests run under pytest, when `allow_command_execution` is on: without it
    # the check would be a verifier agent's claim, not the harness's proof.
    require_fails_before: bool = True
    # Batch P: the tests a testing task marks with a requirement must catch a
    # break of each of its clauses -- a model proposes the breaks, the harness
    # applies them and runs the tests (`core/sabotage.py`). Needs
    # `allow_command_execution`; without it the criterion is not added.
    require_catches_breaks: bool = True
    min_dod_criteria: int = 2

    # Whether the harness itself may run verification commands. Off by default:
    # in host-delegated mode the host runs them under the user's own permission
    # model, which is where that decision belongs.
    allow_command_execution: bool = False
    # An agent's own commands: a bound on one turn of its shell.
    command_timeout_seconds: int = 300
    # The harness's own checks -- a criterion's command, the same command on the
    # baseline, the fails-before run. A check cut short is a failure nobody
    # caused: in a baseline run on this repository the full suite took about 330
    # seconds in the run's worktree, and a task whose every other criterion had
    # passed failed on "command timed out after 300s: pytest -q".
    check_timeout_seconds: int = 1800

    # An execute-mode run on the autonomous backend works in a git worktree on
    # its own branch, never in the owner's tree, and ends with that branch.
    # `core/worktree.py`. In a workspace that is not a git repository there is
    # nothing to branch from, and the run executes in place, saying so.
    # Not protected: a workspace turning it off puts the work back in its own
    # tree, which is the owner's call about the owner's tree.
    execution_worktree: bool = True

    # How proposed tasks are approved: "task", one by one by a person, or
    # "envelope", by the harness within an envelope the owner granted when the
    # run started, with anything its deterministic gate refuses sent to the
    # owner as an escalation. See `core/autonomy.py`. Protected: a repository
    # able to set this would be choosing to skip review of work done on itself.
    approval: str = "task"

    # How an implementer is driven on the autonomous backend: "turns", each
    # supervised turn a fresh JSON answer whose tool results are dropped when
    # it ends, or "conversation", one conversation per implementer in which
    # what it read is kept, tools are called through the provider's native
    # interface, and supervision checks its progress every so many calls. See
    # `core/conversation.py`. Only where the implementer's provider takes tools
    # natively; anywhere else, "turns". Not protected: it changes how the work
    # is done, not what is allowed or how it is checked.
    implementer_loop: str = "turns"

    # How the synthesis that writes a run's tasks is called on the autonomous
    # backend: "one_shot", one JSON answer with thinking off and no tools, or
    # "conversation", a conversation with read-only native tools in which the
    # planner can open the files a task would change before naming them, and
    # answers by calling propose_plan. See `core/planner.py`. Only where the
    # synthesis provider takes tools natively; anywhere else, "one_shot". Not
    # protected, for the reason implementer_loop is not. The default since a
    # measurement on a local model (seven planner cases, three times each):
    # plans that passed every planner check, 15/21 against 3/21 one-shot and
    # 4/21 one-shot with thinking on, for about a minute more planning.
    planner_loop: str = "conversation"

    # Under envelope approval, a second reading of each task the gate passes,
    # by a reviewer that can only veto (`core/review.py`), routed to the
    # "review" stage. Protected: a repository able to turn it off would be
    # choosing to skip the one reading of its own changes that is not a gate.
    veto_review: bool = True

    # A task whose check fails goes back at once, while attempts remain, and its
    # next attempt continues its implementer's conversation -- not at the
    # checkpoint, from a fresh agent (`Supervisor._follow_up`). Off restores the
    # checkpoint as the only way back. Not protected: either way every check
    # still has to pass. Read only with `coordinator` off, which decides instead.
    follow_up: bool = True
    # Batch O: when reports come back -- a failed check, an implementer that
    # stopped, a finding about another task's work -- a coordinator model picks
    # what happens next from a fixed menu, and the harness checks each pick
    # (`core/coordinator.py`). Off, a failed task is followed up as itself and
    # a stopped one goes to the owner. Not protected: it cannot pass a check,
    # drop one, or widen anything.
    coordinator: bool = True
    # How many times a run may consult it: the bound that makes a run stop,
    # beside each task's attempts. Past it, what is open is followed up within
    # its attempts, left failed, or put to the owner, as each needs.
    max_coordinator_decisions: int = 24
    # Once nothing more can run, the final gates' checks run again on a clean
    # checkout of what the run would commit (`core/clean.py`): a check that
    # passes only on what the run's tree has by accident -- build output, caches
    # -- fails there, and its task is sent back. Off skips it.
    clean_check: bool = True

    # Batch L: before the plan, a reader lists what the request and the
    # documents it points to require, quoted (`core/requirements.py`); the plan
    # must account for each and have each tested by a task other than its
    # builder. Protected: a repository able to turn it off would be choosing
    # to be judged only by the criteria its planner happened to write.
    requirements_ledger: bool = True

    # The run's scope envelope: the ceiling on every agent scope in the run.
    # Empty means the whole workspace, which is what an empty scope already
    # means to the toolbox and to the floor beneath it. Not in
    # PROTECTED_SETTINGS, but a workspace file is merged *narrowing only*
    # (`_narrow_scope`): it cannot replace the owner's envelope with a wider
    # one, nor empty their forbidden list -- lists are replaced by a plain
    # merge, which is how an empty list once widened it. A plan may narrow this
    # further; nothing may widen it.
    scope_envelope: list[str] = field(default_factory=list)
    scope_envelope_forbidden: list[str] = field(default_factory=list)
    # How long a grant stands before a resume has to ask again. Resuming a run
    # older than this continues to analyse and report freely, but will not spawn
    # an execution agent until the envelope is renewed -- the same rule as "you
    # approve before anything touches your code", applied to consent that has
    # gone stale rather than to consent that was never given. 0 disables it.
    #
    # Protected: 0 turns the expiry off and a large number is as good, so a
    # workspace able to set it could keep its own grant alive for ever.
    envelope_max_age_days: int = 7

    # Analysis
    min_analysis_lenses: int = 2
    max_analysis_lenses: int = 6
    # The share of the files in its scope an analysis agent must have read
    # before its "done" is accepted while it still has turns left. Below it, the
    # agent is sent back once, with the files it has not read named; its next
    # "done" is accepted whatever it read, and the coverage goes on the record.
    # Measured on a security lens that read 4 of 18 files, said in its own
    # self-assessment which ones it had skipped, and was accepted after one turn
    # of six. 0 turns the check off.
    min_scope_coverage: float = 0.5
    # Lenses every run includes, whatever the prompt says -- built-in or defined
    # under `roles`. Not protected: a workspace can only *add* scrutiny with it.
    # The security lens is forced separately by `require_security_review`,
    # which is protected, so this list cannot be used to drop it.
    required_lenses: list[str] = field(default_factory=list)

    # Improvement loop
    learn_from_failures: bool = True
    apply_lessons: bool = True
    # How consolidation ages the library. The clock is runs-since-confirmed in a
    # workspace that knows the lesson, not wall-clock time -- see
    # `core/consolidate.py` for why. Nothing decays inside the grace period: a
    # lesson is not weaker for having fired in the previous run and not this one.
    lesson_decay_after_runs: int = 10
    lesson_decay_per_run: float = 0.05
    # Below this, a lesson is archived. Not deleted: `lesson_max_age_days` is
    # still the outer bound that actually removes rows.
    lesson_confidence_floor: float = 0.15
    max_lessons_in_brief: int = 6
    # Bounds on the shared lessons library. It is deliberately cross-workspace,
    # so it needs an edge: a lesson older than this is not applied and is dropped
    # the next time the file is rewritten, and one lesson cannot accumulate
    # unbounded occurrences and dominate the ranking of every later brief.
    lesson_max_age_days: int = 180
    lesson_max_occurrences: int = 20      # `Supervisor._record_lesson` passes it to the store


# Only "default" is set. Every other stage resolves through the fallback chain
# in :meth:`HarnessConfig.binding_for`, so overriding "default" in a config file
# actually redirects the whole run -- which it would not if the defaults named
# every stage explicitly.
DEFAULT_ROUTING: dict[str, str] = {"default": "host"}

# Stages the harness routes, listed for discoverability in `supervisor providers`
# and in the generated example config.
KNOWN_STAGES: tuple[str, ...] = (
    "default", "supervisor", "planning", "analysis", "synthesis",
    "execution", "verification", "drift", "improvement", "review", "coordination",
)


@dataclass
class HarnessConfig:
    """Everything the harness needs that is not part of a specific run."""

    home: str = ""
    backend: Backend = Backend.HOST
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    routing: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ROUTING))
    policy: Policy = field(default_factory=Policy)
    # Analysis lenses defined here, beside the built-in ones: id -> {title,
    # charter, summary, objectives, focus_questions, out_of_scope, keywords,
    # base_weight, host_agent_hints}. Validated at load, and a workspace file
    # may only add new ids -- see `_check_roles`.
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Put the whole brief and schema in the packet instead of writing them out
    # and pointing at them. Off by default: the by-reference form exists because
    # a host-delegated run pushes every brief, result and directive through the
    # orchestrator's context, and the orchestrator is not the reader. Turn it on
    # for a host that cannot read files -- it costs context, not correctness.
    # Not a PROTECTED_SETTING: a workspace choosing this only makes its own runs
    # more expensive, which is not the direction protection exists to stop.
    inline_briefs: bool = False
    # The owner's analysis tool servers, by name (`ToolServer`). Protected: the
    # harness runs each one's command.
    tool_servers: dict[str, ToolServer] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)                 # files that contributed
    # Settings a workspace file tried to set but was not permitted to; surfaced
    # to the user rather than silently dropped.
    rejected_settings: list[str] = field(default_factory=list)

    # -- routing -----------------------------------------------------------

    def binding_for(self, stage: str) -> ModelBinding:
        """Resolve a stage to a concrete provider/model, most specific first.

        ``analysis.security`` falls back to ``analysis`` and then ``default``.
        """
        for key in _stage_lookup(stage):
            ref = self.routing.get(key)
            if ref:
                return self._parse_ref(ref)
        return self._parse_ref(self.routing.get("default", "host"))

    def _parse_ref(self, ref: str) -> ModelBinding:
        parts = [p.strip() for p in ref.split("|")]
        primary, fallbacks = parts[0], parts[1:]
        provider, _, model = primary.partition(":")
        provider = provider or "host"
        if not model:
            cfg = self.providers.get(provider)
            model = cfg.default_model if cfg else ""
        cfg = self.providers.get(provider)
        return ModelBinding(
            provider=provider,
            model=model,
            params=dict(cfg.params) if cfg else {},
            fallbacks=fallbacks,
        )

    def enabled_providers(self) -> list[str]:
        return [name for name, cfg in self.providers.items() if cfg.enabled]


def _stage_lookup(stage: str) -> list[str]:
    """['analysis.security', 'analysis', 'default'] for 'analysis.security'."""
    keys = [stage]
    while "." in stage:
        stage = stage.rsplit(".", 1)[0]
        keys.append(stage)
    keys.append("default")
    return keys


def default_config() -> HarnessConfig:
    """Built-in defaults: host-delegated, with local and hosted providers declared."""
    return HarnessConfig(
        backend=Backend.HOST,
        providers={
            "host": ProviderConfig(type="host", default_model=""),
            "ollama": ProviderConfig(
                type="ollama",
                base_url="http://localhost:11434",
                default_model="qwen3.8-code:latest",
            ),
            "openrouter": ProviderConfig(
                type="openrouter",
                api_key_env="OPENROUTER_API_KEY",
                default_model="anthropic/claude-sonnet-4.5",
            ),
            "anthropic": ProviderConfig(
                type="anthropic",
                api_key_env="ANTHROPIC_API_KEY",
                default_model="claude-sonnet-4-5",
            ),
        },
        routing=dict(DEFAULT_ROUTING),
        policy=Policy(),
    )


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------
# Trust boundary
# --------------------------------------------------------------------------

# Configuration arrives from two kinds of place. Files under the user's own home
# (or an explicit SUPERVISOR_HOME) are *trusted*: the user put them there. Files
# inside the workspace are *untrusted*, because the workspace is frequently a
# repository someone else wrote and the user has merely pointed the harness at.
#
# An untrusted file may tune how the harness thinks -- policy thresholds, model
# routing, budgets. It may not grant code execution, and it may not change where
# credentials are sent. Without this split, checking out a repository that
# happens to contain a supervisor.config.json is enough to turn shell execution
# on and redirect a provider's base_url to an attacker's host, at which point the
# provider still resolves the real API key from the environment and posts it there.

# The line the second group draws: a workspace file may choose how the harness
# routes its work, and may not tune how sceptical it is, how much it spends, or
# which backend runs it. Budgets, parallelism and turn counts were once a
# project's own business; they are the owner's now, because a repository that
# sets `max_parallel_agents` or `execution_max_turns` spends the owner's API
# budget, and `backend` decides whether the harness answers for itself. The bars
# a change is judged against, and the thresholds at which an agent is corrected
# or stopped, are the supervisor's -- and the workspace is the subject of that
# judgement. A
# repository that can set `require_security_review: false`, or
# `checkpoint_threshold: 0.0`, or `drift_threshold: 1.0`, is marking its own
# homework: it lowers the bar for work done *on it*, in a file it ships.
PROTECTED_SETTINGS: tuple[tuple[str, ...], ...] = (
    ("home",),
    ("policy", "allow_command_execution"),
    # Mandatory quality bars.
    ("policy", "require_tests"),
    ("policy", "require_security_review"),
    ("policy", "require_code_quality"),
    ("policy", "require_negative_test"),
    ("policy", "require_liveness_review"),
    ("policy", "require_fails_before"),
    ("policy", "require_catches_breaks"),
    ("policy", "min_dod_criteria"),
    # The checkpoint's own pass mark.
    ("policy", "checkpoint_threshold"),
    # Drift control: how far an agent strays before it is corrected or stopped,
    # and whether a model is asked for a second opinion at all.
    ("policy", "drift_threshold"),
    ("policy", "drift_hard_threshold"),
    ("policy", "model_drift_check"),
    # How much of its scope an analysis agent must read before "done" is
    # accepted: a repository lowering it would choose how closely it is read.
    ("policy", "min_scope_coverage"),
    # Whether a person reviews each task, or the harness does within a grant.
    ("policy", "approval"),
    ("policy", "veto_review"),
    ("policy", "requirements_ledger"),
    # Whether the harness drives the run itself or hands it to a host agent.
    ("backend",),
    # What the run may spend, and for how long: parallelism, turn counts and
    # attempt bounds multiply API calls, and the grant's expiry is a bound the
    # workspace would otherwise be setting on its own consent.
    ("policy", "max_parallel_agents"),
    ("policy", "default_max_turns"),
    ("policy", "execution_max_turns"),
    ("policy", "max_checkpoint_iterations"),
    ("policy", "max_task_attempts"),
    ("policy", "max_coordinator_decisions"),
    ("policy", "max_unreported_dispatches"),
    ("policy", "max_analysis_lenses"),
    ("policy", "max_lessons_in_brief"),
    ("policy", "envelope_max_age_days"),
    # How often a model is asked about drift, and whether the final gates are
    # checked again on a clean checkout.
    ("policy", "drift_check_every"),
    ("policy", "clean_check"),
    # The harness runs a tool server's command, and its tools act on the owner's
    # machine: a repository able to add one would choose what runs.
    ("tool_servers",),
)

# Per-provider keys an untrusted file may not touch, for the same reason.
#
# ``params`` is here because of where it ends up rather than what it looks like.
# ``router._merge`` folds it into ``CompletionRequest.extra``, and the
# OpenAI-compatible providers finish assembling the request body with
# ``body.update(request.extra)`` -- *after* ``model`` and ``messages`` have been
# assigned. A workspace file setting ``providers.x.params.messages`` therefore
# replaces the conversation every call sends, and ``params.model`` replaces the
# model answering it. Measured before this changed: ``base_url`` was correctly
# stripped from a workspace config while ``params`` carrying an attacker's
# ``model`` and ``messages`` survived intact.
#
# The trusted path is unaffected -- a user's own config under their home, or an
# explicit SUPERVISOR_HOME, still sets sampling parameters through this key.
# ``region`` and ``profile`` are here for the reason ``base_url`` is: both
# decide where a credentialed request goes and which identity signs it. A
# workspace file able to set them could move a run's traffic to a region the
# user does not audit, or make it assume a different profile -- without ever
# naming a key, because the AWS credential chain resolves those from the
# environment exactly as ``api_key_env`` does.
PROTECTED_PROVIDER_KEYS: frozenset[str] = frozenset(
    {"type", "base_url", "api_key", "api_key_env", "region", "profile", "params",
     "default_model"}
)


def _strip_untrusted(overlay: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Remove settings a workspace-level file is not allowed to set.

    Returns the sanitised overlay and a list of the rejected setting paths, so
    the user can be told what was ignored rather than silently losing it.
    """
    clean = json.loads(json.dumps(overlay))  # cheap deep copy of plain JSON
    rejected: list[str] = []

    for path in PROTECTED_SETTINGS:
        node = clean
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
            if not isinstance(node, dict):
                break
        if isinstance(node, dict) and path[-1] in node:
            node.pop(path[-1])
            rejected.append(".".join(path))

    providers = clean.get("providers")
    if isinstance(providers, dict):
        for name, cfg in providers.items():
            if not isinstance(cfg, dict):
                continue
            for key in sorted(PROTECTED_PROVIDER_KEYS & set(cfg)):
                cfg.pop(key)
                rejected.append(f"providers.{name}.{key}")

    return clean, rejected


def _check_roles(
    overlay: dict[str, Any], trusted: bool, defined: set[str]
) -> tuple[dict[str, Any], list[str]]:
    """Validate the lenses a config file defines, and apply the trust rule to them.

    A workspace file may add a lens with a new id. It may not redefine one that
    already exists -- a built-in, or one a trusted file defined. The workspace
    is usually the code under review, and a repository able to rewrite the
    security lens's charter would be setting the terms of its own review. The
    same reason `require_security_review` is protected. A trusted file may
    replace a built-in lens; that is the user's own decision.

    An invalid definition is dropped and reported rather than failing the load.
    """
    roles = overlay.get("roles")
    if roles is None:
        return overlay, []
    # Lazy: `agents` sits above `config`, and only this check needs the built-ins.
    from .agents.roles import ROLES_BY_ID, role_from_config

    clean = dict(overlay)
    kept: dict[str, Any] = {}
    rejected: list[str] = []
    if not isinstance(roles, dict):
        clean.pop("roles")
        return clean, ["roles (must be an object of id -> definition)"]
    for role_id, raw in roles.items():
        if not trusted and (role_id in ROLES_BY_ID or role_id in defined):
            rejected.append(f"roles.{role_id} (a workspace file may add lenses, not "
                            "redefine an existing one)")
            continue
        try:
            role_from_config(str(role_id), raw)
        except (ValueError, TypeError) as exc:
            rejected.append(f"roles.{role_id} ({exc})")
            continue
        kept[str(role_id)] = raw
    clean["roles"] = kept
    return clean, rejected


def candidate_paths(workspace: Path) -> list[tuple[Path, bool]]:
    """Config files to merge, in order, each flagged as trusted or not."""
    home_env = os.environ.get("SUPERVISOR_HOME")
    paths: list[tuple[Path, bool]] = [(Path.home() / ".supervisor" / CONFIG_BASENAME, True)]
    if home_env:
        paths.append((Path(home_env).expanduser() / CONFIG_BASENAME, True))
    paths.append((workspace / ".supervisor" / CONFIG_BASENAME, False))
    paths.append((workspace / PROJECT_CONFIG, False))
    return paths


def load_config(workspace: Path | str | None = None) -> HarnessConfig:
    """Load and merge configuration for a workspace.

    Later files win, except for the settings listed in :data:`PROTECTED_SETTINGS`
    and :data:`PROTECTED_PROVIDER_KEYS`, which a workspace file cannot set at all.
    """
    ws = Path(workspace) if workspace else Path.cwd()
    merged = to_jsonable(default_config())
    sources: list[str] = []
    rejected: list[str] = []

    for path, trusted in candidate_paths(ws):
        if not path.exists():
            continue
        try:
            overlay = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {path}: {exc}") from exc
        except (UnicodeDecodeError, OSError) as exc:
            # Not valid UTF-8, or not readable: this file contributes nothing,
            # and the owner is told so rather than shown a traceback.
            rejected.append(f"{path} (unreadable, so the whole file was ignored: {exc})")
            continue
        if not isinstance(overlay, dict):
            continue
        if not trusted:
            overlay, narrowed = _narrow_scope(overlay, merged)
            rejected.extend(f"{setting} (from {path})" for setting in narrowed)
            overlay, dropped = _strip_untrusted(overlay)
            rejected.extend(f"{setting} (from {path})" for setting in dropped)
        overlay, dropped = _check_roles(overlay, trusted, set(merged.get("roles") or {}))
        rejected.extend(f"{setting} (from {path})" for setting in dropped)
        merged = _deep_merge(merged, overlay)
        sources.append(str(path))

    config = from_jsonable(merged, HarnessConfig)
    config.sources = sources
    config.rejected_settings = rejected
    _apply_env_overrides(config)
    config.rejected_settings.extend(_clamp_policy(config.policy))
    return config


#: Policy bounds that must be at least this, whoever set them. A zero here is
#: not "unlimited" everywhere: `asyncio.Semaphore(0)` never admits an agent, and
#: a zero turn budget means "unlimited" to one reader and "no turns" to another.
_AT_LEAST: dict[str, int] = {
    "max_parallel_agents": 1,
    "default_max_turns": 1,
    "execution_max_turns": 1,
}


def _clamp_policy(policy: Policy) -> list[str]:
    """Raise out-of-range bounds to their minimum; say what was changed."""
    notes: list[str] = []
    for name, minimum in _AT_LEAST.items():
        value = getattr(policy, name)
        if value < minimum:
            setattr(policy, name, minimum)
            notes.append(f"policy.{name} = {value} (must be at least {minimum}; using {minimum})")
    return notes


def _narrow_scope(
    overlay: dict[str, Any], merged: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """A workspace file's scope envelope, merged so that it can only narrow.

    A plain merge replaces a list, so ``scope_envelope: []`` -- the whole
    workspace -- or an empty ``scope_envelope_forbidden`` from a repository
    replaced the owner's own, and widened it. The allowed paths are kept only
    where they lie inside the envelope already in force; the forbidden paths
    are added to the owner's, never taken from them.
    """
    policy = overlay.get("policy")
    if not isinstance(policy, dict):
        return overlay, []
    from .core.paths import globs_within

    base = (merged.get("policy") or {})
    notes: list[str] = []
    clean = dict(overlay)
    policy = dict(policy)
    clean["policy"] = policy

    if "scope_envelope" in policy:
        wanted = policy["scope_envelope"]
        current = [str(p) for p in base.get("scope_envelope") or []]
        if not isinstance(wanted, list):
            policy.pop("scope_envelope")
            notes.append("policy.scope_envelope (must be a list of paths)")
        else:
            kept = [str(p) for p in wanted if globs_within([str(p)], current)]
            if len(kept) != len(wanted) or (current and not wanted):
                notes.append("policy.scope_envelope (a workspace file may narrow the envelope, "
                             "not widen it; entries outside yours were ignored)")
            policy["scope_envelope"] = kept if (kept or not current) else current
    if "scope_envelope_forbidden" in policy:
        wanted = policy["scope_envelope_forbidden"]
        current = [str(p) for p in base.get("scope_envelope_forbidden") or []]
        if not isinstance(wanted, list):
            policy.pop("scope_envelope_forbidden")
            notes.append("policy.scope_envelope_forbidden (must be a list of paths)")
        else:
            policy["scope_envelope_forbidden"] = current + [
                str(p) for p in wanted if str(p) not in current
            ]
    return clean, notes


def _apply_env_overrides(config: HarnessConfig) -> None:
    """Environment wins over files, so a single run can be redirected cheaply."""
    backend = os.environ.get("SUPERVISOR_BACKEND")
    if backend:
        # An unrecognised value is ignored rather than fatal: the variable is
        # set by hand, and a typo should not stop a run from starting.
        with contextlib.suppress(ValueError):
            config.backend = Backend(backend.lower())

    if os.environ.get("SUPERVISOR_HOME"):
        config.home = os.environ["SUPERVISOR_HOME"]

    # SUPERVISOR_ROUTE_ANALYSIS=ollama:qwen3.8-code  ->  routing["analysis"]
    prefix = "SUPERVISOR_ROUTE_"
    for key, value in os.environ.items():
        if key.startswith(prefix) and value:
            stage = key[len(prefix):].lower().replace("__", ".")
            config.routing[stage] = value

    if os.environ.get("OLLAMA_HOST") and "ollama" in config.providers:
        config.providers["ollama"].base_url = os.environ["OLLAMA_HOST"]


def write_example(path: Path, *, trusted: bool = False) -> Path:
    """Write a starter configuration.

    A workspace file gets only the settings a workspace file may actually set,
    so the harness does not generate a file that trips its own trust boundary.
    Provider endpoints and credentials belong in the user-level config, and the
    generated file says so -- JSON has no comments, and the ``_note`` key is
    ignored by the loader.
    """
    example: dict[str, Any] = {
        "routing": {
            "default": "host",
            "supervisor": "host",
            "planning": "host",
            "analysis": "host",
            "analysis.security": "host",
            "analysis.architecture": "host",
            "synthesis": "host",
            "execution": "host",
            "verification": "host",
            "drift": "ollama:qwen3.8-code:latest|host",
            "improvement": "ollama:qwen3.8-code:latest|host",
        },
        "policy": to_jsonable(Policy()),
    }

    if trusted:
        providers = to_jsonable(default_config())["providers"]
        for provider in providers.values():
            # Never generate a field that invites pasting a key into a file.
            provider.pop("api_key", None)
            if not provider.get("base_url"):
                provider.pop("base_url", None)
        example["providers"] = providers
        example["backend"] = "host"
    else:
        example["_note"] = (
            "Workspace config. It may choose routing, add analysis lenses and "
            "narrow the scope envelope. It may not tune how sceptical the harness "
            "is about work done on this repository, what the run may spend, or "
            "where your credentials go, so these are ignored here on purpose: "
            + ", ".join(".".join(setting) for setting in PROTECTED_SETTINGS)
            + ", and providers.*."
            + " / providers.*.".join(sorted(PROTECTED_PROVIDER_KEYS))
            + ". Put those in ~/.supervisor/config.json instead, where you own "
            "the file."
        )
        # Everything the loader would strip is left out rather than written and
        # then ignored: a generated file that trips its own trust boundary
        # teaches the reader that the boundary is noise.
        for setting in PROTECTED_SETTINGS:
            if len(setting) == 2 and setting[0] == "policy":
                example["policy"].pop(setting[1], None)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(example, indent=2) + "\n", encoding="utf-8")
    return path
