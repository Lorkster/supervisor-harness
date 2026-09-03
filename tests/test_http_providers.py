"""The three HTTP providers: what they send, and what they make of the answer.

Finding **Q-C3** of `docs/quality-assessment.md`. `openrouter` sat at 26%,
`ollama` at 27% and `anthropic` at 30% -- request building, response mapping and
error translation almost entirely unexercised, while `providers/bedrock.py`,
written to the current bar, was at 93%.

The contrast is the point. These three predate the bar rather than fall short of
it, and the template is `test_bedrock_provider.py`: a stub in place of the
network, and assertions on the two things a provider is actually for --

* **what reaches the wire**, because a dropped schema instruction or a lost
  `stop` sequence surfaces far away, as a parse failure blamed on the model;
* **what comes back**, because usage is what every budget and token ceiling in
  the harness is measured in, and a provider that reports zero is an agent with
  no ceiling at all.

Error translation gets the same treatment as Bedrock's: the router's fallback
chain only retries what is marked retryable, so collapsing that would silently
disable `|` fallback for throttling -- the case it exists for.

`httpx.MockTransport` stands in for the network. No test here opens a socket.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from supervisor_harness.providers.anthropic import AnthropicProvider
from supervisor_harness.providers.base import (
    ChatMessage,
    CompletionRequest,
    Provider,
    ProviderError,
)
from supervisor_harness.providers.ollama import OllamaProvider
from supervisor_harness.providers.openrouter import OpenRouterProvider

SCHEMA: dict[str, Any] = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


class Wire:
    """Records the requests a provider makes and answers them from a script."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0) if self.responses else httpx.Response(200, json={})

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)

    def attach(self, provider: Provider, **client: Any) -> Provider:
        # `base_url` matters: every provider posts to a relative path, and its
        # own `_http()` is what normally supplies the origin. Injecting a client
        # without one makes `/api/tags` an unresolvable URL rather than a
        # request the transport can answer.
        client.setdefault("base_url", getattr(provider, "base_url", "http://test"))
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(self), **client)
        return provider


def _json(payload: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _anthropic_ok(text: str = '{"ok": true}') -> httpx.Response:
    return _json({
        "content": [{"type": "thinking", "thinking": "weighing it"},
                    {"type": "text", "text": text}],
        "model": "claude-sonnet-4-5",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 5},
    })


def _openrouter_ok(text: str = '{"ok": true}') -> httpx.Response:
    return _json({
        "choices": [{"message": {"content": text, "reasoning": "weighing it"}}],
        "model": "anthropic/claude-sonnet-4.5",
        "usage": {"prompt_tokens": 12, "completion_tokens": 5},
    })


def _ollama_ok(text: str = '{"ok": true}') -> httpx.Response:
    return _json({
        "message": {"content": text, "thinking": "weighing it"},
        "model": "qwen3.8-code:latest",
        "prompt_eval_count": 12,
        "eval_count": 5,
    })


def _request(**kwargs: Any) -> CompletionRequest:
    base: dict[str, Any] = {
        "messages": [ChatMessage("user", "the brief")],
        "system": "you are a supervised agent",
        "temperature": 0.3,
        "max_tokens": 1234,
    }
    base.update(kwargs)
    return CompletionRequest(**base)


# -- what reaches the wire --------------------------------------------------


async def test_anthropic_sends_the_schema_instruction_and_the_sampling_knobs() -> None:
    wire = Wire(_anthropic_ok())
    provider = wire.attach(AnthropicProvider(api_key="k"))

    response = await provider.complete(_request(stop=["STOP"], json_schema=SCHEMA))

    body = wire.body
    assert body["model"] == "claude-sonnet-4-5"
    assert body["max_tokens"] == 1234
    assert body["temperature"] == 0.3
    assert body["stop_sequences"] == ["STOP"]
    assert body["messages"] == [{"role": "user", "content": "the brief"}]
    assert "JSON Schema" in body["system"], "the schema instruction was dropped"
    assert response.json() == {"ok": True}


async def test_openrouter_asks_for_structured_output_and_still_says_it_in_words() -> None:
    """Both halves matter: a provider that ignores `response_format` gets the prompt."""
    wire = Wire(_openrouter_ok())
    provider = wire.attach(OpenRouterProvider(api_key="k"))

    await provider.complete(_request(json_schema=SCHEMA))

    body = wire.body
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert "JSON Schema" in body["messages"][0]["content"]
    assert body["messages"][0]["role"] == "system"


async def test_ollama_constrains_generation_and_turns_thinking_off() -> None:
    """`think: False` is not cosmetic.

    A reasoning model given a schema otherwise spends the whole token budget in
    the thinking channel and returns empty content -- which reads as a model
    that answered nothing rather than one that was asked wrongly.
    """
    wire = Wire(_ollama_ok())
    provider = wire.attach(OllamaProvider())

    await provider.complete(_request(stop=["STOP"], json_schema=SCHEMA))

    body = wire.body
    assert body["format"] == SCHEMA
    assert body["think"] is False
    assert body["stream"] is False
    assert body["options"]["temperature"] == 0.3
    assert body["options"]["num_predict"] == 1234
    assert body["options"]["stop"] == ["STOP"]
    assert body["messages"][0] == {"role": "system", "content": "you are a supervised agent"}


@pytest.mark.parametrize(
    ("provider", "responder"),
    [
        (AnthropicProvider(api_key="k"), _anthropic_ok),
        (OpenRouterProvider(api_key="k"), _openrouter_ok),
        (OllamaProvider(), _ollama_ok),
    ],
    ids=["anthropic", "openrouter", "ollama"],
)
async def test_provider_params_reach_the_body(provider: Provider, responder: Any) -> None:
    """`extra` is how routing params reach a request, and it is applied last.

    That ordering is deliberate and is why `params` is in
    `PROTECTED_PROVIDER_KEYS`: a workspace config able to set it could replace
    the conversation. The trusted path still has to work.
    """
    wire = Wire(responder())
    wire.attach(provider)

    await provider.complete(_request(extra={"top_p": 0.9}))

    assert wire.body["top_p"] == 0.9


# -- what comes back --------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "responder", "model"),
    [
        (AnthropicProvider(api_key="k"), _anthropic_ok, "claude-sonnet-4-5"),
        (OpenRouterProvider(api_key="k"), _openrouter_ok, "anthropic/claude-sonnet-4.5"),
        (OllamaProvider(), _ollama_ok, "qwen3.8-code:latest"),
    ],
    ids=["anthropic", "openrouter", "ollama"],
)
async def test_a_response_maps_back_with_its_usage_and_reasoning(
    provider: Provider, responder: Any, model: str
) -> None:
    """Usage is what every budget and ceiling is measured in; zero fails open."""
    wire = Wire(responder("answer"))
    wire.attach(provider)

    response = await provider.complete(_request())

    assert response.text == "answer"
    assert response.reasoning == "weighing it"
    assert response.model == model
    assert response.provider == provider.name
    assert (response.usage.input_tokens, response.usage.output_tokens) == (12, 5)


# -- what goes wrong --------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (503, True), (500, True), (400, False), (401, False), (404, False)],
)
@pytest.mark.parametrize(
    ("provider", "path"),
    [
        (AnthropicProvider(api_key="k"), "anthropic"),
        (OpenRouterProvider(api_key="k"), "openrouter"),
        (OllamaProvider(), "ollama"),
    ],
    ids=["anthropic", "openrouter", "ollama"],
)
async def test_http_errors_keep_their_retryability(
    provider: Provider, path: str, status: int, retryable: bool
) -> None:
    wire = Wire(httpx.Response(status, text="upstream said no"))
    wire.attach(provider)

    with pytest.raises(ProviderError) as caught:
        await provider.complete(_request())

    assert caught.value.retryable is retryable
    assert str(status) in str(caught.value)


@pytest.mark.parametrize(
    "provider",
    [AnthropicProvider(api_key="k"), OpenRouterProvider(api_key="k"), OllamaProvider()],
    ids=["anthropic", "openrouter", "ollama"],
)
async def test_a_transport_failure_is_retryable(provider: Provider) -> None:
    """It carries no status, so the status path cannot classify it."""

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(explode))

    with pytest.raises(ProviderError) as caught:
        await provider.complete(_request())

    assert caught.value.retryable is True


async def test_openrouter_reports_an_error_body_that_arrived_with_status_200() -> None:
    """The shape that looks like success. Nothing else in the suite reaches it."""
    wire = Wire(_json({"error": {"message": "no credits"}}))
    provider = wire.attach(OpenRouterProvider(api_key="k"))

    with pytest.raises(ProviderError) as caught:
        await provider.complete(_request())

    assert "no credits" in str(caught.value)
    assert caught.value.retryable is True


async def test_openrouter_names_an_unexpected_shape_rather_than_raising_keyerror() -> None:
    wire = Wire(_json({"choices": []}))
    provider = wire.attach(OpenRouterProvider(api_key="k"))

    with pytest.raises(ProviderError) as caught:
        await provider.complete(_request())

    assert "unexpected response shape" in str(caught.value)


@pytest.mark.parametrize(
    ("build", "variable"),
    [
        (AnthropicProvider, "ANTHROPIC_API_KEY"),
        (OpenRouterProvider, "OPENROUTER_API_KEY"),
    ],
    ids=["anthropic", "openrouter"],
)
async def test_a_missing_key_is_named_before_a_request_is_made(
    build: Any, variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configuration mistake, reported as one rather than as a 401.

    Both providers fall back to the environment when handed an empty key, so the
    variable is cleared and the provider built *inside* the test. Parametrising
    over constructed instances reads better and is wrong: they would be built at
    collection time, before any fixture could clear anything, and this test then
    passes or fails according to whether the developer happens to have a key
    exported. It found exactly that on the machine it was written on.
    """
    monkeypatch.delenv(variable, raising=False)
    provider = build(api_key="")
    wire = Wire()
    wire.attach(provider)

    assert await provider.available() is False
    with pytest.raises(ProviderError) as caught:
        await provider.complete(_request())

    assert variable in str(caught.value)
    assert wire.requests == [], "a request was sent without a key"


# -- ollama's own health surface --------------------------------------------


async def test_ollama_availability_follows_the_tags_endpoint() -> None:
    """Ollama has no key, so "available" is a question only the daemon answers."""
    up = Wire(httpx.Response(200, json={"models": []}))
    assert await up.attach(OllamaProvider()).available() is True

    down = Wire(httpx.Response(500))
    assert await down.attach(OllamaProvider()).available() is False


async def test_ollama_lists_the_models_it_has_and_survives_a_bad_answer() -> None:
    listed = Wire(httpx.Response(200, json={"models": [{"name": "a"}, {"name": "b"}]}))
    assert await listed.attach(OllamaProvider()).list_models() == ["a", "b"]

    # A daemon answering a different shape must not end the run that asked.
    malformed = Wire(httpx.Response(200, json={"models": [{"tag": "a"}]}))
    assert await malformed.attach(OllamaProvider()).list_models() == []


def test_ollama_accepts_a_bare_host_and_makes_a_url_of_it() -> None:
    """`OLLAMA_HOST=localhost:11434` is the documented spelling and has no scheme."""
    assert OllamaProvider(base_url="localhost:11434").base_url == "http://localhost:11434"
    assert OllamaProvider(base_url="http://box:1234/").base_url == "http://box:1234"
