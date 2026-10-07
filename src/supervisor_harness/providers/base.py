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

from ..assists import record_assist
from ..models import Usage


class ProviderError(RuntimeError):
    """A provider could not fulfil a request."""

    def __init__(self, provider: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.retryable = retryable


class ProviderRefusal(ProviderError):
    """The model declined the request: a policy decision, not a failure.

    Never retried, and never passed down a fallback chain. Retrying a refusal
    until it goes away is circumventing a safeguard, and routing it to another
    model silently changes which model did the work -- a fallback route is for
    outages. It is raised so the caller records a refusal *as* a refusal,
    rather than reading an empty answer as an agent that did nothing and
    sending it back for another paid attempt.
    """

    def __init__(self, provider: str, *, model: str = "", category: str = "",
                 explanation: str = "") -> None:
        detail = f"refused by {model or 'the model'}"
        if category:
            detail += f" ({category})"
        if explanation:
            detail += f": {explanation}"
        super().__init__(provider, detail, retryable=False)
        self.model = model
        self.category = category
        self.explanation = explanation


class DelegationRequired(Exception):
    """Raised by the host provider: the calling host must run this turn itself.

    Carries the payload the host agent (Claude Code or Cursor) needs in order to
    execute the turn with its own tools and report the result back.
    """

    def __init__(self, packet: dict[str, Any]) -> None:
        super().__init__("host delegation required")
        self.packet = packet


@dataclass
class ToolCall:
    """One call a model made through its provider's native tool interface."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = ""


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    #: On an assistant message: the native tool calls it made.
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: On a tool message: which tool's result this is, and the call it answers.
    tool_name: str = ""
    tool_call_id: str = ""


#: The sampling temperature the OpenAI-compatible and Ollama providers use when
#: a route sets none -- what every request carried before temperature became
#: optional, kept so those routes behave exactly as they did.
DEFAULT_TEMPERATURE = 0.2

#: Room for an answer *and* the reasoning before it. Current Claude models think
#: on every request and bill the thinking as output, so the old 4096 could be
#: spent before the answer began; a truncated answer is a failed turn that was
#: paid for, and then usually paid for again.
DEFAULT_MAX_TOKENS = 16_000


@dataclass
class CompletionRequest:
    messages: list[ChatMessage] = field(default_factory=list)
    system: str = ""
    model: str = ""
    # ``None`` means "the provider's own default". The Anthropic Messages API
    # rejects sampling parameters on current models (HTTP 400), so it must be
    # possible not to send one at all; a route that wants one sets it in params.
    temperature: float | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    stop: list[str] = field(default_factory=list)
    json_schema: dict[str, Any] | None = None   # ask for structured output
    timeout: float = 180.0
    extra: dict[str, Any] = field(default_factory=dict)
    #: The caller will resend this conversation with more appended -- an agent's
    #: tool rounds and turns -- so caching its prefix pays. One-shot calls leave
    #: it off: a cache write costs more than plain input and would never be read.
    cache: bool = False
    #: Tools offered through the provider's native interface, as JSON-schema
    #: function specs (``{"name", "description", "parameters"}``). Only for a
    #: provider whose ``native_tools`` is true; the others never see it.
    tools: list[dict[str, Any]] | None = None
    #: Sample with the model's own tuned settings rather than the harness's
    #: default temperature. A local model ships with the sampling its makers
    #: tested; 0.2 is what induced repetition loops in one.
    model_sampling: bool = False


@dataclass
class CompletionResponse:
    text: str = ""
    reasoning: str = ""   # extended-thinking / reasoning channel, when exposed
    model: str = ""
    provider: str = ""
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    #: Native tool calls, when the request offered tools and the model used them.
    tool_calls: list[ToolCall] = field(default_factory=list)

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
    #: Whether ``complete`` honours ``CompletionRequest.tools`` and returns
    #: ``CompletionResponse.tool_calls``. An agent on a provider without it is
    #: driven through the JSON turn contract instead.
    native_tools: bool = False

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

    for index, candidate in enumerate(candidates):
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
            # Every branch below this point is the harness helping. Counted
            # rather than merely done, so a provider that needs its JSON dug
            # out of prose on every turn is visible in the report instead of
            # being absorbed silently -- see `assists`.
            if index > 0:
                record_assist("json_from_prose", candidate[:120])
            if not strict:
                record_assist("json_non_strict", candidate[:120])
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                record_assist("json_list_wrapped", f"{len(parsed)} item(s)")
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
