"""Configuration: providers, per-stage model routing and supervision policy.

Layering, later wins::

    built-in defaults
    ~/.supervisor/config.json          user-wide
    <workspace>/.supervisor/config.json
    <workspace>/supervisor.config.json  checked into the project
    environment overrides

Model routing is per *stage*, so an architecture review can run on a strong
hosted model while drift checks run locally on Ollama.
"""

from __future__ import annotations

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
class ProviderConfig:
    """One configured model source."""

    type: str = "openrouter"      # openrouter | ollama | anthropic | host
    enabled: bool = True
    base_url: str = ""
    api_key_env: str = ""
    api_key: str = ""             # discouraged; prefer api_key_env
    default_model: str = ""
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
    drift_check_every: int = 1          # assess after every N turns
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
    min_dod_criteria: int = 2

    # Whether the harness itself may run verification commands. Off by default:
    # in host-delegated mode the host runs them under the user's own permission
    # model, which is where that decision belongs.
    allow_command_execution: bool = False
    command_timeout_seconds: int = 300

    # Analysis
    min_analysis_lenses: int = 2
    max_analysis_lenses: int = 6

    # Improvement loop
    learn_from_failures: bool = True
    apply_lessons: bool = True
    max_lessons_in_brief: int = 6
    # Bounds on the shared lessons library. It is deliberately cross-workspace,
    # so it needs an edge: a lesson older than this is not applied and is dropped
    # the next time the file is rewritten, and one lesson cannot accumulate
    # unbounded occurrences and dominate the ranking of every later brief.
    lesson_max_age_days: int = 180
    lesson_max_occurrences: int = 20


# Only "default" is set. Every other stage resolves through the fallback chain
# in :meth:`HarnessConfig.binding_for`, so overriding "default" in a config file
# actually redirects the whole run -- which it would not if the defaults named
# every stage explicitly.
DEFAULT_ROUTING: dict[str, str] = {"default": "host"}

# Stages the harness routes, listed for discoverability in `supervisor providers`
# and in the generated example config.
KNOWN_STAGES: tuple[str, ...] = (
    "default", "supervisor", "planning", "analysis", "synthesis",
    "execution", "verification", "drift", "improvement",
)


@dataclass
class HarnessConfig:
    """Everything the harness needs that is not part of a specific run."""

    home: str = ""
    backend: Backend = Backend.HOST
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    routing: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ROUTING))
    policy: Policy = field(default_factory=Policy)
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)   # extra/overridden roles
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

# The line the second group draws: a workspace file may tune how much work the
# harness does, and may not tune how sceptical it is. Budgets, parallelism and
# turn counts are a project's own business. The bars a change is judged against,
# and the thresholds at which an agent is corrected or stopped, are the
# supervisor's -- and the workspace is the subject of that judgement. A
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
    ("policy", "min_dod_criteria"),
    # The checkpoint's own pass mark.
    ("policy", "checkpoint_threshold"),
    # Drift control: how far an agent strays before it is corrected or stopped,
    # and whether a model is asked for a second opinion at all.
    ("policy", "drift_threshold"),
    ("policy", "drift_hard_threshold"),
    ("policy", "model_drift_check"),
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
PROTECTED_PROVIDER_KEYS: frozenset[str] = frozenset(
    {"type", "base_url", "api_key", "api_key_env", "params"}
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
        if not isinstance(overlay, dict):
            continue
        if not trusted:
            overlay, dropped = _strip_untrusted(overlay)
            rejected.extend(f"{setting} (from {path})" for setting in dropped)
        merged = _deep_merge(merged, overlay)
        sources.append(str(path))

    config = from_jsonable(merged, HarnessConfig)
    config.sources = sources
    config.rejected_settings = rejected
    _apply_env_overrides(config)
    return config


def _apply_env_overrides(config: HarnessConfig) -> None:
    """Environment wins over files, so a single run can be redirected cheaply."""
    backend = os.environ.get("SUPERVISOR_BACKEND")
    if backend:
        try:
            config.backend = Backend(backend.lower())
        except ValueError:
            pass

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
            "Workspace config. It may tune how much work the harness does -- "
            "budgets, parallelism, turn counts, routing. It may not tune how "
            "sceptical the harness is about work done on this repository, nor "
            "where your credentials go, so the quality bars, the checkpoint pass "
            "mark, the drift thresholds, providers.*.base_url / api_key / "
            "api_key_env / type / params, policy.allow_command_execution and home "
            "are all ignored here on purpose. Put those in "
            "~/.supervisor/config.json instead, where you own the file."
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
