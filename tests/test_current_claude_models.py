"""Calling current Claude models, and counting what every call costs.

Four defects, found while planning an evaluation that runs the harness
unattended on Claude Opus 5.5 and Sonnet 5.5:

* **Every request carried ``temperature: 0.2``.** Current Claude models reject
  sampling parameters with HTTP 400, so the Anthropic and Bedrock providers
  could not call them at all. ``temperature`` is now optional, sent only when a
  route sets one; the OpenAI-compatible and Ollama providers keep 0.2 as their
  default, so nothing changes for them.
* **A refusal read as an empty answer.** ``stop_reason: "refusal"`` came back
  as a response with no text, which the drift heuristics then scored as an
  agent that did nothing, and sent back for another paid attempt. It is now
  :class:`ProviderRefusal`: never retried, never passed down a fallback chain,
  and recorded on the log as a refusal.
* **An autonomous turn was billed as one call.** Only the answering round's
  usage reached the record, so the tool rounds before it -- reading the files
  the answer is about -- were free as far as the run knew. And a model could
  write its own ``usage`` into its answer and have it believed.
* **Supervisor-side calls were not counted at all.** Planning, synthesis,
  checkpoint, improvement and drift second opinions spent tokens the run's
  total never saw.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import AgentStatus, RunMode, Usage
from supervisor_harness.providers.anthropic import AnthropicProvider
from supervisor_harness.providers.base import (
    DEFAULT_TEMPERATURE,
    ChatMessage,
    CompletionRequest,
    CompletionResponse,
    Provider,
    ProviderError,
    ProviderRefusal,
)
from supervisor_harness.providers.bedrock import BedrockProvider
from supervisor_harness.providers.ollama import OllamaProvider
from supervisor_harness.providers.openrouter import OpenRouterProvider
from supervisor_harness.providers.router import ModelRouter
from supervisor_harness.store.events import EventType

from .conftest import FakeProvider

# -- the wire ----------------------------------------------------------------


class Wire:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return self.responses.pop(0) if self.responses else httpx.Response(200, json={})

    def attach(self, provider: Provider) -> Provider:
        provider._client = httpx.AsyncClient(  # type: ignore[attr-defined]
            transport=httpx.MockTransport(self),
            base_url=getattr(provider, "base_url", "http://test"),
        )
        return provider


def anthropic_answer(**overrides: Any) -> httpx.Response:
    payload: dict[str, Any] = {
        "content": [{"type": "text", "text": '{"ok": true}'}],
        "model": "claude-opus-5-5",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 5},
    }
    payload.update(overrides)
    return httpx.Response(200, json=payload)


def request(**kwargs: Any) -> CompletionRequest:
    return CompletionRequest(messages=[ChatMessage("user", "the brief")], **kwargs)


async def test_no_sampling_parameter_is_sent_unless_a_route_sets_one() -> None:
    """The defect that made every current Claude model unreachable."""
    wire = Wire(anthropic_answer(), anthropic_answer())
    provider = wire.attach(AnthropicProvider(api_key="k"))

    await provider.complete(request())
    await provider.complete(request(temperature=0.7))

    assert "temperature" not in wire.bodies[0]
    assert wire.bodies[1]["temperature"] == 0.7, "an explicit setting still reaches the wire"


async def test_the_default_leaves_room_for_thinking_and_names_a_current_model() -> None:
    wire = Wire(anthropic_answer())
    provider = wire.attach(AnthropicProvider(api_key="k"))

    await provider.complete(request())

    assert wire.bodies[0]["model"] == "claude-opus-5-5"
    assert wire.bodies[0]["max_tokens"] >= 16_000


async def test_a_refusal_is_raised_as_a_refusal_with_its_category() -> None:
    wire = Wire(anthropic_answer(
        content=[], stop_reason="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": "declined"},
    ))
    provider = wire.attach(AnthropicProvider(api_key="k"))

    with pytest.raises(ProviderRefusal) as caught:
        await provider.complete(request())

    assert caught.value.category == "cyber"
    assert caught.value.retryable is False
    assert "cyber" in str(caught.value)


async def test_caching_is_asked_for_only_on_a_conversation_that_will_be_resent() -> None:
    """A cache write costs more than plain input; on a one-shot call it is never read."""
    wire = Wire(anthropic_answer(), anthropic_answer())
    provider = wire.attach(AnthropicProvider(api_key="k"))

    await provider.complete(request())
    await provider.complete(request(cache=True))

    assert "cache_control" not in wire.bodies[0]
    assert wire.bodies[1]["cache_control"] == {"type": "ephemeral"}


async def test_cache_traffic_is_counted_apart_from_input() -> None:
    wire = Wire(anthropic_answer(usage={
        "input_tokens": 10, "output_tokens": 5,
        "cache_read_input_tokens": 900, "cache_creation_input_tokens": 40,
    }))
    provider = wire.attach(AnthropicProvider(api_key="k"))

    usage = (await provider.complete(request(cache=True))).usage

    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (10, 900, 40)


@pytest.mark.parametrize("make", [
    lambda: OpenRouterProvider(api_key="k"),
    lambda: OllamaProvider(),
])
async def test_the_other_providers_keep_the_old_default_temperature(make: Any) -> None:
    """Nothing changes for routes that were working: they still send 0.2."""
    answer = httpx.Response(200, json={
        "choices": [{"message": {"content": "{}"}}], "usage": {},
        "message": {"content": "{}"},
    })
    wire = Wire(answer)
    provider = wire.attach(make())

    await provider.complete(request())

    body = wire.bodies[0]
    sent = body.get("temperature", body.get("options", {}).get("temperature"))
    assert sent == DEFAULT_TEMPERATURE


async def test_bedrock_sends_no_temperature_and_raises_a_refusal() -> None:
    refused = SimpleNamespace(stop_reason="refusal", model="claude-opus-5-5", content=[],
                              stop_details=SimpleNamespace(category="cyber", explanation=""))
    calls: list[dict[str, Any]] = []

    class Messages:
        async def create(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return refused

    provider = BedrockProvider(region="eu-west-1")
    provider._client = SimpleNamespace(messages=Messages())  # type: ignore[assignment]

    with pytest.raises(ProviderRefusal, match="cyber"):
        await provider.complete(request(model="claude-opus-5-5"))
    assert "temperature" not in calls[0]


# -- the router --------------------------------------------------------------


class Scripted(Provider):
    def __init__(self, name: str, outcome: Any) -> None:
        self.name = name
        self.outcome = outcome
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return CompletionResponse(text="{}", provider=self.name)


def router_with(config: Any, **providers: Provider) -> ModelRouter:
    router = ModelRouter(config)
    for name, provider in providers.items():
        router.register(name, provider)
    return router


async def test_a_refusal_is_neither_retried_nor_passed_down_the_fallback_chain(
    config: Any,
) -> None:
    """A fallback route is for outages. Using it to get past a refusal would
    silently change which model did the work."""
    first = Scripted("first", ProviderRefusal("first", category="cyber"))
    second = Scripted("second", None)
    config.routing = {"default": "first:m|second:m"}
    router = router_with(config, first=first, second=second)

    with pytest.raises(ProviderRefusal):
        await router.complete("analysis", request(), retries=3)

    assert len(first.requests) == 1
    assert second.requests == []


async def test_an_outage_still_falls_back(config: Any) -> None:
    first = Scripted("first", ProviderError("first", "HTTP 529", retryable=True))
    second = Scripted("second", None)
    config.routing = {"default": "first:m|second:m"}
    router = router_with(config, first=first, second=second)

    await router.complete("analysis", request(), retries=0)

    assert len(second.requests) == 1


async def test_a_route_can_still_set_a_temperature_and_otherwise_sends_none(config: Any) -> None:
    plain = Scripted("plain", None)
    config.routing = {"default": "plain:m"}
    router = router_with(config, plain=plain)

    await router.complete("analysis", request(cache=True))
    config.providers.setdefault("plain", type(next(iter(config.providers.values())))())
    config.providers["plain"].params = {"temperature": "0.4"}
    await router.complete("analysis", request())

    assert plain.requests[0].temperature is None
    assert plain.requests[0].cache is True, "the cache flag survives routing"
    assert plain.requests[1].temperature == 0.4


# -- the autonomous loop -----------------------------------------------------


class ToolReading(FakeProvider):
    """An analysis agent that reads a file before answering, claiming a usage figure."""

    def __init__(self) -> None:
        super().__init__()
        self.analysis_rounds: dict[str, int] = {}

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if self._classify(request) != "analysis":
            return await super().complete(request)
        key = request.messages[0].content[:200]
        self.analysis_rounds[key] = self.analysis_rounds.get(key, 0) + 1
        if self.analysis_rounds[key] == 1:
            payload: dict[str, Any] = {
                "tool_calls": [{"tool": "read_file", "args": {"path": "src/auth/login.py"}}],
            }
        else:
            payload = {**self._analysis(request), "usage": {"input_tokens": 1}}
        return CompletionResponse(text=json.dumps(payload), model="fake-1", provider="fake",
                                  usage=Usage(input_tokens=100, output_tokens=50,
                                              cache_read_tokens=30))


def run_with(supervisor: Supervisor, provider: FakeProvider) -> Any:
    supervisor.router.register("fake", provider)
    return supervisor


async def test_a_turn_is_billed_for_every_round_and_the_model_cannot_report_its_own(
    supervisor: Supervisor,
) -> None:
    run_with(supervisor, ToolReading())

    response = await supervisor.run("Review src/auth for security problems",
                                    mode=RunMode.REPORT)
    state = supervisor.store.open(response.run_id).state

    turns = [t for t in state.turns if state.agents[t.agent_id].kind.value == "analysis"]
    assert turns
    for turn in turns:
        assert turn.usage.input_tokens == 200, "two rounds of 100, not the model's claimed 1"
        assert turn.usage.cache_read_tokens == 60


async def test_supervisor_side_calls_are_counted_in_the_run_total(
    supervisor: Supervisor,
) -> None:
    response = await supervisor.run("Review src/auth for security problems",
                                    mode=RunMode.REPORT)
    session = supervisor.store.open(response.run_id)
    state = session.state

    recorded = [e for e in session.store.log(state.id).read()
                if e.type is EventType.USAGE_RECORDED]
    stages = {e.payload["stage"] for e in recorded}
    assert {"planning", "synthesis"} <= stages
    assert state.usage.get("stage:synthesis", Usage()).input_tokens > 0
    agents_only = sum(u.input_tokens for k, u in state.usage.items() if not k.startswith("stage:"))
    assert state.total_usage().input_tokens > agents_only


class Refusing(FakeProvider):
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        brief = request.messages[0].content[:400].lower()
        if self._classify(request) == "analysis" and "security" in brief:
            raise ProviderRefusal("fake", model="fake-1", category="cyber")
        return await super().complete(request)


async def test_a_refused_agent_is_recorded_as_refused_and_the_run_goes_on(
    supervisor: Supervisor,
) -> None:
    run_with(supervisor, Refusing())

    response = await supervisor.run("Review src/auth for security problems",
                                    mode=RunMode.REPORT)
    session = supervisor.store.open(response.run_id)
    state = session.state

    notes = [e for e in session.store.log(state.id).read()
             if e.type is EventType.NOTE and e.payload.get("refusal")]
    assert notes
    assert len({n.actor for n in notes}) == len(notes), "one refusal per agent, never retried"
    for note in notes:
        assert note.payload["category"] == "cyber"
        assert state.agents[note.actor].status is AgentStatus.FAILED
    assert response.action == "complete", "one refusal does not end the run"


class Truncating(FakeProvider):
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        response = await super().complete(request)
        if self._classify(request) == "analysis":
            response.finish_reason = "max_tokens"
        return response


async def test_a_truncated_answer_says_so_on_the_log(supervisor: Supervisor) -> None:
    run_with(supervisor, Truncating())

    response = await supervisor.run("Review src/auth for security problems",
                                    mode=RunMode.REPORT)
    session = supervisor.store.open(response.run_id)

    assert any(e.type is EventType.NOTE and "truncated" in str(e.payload.get("text"))
               for e in session.store.log(response.run_id).read())


def test_usage_folds_cache_fields_and_old_logs_still_read(tmp_path: Path) -> None:
    """A log written before these fields existed must still fold."""
    from supervisor_harness.serde import from_jsonable

    old = from_jsonable({"input_tokens": 3, "output_tokens": 4}, Usage)
    assert (old.cache_read_tokens, old.cache_write_tokens) == (0, 0)
    assert old.add(Usage(cache_read_tokens=5)).cache_read_tokens == 5
