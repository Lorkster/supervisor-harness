"""Local Ollama provider.

Ollama is the natural home for the high-frequency, low-stakes stages -- drift
checks, message triage, deterministic inspection -- where a local model keeps
cost and latency down without touching the network.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from ..models import Usage
from .base import CompletionRequest, CompletionResponse, Provider, ProviderError

DEFAULT_BASE_URL = "http://localhost:11434"


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(
        self,
        base_url: str | None = None,
        default_model: str = "qwen3.8-code:latest",
        keep_alive: str = "5m",
    ) -> None:
        self.base_url = (base_url or os.environ.get("OLLAMA_HOST") or DEFAULT_BASE_URL).rstrip("/")
        if not self.base_url.startswith("http"):
            self.base_url = f"http://{self.base_url}"
        self.default_model = default_model
        self.keep_alive = keep_alive
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url)
        return self._client

    async def available(self) -> bool:
        try:
            resp = await self._http().get("/api/tags", timeout=3.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def list_models(self) -> list[str]:
        try:
            resp = await self._http().get("/api/tags", timeout=5.0)
            resp.raise_for_status()
            return [m["name"] for m in resp.json().get("models", [])]
        except (httpx.HTTPError, KeyError, TypeError):
            return []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        messages: list[dict[str, str]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend({"role": m.role, "content": m.content} for m in request.messages)

        options: dict[str, Any] = {"temperature": request.temperature}
        if request.max_tokens:
            options["num_predict"] = request.max_tokens
        if request.stop:
            options["stop"] = request.stop

        body: dict[str, Any] = {
            "model": request.model or self.default_model,
            "messages": messages,
            "stream": False,
            "options": options,
            "keep_alive": self.keep_alive,
        }
        if request.json_schema:
            # Ollama constrains generation to the schema when given one directly.
            body["format"] = request.json_schema
            # Reasoning models otherwise spend the whole token budget in the
            # thinking channel and return empty content. Callers that want the
            # reasoning back can pass think=True explicitly.
            body["think"] = False
        body.update(request.extra)

        try:
            resp = await self._http().post("/api/chat", json=body, timeout=request.timeout)
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"request failed ({self.base_url}): {exc}", retryable=True) from exc

        if resp.status_code >= 400:
            raise ProviderError(
                self.name,
                f"HTTP {resp.status_code}: {resp.text[:300]}",
                retryable=resp.status_code in (408, 429, 500, 502, 503, 504),
            )

        data = resp.json()
        message = data.get("message") or {}
        return CompletionResponse(
            text=message.get("content", ""),
            reasoning=message.get("thinking", "") or "",
            model=data.get("model", body["model"]),
            provider=self.name,
            usage=Usage(
                input_tokens=int(data.get("prompt_eval_count", 0)),
                output_tokens=int(data.get("eval_count", 0)),
                seconds=float(data.get("total_duration", 0)) / 1e9,
            ),
            finish_reason=data.get("done_reason", ""),
            raw=data,
        )

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "base_url": self.base_url, "default_model": self.default_model}

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = ["OllamaProvider"]
