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

    # Quality gates
    checkpoint_threshold: float = 0.75  # mean of quality/scope/completeness to pass
    max_checkpoint_iterations: int = 3
    max_task_attempts: int = 3

    # Mandatory quality bars, applied where the task admits them
    require_tests: bool = True
    require_security_review: bool = True
    require_code_quality: bool = True
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


def candidate_paths(workspace: Path) -> list[Path]:
    home_env = os.environ.get("SUPERVISOR_HOME")
    paths = [Path.home() / ".supervisor" / CONFIG_BASENAME]
    if home_env:
        paths.append(Path(home_env).expanduser() / CONFIG_BASENAME)
    paths.append(workspace / ".supervisor" / CONFIG_BASENAME)
    paths.append(workspace / PROJECT_CONFIG)
    return paths


def load_config(workspace: Path | str | None = None) -> HarnessConfig:
    """Load and merge configuration for a workspace."""
    ws = Path(workspace) if workspace else Path.cwd()
    merged = to_jsonable(default_config())
    sources: list[str] = []

    for path in candidate_paths(ws):
        if not path.exists():
            continue
        try:
            overlay = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {path}: {exc}") from exc
        if isinstance(overlay, dict):
            merged = _deep_merge(merged, overlay)
            sources.append(str(path))

    config = from_jsonable(merged, HarnessConfig)
    config.sources = sources
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


def write_example(path: Path) -> Path:
    """Write a fully-populated example configuration."""
    example = to_jsonable(default_config())
    example["routing"] = {
        "default": "host",
        "supervisor": "host",
        "planning": "host",
        "analysis": "host",
        "analysis.security": "openrouter:anthropic/claude-opus-4.1|host",
        "analysis.architecture": "host",
        "synthesis": "host",
        "execution": "host",
        "verification": "host",
        "drift": "ollama:qwen3.8-code:latest|host",
        "improvement": "ollama:qwen3.8-code:latest|host",
    }
    example.pop("sources", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(example, indent=2) + "\n", encoding="utf-8")
    return path
