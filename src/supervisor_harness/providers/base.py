"""Provider abstraction.

Every stage of a run -- analysis, synthesis, drift checking, execution,
verification, improvement -- resolves to a :class:`Provider` plus a model name,
so a cheap local model can watch for drift while a strong hosted model does the
architecture pass.
"""

from __future__ import annotations

import abc
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..models import Usage


class ProviderError(RuntimeError):
    """A provider could not fulfil a request."""

    def __init__(self, provider: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.retryable = retryable


class DelegationRequired(Exception):
    """Raised by the host provider: the calling host must run this turn itself.

    Carries the payload the host agent (Claude Code or Cursor) needs in order to
    execute the turn with its own tools and report the result back.
    """

    def __init__(self, packet: dict[str, Any]) -> None:
        super().__init__("host delegation required")
        self.packet = packet


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class CompletionRequest:
    messages: list[ChatMessage] = field(default_factory=list)
    system: str = ""
    model: str = ""
    temperature: float = 0.2
    max_tokens: int = 4096
    stop: list[str] = field(default_factory=list)
    json_schema: dict[str, Any] | None = None   # ask for structured output
    timeout: float = 180.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompletionResponse:
    text: str = ""
    reasoning: str = ""   # extended-thinking / reasoning channel, when exposed
    model: str = ""
    provider: str = ""
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def json(self, *, required: bool = True) -> dict[str, Any]:
        """Parse the response as JSON, tolerating fences and surrounding prose."""
        parsed = extract_json(self.text)
        if parsed is None:
            if required:
                preview = self.text[:400].replace("\n", " ")
                raise ProviderError(self.provider, f"expected JSON, got: {preview!r}")
            return {}
        return parsed


class Provider(abc.ABC):
    """Minimal surface: one non-streaming completion, plus a health check."""

    name: str = "provider"

    @abc.abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Run a single completion."""

    async def available(self) -> bool:
        """Whether this provider is usable right now (credentials, reachability)."""
        return True

    def describe(self) -> dict[str, Any]:
        return {"name": self.name}

    async def aclose(self) -> None:  # noqa: B027 - an optional hook, not a contract
        """Release any held connections.

        Deliberately concrete and empty rather than abstract: a provider that
        holds no connection has nothing to close, and requiring every one to
        say so would be ceremony. Subclasses that own a client override it.
        """


# --------------------------------------------------------------------------
# JSON extraction
# --------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model response.

    Models wrap JSON in fences, prefix it with commentary, or append a summary.
    Tried in order: whole string, fenced block, then the first balanced ``{...}``
    span found by scanning while respecting string literals and escapes.
    """
    if not text:
        return None
    candidates: list[str] = [text.strip()]
    candidates.extend(m.group(1).strip() for m in _FENCE.finditer(text))
    span = _balanced_span(text)
    if span:
        candidates.append(span)

    for candidate in candidates:
        if not candidate:
            continue
        # strict=False permits the literal newlines and tabs models routinely
        # leave inside string values. Rejecting those lost otherwise complete
        # answers over a transport detail.
        for strict in (True, False):
            try:
                parsed = json.loads(candidate, strict=strict)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"items": parsed}
    return None


def _balanced_span(text: str) -> str | None:
    """Return the first brace-balanced substring, ignoring braces inside strings."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def schema_instruction(schema: dict[str, Any]) -> str:
    """Prompt fragment used when a provider has no native structured-output mode."""
    return (
        "Respond with a single JSON object and nothing else -- no prose, no code "
        "fence, no trailing commentary. It must conform to this JSON Schema:\n"
        f"{json.dumps(schema, indent=2)}"
    )
