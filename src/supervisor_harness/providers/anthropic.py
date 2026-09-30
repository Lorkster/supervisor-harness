"""Anthropic Messages API provider.

Used when the harness should call the same cloud provider the host runs on, but
directly -- for example to give the supervisor a stronger model than the agents,
or to run autonomous agents outside a Claude Code session.

Three things about current Claude models shape this file. They reject sampling
parameters with HTTP 400, so ``temperature`` is sent only when a route sets it.
They can decline a request with ``stop_reason: "refusal"``, which is raised as
:class:`ProviderRefusal` rather than returned as an empty answer. And they think
on every request, billed as output, which is why the default ``max_tokens`` is
large. Prompt caching is requested only for conversations the caller says it
will resend (``CompletionRequest.cache``).
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from ..models import Usage
from .base import (
    DEFAULT_MAX_TOKENS,
    CompletionRequest,
    CompletionResponse,
    Provider,
    ProviderError,
    ProviderRefusal,
    schema_instruction,
)

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        default_model: str = "claude-opus-5-5",
    ) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": API_VERSION,
                    "content-type": "application/json",
                },
            )
        return self._client

    async def available(self) -> bool:
        return bool(self.api_key)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if not self.api_key:
            raise ProviderError(self.name, "ANTHROPIC_API_KEY is not set")

        system = request.system
        if request.json_schema:
            system = f"{system}\n\n{schema_instruction(request.json_schema)}".strip()

        # The Messages API requires alternating turns starting with "user".
        messages = [
            {"role": "assistant" if m.role == "assistant" else "user", "content": m.content}
            for m in request.messages
            if m.content.strip()
        ]
        if not messages:
            messages = [{"role": "user", "content": "Proceed."}]
        if messages[0]["role"] != "user":
            messages.insert(0, {"role": "user", "content": "Proceed."})

        body: dict[str, Any] = {
            "model": request.model or self.default_model,
            "max_tokens": request.max_tokens or DEFAULT_MAX_TOKENS,
            "messages": messages,
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.cache:
            # Automatic caching: the breakpoint moves to the end of each request,
            # so every round of an agent's turn reads the prefix the last one wrote.
            body["cache_control"] = {"type": "ephemeral"}
        if system:
            body["system"] = system
        if request.stop:
            body["stop_sequences"] = request.stop
        body.update(request.extra)

        try:
            resp = await self._http().post("/v1/messages", json=body, timeout=request.timeout)
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"request failed: {exc}", retryable=True) from exc

        if resp.status_code >= 400:
            raise ProviderError(
                self.name,
                f"HTTP {resp.status_code}: {resp.text[:300]}",
                retryable=resp.status_code in (408, 429, 500, 502, 503, 504, 529),
            )

        data = resp.json()
        if data.get("stop_reason") == "refusal":
            details = data.get("stop_details") or {}
            raise ProviderRefusal(
                self.name,
                model=str(data.get("model", body["model"])),
                category=str(details.get("category") or ""),
                explanation=str(details.get("explanation") or ""),
            )
        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        usage_raw = data.get("usage") or {}
        reasoning = "".join(
            block.get("thinking", "")
            for block in data.get("content", [])
            if block.get("type") == "thinking"
        )
        return CompletionResponse(
            text=text,
            reasoning=reasoning,
            model=data.get("model", body["model"]),
            provider=self.name,
            usage=Usage(
                input_tokens=int(usage_raw.get("input_tokens", 0) or 0),
                output_tokens=int(usage_raw.get("output_tokens", 0) or 0),
                cache_read_tokens=int(usage_raw.get("cache_read_input_tokens", 0) or 0),
                cache_write_tokens=int(usage_raw.get("cache_creation_input_tokens", 0) or 0),
            ),
            finish_reason=data.get("stop_reason", ""),
            raw=data,
        )

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "base_url": self.base_url,
                "default_model": self.default_model, "configured": bool(self.api_key)}

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = ["AnthropicProvider"]
