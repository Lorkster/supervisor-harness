"""Local Ollama provider.

Ollama is the natural home for the high-frequency, low-stakes stages -- drift
checks, message triage, deterministic inspection -- where a local model keeps
cost and latency down without touching the network.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from ..models import Usage
from .base import (
    DEFAULT_TEMPERATURE,
    ChatMessage,
    CompletionRequest,
    CompletionResponse,
    Provider,
    ProviderError,
    ToolCall,
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
    native_tools = True

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
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend(_wire_message(m) for m in request.messages)

        temperature = request.temperature
        options: dict[str, Any] = {}
        if temperature is not None or not request.model_sampling:
            options["temperature"] = DEFAULT_TEMPERATURE if temperature is None else temperature
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
        if request.tools:
            body["tools"] = [{"type": "function", "function": spec} for spec in request.tools]
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
            tool_calls=_tool_calls(message.get("tool_calls")),
        )

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "base_url": self.base_url, "default_model": self.default_model}

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _wire_message(message: ChatMessage) -> dict[str, Any]:
    """A message as Ollama's chat API takes it, tool calls and results included."""
    wire: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        wire["tool_calls"] = [
            {"function": {"name": call.name, "arguments": call.arguments}}
            for call in message.tool_calls
        ]
    if message.tool_name:
        wire["tool_name"] = message.tool_name
    return wire


def _tool_calls(raw: Any) -> list[ToolCall]:
    """The native tool calls in a response, skipping any that are malformed.

    Arguments arrive as an object, or from some models as a JSON string.
    """
    calls: list[ToolCall] = []
    for entry in raw if isinstance(raw, list) else []:
        function = entry.get("function") if isinstance(entry, dict) else None
        if not isinstance(function, dict) or not str(function.get("name", "")).strip():
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        calls.append(ToolCall(name=str(function["name"]).strip(),
                              arguments=arguments if isinstance(arguments, dict) else {},
                              id=str(entry.get("id", ""))))
    return calls


__all__ = ["OllamaProvider"]
