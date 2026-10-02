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
from .base import (
    DEFAULT_TEMPERATURE,
    CompletionRequest,
    CompletionResponse,
    Provider,
    ProviderError,
    schema_instruction,
)

DEFAULT_BASE_URL = "http://localhost:11434"

#: Above this many characters of prompt, the schema is described in the system
#: text and Ollama is asked only for JSON, instead of being handed the schema
#: as a grammar. Constrained decoding collapses on long prompts: measured with
#: qwen3.8-code at 131k context, a 288,000-character review came back as
#: ``{"findings": []}`` in 11 tokens -- an empty answer that reads exactly like
#: "found nothing" -- while the same prompt in JSON mode with the schema
#: described returned three findings, and at half the length the grammar
#: worked. Short prompts keep the grammar, which is stricter where it holds.
GRAMMAR_MAX_PROMPT_CHARS = 100_000


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

        temperature = request.temperature
        options: dict[str, Any] = {
            "temperature": DEFAULT_TEMPERATURE if temperature is None else temperature,
        }
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
            if sum(len(m["content"]) for m in messages) <= GRAMMAR_MAX_PROMPT_CHARS:
                # Ollama constrains generation to the schema when given one directly.
                body["format"] = request.json_schema
            else:
                # Too long for the grammar to hold (see GRAMMAR_MAX_PROMPT_CHARS):
                # JSON mode, with the schema described the way every other
                # provider here describes it.
                body["format"] = "json"
                instruction = schema_instruction(request.json_schema)
                if messages and messages[0]["role"] == "system":
                    messages[0] = {"role": "system",
                                   "content": f"{messages[0]['content']}\n\n{instruction}"}
                else:
                    messages.insert(0, {"role": "system", "content": instruction})
            # Reasoning models otherwise spend the whole token budget in the
            # thinking channel and return empty content. Callers that want the
            # reasoning back can pass think=True explicitly.
            body["think"] = False
        body.update(request.extra)

        try:
            resp = await self._http().post("/api/chat", json=body, timeout=request.timeout)
        except httpx.HTTPError as exc:
            raise ProviderError(
                self.name, f"request failed ({self.base_url}): {exc}", retryable=True
            ) from exc

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
