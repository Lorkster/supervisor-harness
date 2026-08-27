"""Stage-to-model routing with fallbacks.

Callers ask for a *stage* ("analysis.security", "drift", "verification"), never a
provider. That keeps model choice a configuration concern and lets a run degrade
gracefully when a provider is unreachable.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..config import HarnessConfig, ProviderConfig
from ..models import ModelBinding
from .anthropic import AnthropicProvider
from .base import (
    ChatMessage,
    CompletionRequest,
    CompletionResponse,
    DelegationRequired,
    Provider,
    ProviderError,
)
from .host import HostProvider
from .ollama import OllamaProvider
from .openrouter import OpenRouterProvider


def build_provider(name: str, cfg: ProviderConfig, host_name: str = "unknown") -> Provider:
    """Instantiate one provider from its configuration."""
    kind = (cfg.type or name).lower()
    if kind == "ollama":
        return OllamaProvider(
            base_url=cfg.base_url or None,
            default_model=cfg.default_model or "qwen3.8-code:latest",
        )
    if kind == "openrouter":
        return OpenRouterProvider(
            api_key=cfg.resolved_key() or None,
            base_url=cfg.base_url or "https://openrouter.ai/api/v1",
            default_model=cfg.default_model or "anthropic/claude-sonnet-4.5",
        )
    if kind == "anthropic":
        return AnthropicProvider(
            api_key=cfg.resolved_key() or None,
            base_url=cfg.base_url or "https://api.anthropic.com",
            default_model=cfg.default_model or "claude-sonnet-4-5",
        )
    if kind == "host":
        return HostProvider(host_name=host_name)
    raise ValueError(f"unknown provider type: {kind!r} (for provider {name!r})")


class ModelRouter:
    """Resolves stages to providers and runs completions against them."""

    def __init__(self, config: HarnessConfig, host_name: str = "unknown") -> None:
        self.config = config
        self.host_name = host_name
        self._providers: dict[str, Provider] = {}

    # -- resolution --------------------------------------------------------

    def register(self, name: str, provider: Provider) -> None:
        """Install a provider instance directly, bypassing configuration.

        Used by tests and by embedders that construct providers themselves.
        """
        self._providers[name] = provider

    def provider(self, name: str) -> Provider:
        if name not in self._providers:
            cfg = self.config.providers.get(name)
            if cfg is None:
                if name == "host":
                    cfg = ProviderConfig(type="host")
                else:
                    raise ValueError(f"provider {name!r} is not configured")
            if not cfg.enabled:
                raise ValueError(f"provider {name!r} is disabled")
            self._providers[name] = build_provider(name, cfg, self.host_name)
        return self._providers[name]

    def binding(self, stage: str) -> ModelBinding:
        return self.config.binding_for(stage)

    def is_host(self, stage: str) -> bool:
        return self.binding(stage).provider == "host"

    # -- execution ---------------------------------------------------------

    async def complete(
        self,
        stage: str,
        request: CompletionRequest,
        *,
        binding: ModelBinding | None = None,
        retries: int = 1,
    ) -> CompletionResponse:
        """Complete against the stage's binding, falling back on failure.

        :class:`DelegationRequired` is control flow, not failure: it propagates
        immediately rather than triggering the fallback chain.
        """
        binding = binding or self.binding(stage)
        chain: list[tuple[str, str]] = [(binding.provider, binding.model)]
        for ref in binding.fallbacks:
            prov, _, model = ref.partition(":")
            chain.append((prov or "host", model))

        errors: list[str] = []
        for provider_name, model in chain:
            try:
                provider = self.provider(provider_name)
            except ValueError as exc:
                errors.append(str(exc))
                continue

            attempt_request = _with_model(request, model, binding.params)
            for attempt in range(retries + 1):
                try:
                    return await provider.complete(attempt_request)
                except DelegationRequired:
                    raise
                except ProviderError as exc:
                    errors.append(str(exc))
                    if exc.retryable and attempt < retries:
                        await asyncio.sleep(0.6 * (attempt + 1))
                        continue
                    break
                except Exception as exc:  # noqa: BLE001 - provider bugs must not kill a run
                    errors.append(f"[{provider_name}] unexpected: {exc}")
                    break

        raise ProviderError(
            binding.provider,
            f"stage {stage!r} failed on every route ({' -> '.join(p for p, _ in chain)}): "
            + " | ".join(errors[-3:]),
        )

    async def health(self) -> dict[str, Any]:
        """Report which configured providers are actually usable."""
        out: dict[str, Any] = {}
        for name, cfg in self.config.providers.items():
            if not cfg.enabled:
                out[name] = {"enabled": False}
                continue
            try:
                provider = self.provider(name)
                out[name] = {
                    "enabled": True,
                    "available": await provider.available(),
                    **provider.describe(),
                }
            except Exception as exc:  # noqa: BLE001 - health must never raise
                out[name] = {"enabled": True, "available": False, "error": str(exc)}
        return out

    async def aclose(self) -> None:
        await asyncio.gather(
            *(p.aclose() for p in self._providers.values()), return_exceptions=True
        )
        self._providers.clear()


def _with_model(
    request: CompletionRequest, model: str, params: dict[str, Any]
) -> CompletionRequest:
    """Copy the request with the route's model and provider params applied.

    Sampling knobs are lifted onto the typed fields rather than passed through
    ``extra``, because providers place them differently in the wire format
    (top level for OpenAI-compatible APIs, under ``options`` for Ollama).
    """
    params = dict(params)
    temperature = float(params.pop("temperature", request.temperature))
    max_tokens = int(params.pop("max_tokens", request.max_tokens))
    timeout = float(params.pop("timeout", request.timeout))

    extra = params
    extra.update(request.extra)
    return CompletionRequest(
        messages=request.messages,
        system=request.system,
        model=model or request.model,
        temperature=temperature,
        max_tokens=max_tokens,
        stop=request.stop,
        json_schema=request.json_schema,
        timeout=timeout,
        extra=extra,
    )


__all__ = [
    "ChatMessage",
    "CompletionRequest",
    "CompletionResponse",
    "DelegationRequired",
    "ModelRouter",
    "Provider",
    "ProviderError",
    "build_provider",
]
