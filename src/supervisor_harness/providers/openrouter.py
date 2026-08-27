"""OpenRouter provider (OpenAI-compatible chat completions)."""

from __future__ import annotations

import os
from typing import Any

import httpx

from ..models import Usage
from .base import (
    ChatMessage,
    CompletionRequest,
    CompletionResponse,
    Provider,
    ProviderError,
    schema_instruction,
)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(Provider):
    name = "openrouter"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        default_model: str = "anthropic/claude-sonnet-4.5",
        referer: str = "https://github.com/Lorkster/supervisor-harness",
        title: str = "supervisor-harness",
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.referer = referer
        self.title = title
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "HTTP-Referer": self.referer,
                    "X-Title": self.title,
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def available(self) -> bool:
        return bool(self.api_key)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if not self.api_key:
            raise ProviderError(self.name, "OPENROUTER_API_KEY is not set")

        messages: list[dict[str, str]] = []
        system = request.system
        if request.json_schema:
            system = f"{system}\n\n{schema_instruction(request.json_schema)}".strip()
        if system:
            messages.append({"role": "system", "content": system})
        messages.extend({"role": m.role, "content": m.content} for m in request.messages)

        body: dict[str, Any] = {
            "model": request.model or self.default_model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.stop:
            body["stop"] = request.stop
        if request.json_schema:
            # Providers that ignore this still get the schema via the system prompt.
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "strict": False,
                    "schema": request.json_schema,
                },
            }
        body.update(request.extra)

        try:
            resp = await self._http().post("/chat/completions", json=body, timeout=request.timeout)
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"request failed: {exc}", retryable=True) from exc

        if resp.status_code >= 400:
            raise ProviderError(
                self.name,
                f"HTTP {resp.status_code}: {resp.text[:300]}",
                retryable=resp.status_code in (408, 429, 500, 502, 503, 504),
            )

        data = resp.json()
        if "error" in data and not data.get("choices"):
            raise ProviderError(self.name, str(data["error"])[:300], retryable=True)
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(self.name, f"unexpected response shape: {str(data)[:300]}") from exc

        usage_raw = data.get("usage") or {}
        return CompletionResponse(
            text=text,
            reasoning=choice["message"].get("reasoning", "") or "",
            model=data.get("model", body["model"]),
            provider=self.name,
            usage=Usage(
                input_tokens=int(usage_raw.get("prompt_tokens", 0)),
                output_tokens=int(usage_raw.get("completion_tokens", 0)),
            ),
            finish_reason=choice.get("finish_reason", ""),
            raw=data,
        )

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "base_url": self.base_url,
                "default_model": self.default_model, "configured": bool(self.api_key)}

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = ["OpenRouterProvider", "ChatMessage"]
