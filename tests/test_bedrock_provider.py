"""Autonomous Bedrock: the optional extra, and what must hold without it.

Item 1b. The decision recorded in `docs/next-three.md` was an **optional extra**
rather than an always-on dependency, which creates two install shapes and
therefore two things to test:

* **Without the extra** -- the overwhelmingly common install -- importing the
  provider table must still work, a `bedrock` route must fail with a message
  naming what to install, and nothing may import the SDK at module scope. These
  tests force that shape regardless of what is installed in the running
  environment, by blocking the import; otherwise they would silently stop
  testing anything the moment the extra was added to the dev environment.
* **With the extra**, the request and response mapping is exercised against a
  stub standing in for the SDK client, so the mapping is tested without a
  network call, an AWS account, or a bill.

The host-delegated path -- where Bedrock already worked and still needs nothing
-- is `tests/test_bedrock_routing.py`, and is unaffected by any of this.
"""

from __future__ import annotations

import builtins
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness.config import (
    PROTECTED_PROVIDER_KEYS,
    ProviderConfig,
    default_config,
    load_config,
)
from supervisor_harness.providers.base import (
    ChatMessage,
    CompletionRequest,
    ProviderError,
)
from supervisor_harness.providers.bedrock import (
    DEFAULT_BEDROCK_MODEL,
    BedrockProvider,
)
from supervisor_harness.providers.router import ModelRouter, build_provider

REGION = "eu-west-1"


@pytest.fixture(autouse=True)
def _no_ambient_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the developer's own AWS environment.

    ``BedrockProvider`` falls back to ``AWS_REGION`` and friends, so a machine
    configured for Bedrock would otherwise make the no-region tests pass for the
    wrong reason -- and pass only on that machine.
    """
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def without_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the *absolute* ``import anthropic`` fail, whatever is installed.

    Forcing the shape rather than skipping is the point: these are the
    guarantees for the *default* install, and a test that quietly skipped once
    the extra was installed would stop guarding them exactly when the developer
    could no longer notice.

    Only ``level == 0`` is blocked, and that is not a detail. This package has
    its own ``providers/anthropic.py``, so ``from .anthropic import
    AnthropicProvider`` in `router.py` also arrives here as the name
    ``"anthropic"`` -- blocking by name alone breaks the module this fixture
    exists to prove still imports, which is how the first draft of it failed.
    """
    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        level = args[3] if len(args) > 3 else kwargs.get("level", 0)
        if level == 0 and (name == "anthropic" or name.startswith("anthropic.")):
            raise ImportError("No module named 'anthropic'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    for module in [m for m in sys.modules if m == "anthropic" or m.startswith("anthropic.")]:
        monkeypatch.delitem(sys.modules, module, raising=False)


# -- the default install ----------------------------------------------------


# Run in a *fresh interpreter* rather than by reloading modules in this one.
# Reloading rebinds the classes other tests hold references to -- the first
# draft of this did, and the very next test failed an `isinstance` against a
# class that was no longer the same object. A subprocess is also the more
# faithful test: a default install is a cold start without the SDK, not a
# running process that has had it taken away.
_FRESH_IMPORT = """
import sys

class Blocked:
    def find_spec(self, name, path=None, target=None):
        if name == "anthropic" or name.startswith("anthropic."):
            raise ImportError("No module named 'anthropic'")
        return None

sys.meta_path.insert(0, Blocked())
for module in [m for m in sys.modules if m == "anthropic" or m.startswith("anthropic.")]:
    del sys.modules[module]

from supervisor_harness.config import ProviderConfig
from supervisor_harness.providers.router import build_provider
from supervisor_harness.providers.bedrock import BedrockProvider

provider = build_provider("b", ProviderConfig(type="bedrock", region="eu-west-1"))
assert isinstance(provider, BedrockProvider), provider
assert provider.region == "eu-west-1"
print("OK")
"""


def test_the_provider_table_imports_without_the_extra() -> None:
    """A default install must be able to import the module that lists providers.

    This is the failure that would matter most and be noticed least: an SDK
    import at module scope in `bedrock.py` breaks `providers.router` for
    *everyone*, including the host-delegated users who never wanted Bedrock, and
    it breaks it at import time rather than at use -- so the harness would not
    start at all, for a provider nobody had configured.
    """
    result = subprocess.run(
        [sys.executable, "-c", _FRESH_IMPORT],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        check=False,   # the assertion below reports the stderr; `check` would hide it
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert "OK" in result.stdout


def test_a_bedrock_route_without_the_extra_names_what_to_install(
    without_the_extra: None,
) -> None:
    """The error a user actually hits, and the one thing it has to tell them.

    Without this the failure is an ImportError raised from inside a provider
    during a run, which says the `anthropic` module is missing -- naming a
    package that is not what the user should install, since the plain SDK
    without its `[bedrock]` extra would not help either.
    """
    provider = BedrockProvider(region=REGION)

    with pytest.raises(ProviderError) as caught:
        provider._sdk()

    message = str(caught.value)
    assert "supervisor-harness[bedrock]" in message
    assert "pip install" in message


async def test_availability_is_false_without_the_extra(without_the_extra: None) -> None:
    """`supervisor providers` has to say so up front rather than mid-run."""
    provider = BedrockProvider(region=REGION)

    assert await provider.available() is False
    assert provider.describe()["dependency_installed"] is False
    assert provider.describe()["configured"] is False


def test_a_bedrock_provider_is_still_constructible_without_the_extra(
    without_the_extra: None,
) -> None:
    """Construction must not be where it fails.

    `ModelRouter.provider` builds every configured provider, and `health()`
    builds all of them to report on them. If constructing a Bedrock provider
    raised, configuring one would break `supervisor providers` -- the command
    whose whole job is to tell you that it is misconfigured.
    """
    provider = build_provider("mybedrock", ProviderConfig(type="bedrock", region=REGION))

    assert isinstance(provider, BedrockProvider)
    assert provider.region == REGION


def test_the_sdk_import_is_not_shadowed_by_this_package(without_the_extra: None) -> None:
    """`from anthropic import ...` inside `providers/` must reach the SDK.

    ``supervisor_harness/providers/anthropic.py`` sits in the same package as
    ``bedrock.py``, so the name is genuinely ambiguous to a reader even though
    Python 3 resolves it absolutely. If it ever *did* resolve to the sibling,
    the failure would be bizarre rather than obvious: an ImportError naming
    ``AsyncAnthropicBedrock``, from a module that has nothing to do with AWS.

    Under a blocked absolute import the lookup must fail -- proving it was
    reaching outward for the SDK and not finding the neighbour.
    """
    with pytest.raises(ProviderError) as caught:
        BedrockProvider(region=REGION)._sdk()

    assert "supervisor-harness[bedrock]" in str(caught.value)


# -- configuration ----------------------------------------------------------


def test_the_bedrock_default_model_is_a_full_inference_profile_id() -> None:
    """The default carries a colon, which is the case item 1a pinned.

    A default that did not would leave the colon path untested by every ordinary
    use of the provider.
    """
    assert DEFAULT_BEDROCK_MODEL.endswith(":0")
    cfg = default_config()
    cfg.providers["bedrock"] = ProviderConfig(type="bedrock", default_model=DEFAULT_BEDROCK_MODEL)
    cfg.routing["analysis"] = f"bedrock:{DEFAULT_BEDROCK_MODEL}"

    assert cfg.binding_for("analysis").model == DEFAULT_BEDROCK_MODEL


def test_region_and_profile_cannot_be_set_by_a_workspace_config(tmp_path: Path) -> None:
    """Both redirect a credentialed request, so they sit with `base_url`.

    The AWS credential chain resolves an identity from the environment, so a
    workspace file that could set `profile` could make a run assume a different
    identity without ever naming a key -- and one that could set `region` could
    move the traffic somewhere the user does not audit. Neither needs to touch a
    secret to do damage, which is exactly the shape `base_url` was protected
    against.
    """
    assert {"region", "profile"} <= PROTECTED_PROVIDER_KEYS

    (tmp_path / "supervisor.config.json").write_text(
        '{"providers": {"bedrock": {"type": "bedrock", "region": "attacker-region-1",'
        ' "profile": "someone-else", "default_model": "fine-to-set"}}}',
        encoding="utf-8",
    )

    cfg = load_config(workspace=tmp_path)
    bedrock = cfg.providers.get("bedrock")

    assert bedrock is None or bedrock.region != "attacker-region-1"
    assert bedrock is None or bedrock.profile != "someone-else"
    assert any("region" in r for r in cfg.rejected_settings), cfg.rejected_settings
    assert any("profile" in r for r in cfg.rejected_settings), cfg.rejected_settings


async def test_a_bedrock_route_with_no_region_says_so() -> None:
    """A missing region is a configuration mistake, not an AWS error.

    Left to the SDK it surfaces deep in a run as a botocore region-resolution
    failure, which does not mention the harness or what to set.
    """
    provider = BedrockProvider()

    assert provider.region == ""
    with pytest.raises(ProviderError) as caught:
        await provider.complete(CompletionRequest(messages=[ChatMessage("user", "hi")]))

    assert "AWS_REGION" in str(caught.value)


# -- with the extra: the request and response mapping -----------------------

# Imported defensively rather than with a module-level `pytest.importorskip`.
# That call skips the *whole module* from the point it runs, which took the
# "without the extra" tests above with it -- so on a default install, the very
# tests guaranteeing that a default install works did not run at all. Measured
# before this changed: this file contributed 0 passed and 1 skipped in a venv
# without the extra, while reporting success.
#
# The marker below skips only the tests that genuinely need the SDK.
try:
    import anthropic
except ImportError:  # pragma: no cover - depends on the install shape
    anthropic = None

needs_the_extra = pytest.mark.skipif(
    anthropic is None, reason="the bedrock extra is not installed in this environment"
)


class StubMessages:
    """Stands in for ``client.messages``, recording what it was called with."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class StubClient:
    def __init__(self, outcome: Any) -> None:
        self.messages = StubMessages(outcome)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _message(text: str = "{}", *, thinking: str = "") -> Any:
    """A response shaped like the SDK's, built from the SDK's own types.

    Constructed rather than hand-stubbed so that a field the harness reads
    disappearing from the vendor's model is a test failure here rather than an
    empty answer in production.
    """
    content: list[Any] = [anthropic.types.TextBlock(type="text", text=text)]
    if thinking:
        content.insert(
            0, anthropic.types.ThinkingBlock(type="thinking", thinking=thinking, signature="")
        )
    return anthropic.types.Message(
        id="msg_1",
        type="message",
        role="assistant",
        model=DEFAULT_BEDROCK_MODEL,
        content=content,
        stop_reason="end_turn",
        usage=anthropic.types.Usage(input_tokens=11, output_tokens=7),
    )


def _provider_with(outcome: Any) -> tuple[BedrockProvider, StubClient]:
    provider = BedrockProvider(region=REGION)
    client = StubClient(outcome)
    provider._client = client
    return provider, client


@needs_the_extra
async def test_a_request_maps_onto_the_sdk_call() -> None:
    """Everything the harness promises a stage must reach the SDK.

    The schema instruction is the one most easily lost: every structured stage
    depends on it, and dropping it produces prose where JSON was required --
    which surfaces far away, as a parse error blamed on the model.
    """
    provider, client = _provider_with(_message('{"ok": true}'))

    response = await provider.complete(
        CompletionRequest(
            messages=[ChatMessage("user", "the brief")],
            system="you are a supervised agent",
            model=DEFAULT_BEDROCK_MODEL,
            temperature=0.3,
            max_tokens=1234,
            stop=["STOP"],
            json_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
            timeout=42.0,
        )
    )

    (call,) = client.messages.calls
    assert call["model"] == DEFAULT_BEDROCK_MODEL
    assert call["max_tokens"] == 1234
    assert call["temperature"] == 0.3
    assert call["stop_sequences"] == ["STOP"]
    assert call["timeout"] == 42.0
    assert call["messages"] == [{"role": "user", "content": "the brief"}]
    assert "you are a supervised agent" in call["system"]
    assert "JSON Schema" in call["system"], "the schema instruction was dropped"
    assert response.json() == {"ok": True}


@needs_the_extra
async def test_a_response_maps_back_including_usage_and_reasoning() -> None:
    """Usage is what every budget and ceiling in the harness is measured in.

    An agent whose usage reads zero is an agent with no token ceiling, which
    fails open rather than closed.
    """
    provider, _ = _provider_with(_message("answer", thinking="considering"))

    response = await provider.complete(
        CompletionRequest(messages=[ChatMessage("user", "q")])
    )

    assert response.text == "answer"
    assert response.reasoning == "considering"
    assert response.provider == "bedrock"
    assert response.model == DEFAULT_BEDROCK_MODEL
    assert response.finish_reason == "end_turn"
    assert (response.usage.input_tokens, response.usage.output_tokens) == (11, 7)


@needs_the_extra
async def test_an_empty_conversation_is_repaired_rather_than_rejected() -> None:
    """The Messages API refuses an empty list and a leading assistant turn.

    Both are reachable: a turn history can be filtered down to nothing by the
    blank-content rule, and a retry after an assistant turn starts with one.
    """
    provider, client = _provider_with(_message())

    await provider.complete(
        CompletionRequest(messages=[ChatMessage("user", "   "), ChatMessage("assistant", "")])
    )
    await provider.complete(
        CompletionRequest(messages=[ChatMessage("assistant", "I was saying")])
    )

    first, second = client.messages.calls
    assert first["messages"] == [{"role": "user", "content": "Proceed."}]
    assert second["messages"][0]["role"] == "user"


@needs_the_extra
@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (503, True), (529, True), (400, False), (403, False), (404, False)],
)
@needs_the_extra
async def test_sdk_status_errors_keep_their_retryability(status: int, retryable: bool) -> None:
    """The router's fallback chain only retries what is marked retryable.

    Collapsing every SDK exception to non-retryable would disable fallback for
    the case it most exists to cover -- Bedrock throttling, which is the single
    most likely failure on a real account and the one a `|` fallback route is
    written for.
    """
    error = anthropic.APIStatusError(
        "throttled", response=_httpx_response(status), body=None
    )
    provider, _ = _provider_with(error)

    with pytest.raises(ProviderError) as caught:
        await provider.complete(CompletionRequest(messages=[ChatMessage("user", "q")]))

    assert caught.value.retryable is retryable
    assert str(status) in str(caught.value)


@needs_the_extra
async def test_a_connection_failure_is_retryable() -> None:
    """It carries no status code, so the status path cannot classify it."""
    error = anthropic.APIConnectionError(request=_httpx_request())
    provider, _ = _provider_with(error)

    with pytest.raises(ProviderError) as caught:
        await provider.complete(CompletionRequest(messages=[ChatMessage("user", "q")]))

    assert caught.value.retryable is True


@needs_the_extra
def test_the_real_client_takes_the_region_and_leaves_retries_to_the_router() -> None:
    """Built with the actual SDK client, not a stub, because both claims are its.

    Two things that would otherwise be assertions in a comment:

    * **The region reaches the endpoint.** It is the only thing distinguishing
      one Bedrock account's traffic from another's, and a region that failed to
      arrive would default somewhere the user did not choose.
    * **`max_retries=0`.** `ModelRouter.complete` already retries with its own
      backoff and then falls back down the `|` chain. Leaving the SDK's retry
      loop on multiplies the two -- a stage configured for one retry would make
      up to eight attempts against a throttled endpoint, which is the worst
      possible response to throttling.

    Constructing the client makes no network call and needs no credentials.
    """
    client = BedrockProvider(region=REGION)._bedrock()

    assert client.max_retries == 0
    assert REGION in str(client.base_url)


@needs_the_extra
async def test_closing_the_provider_closes_the_sdk_client() -> None:
    """`ModelRouter.aclose` closes every provider it built; this one holds a client."""
    provider, client = _provider_with(_message())

    await provider.aclose()

    assert client.closed is True
    assert provider._client is None


@needs_the_extra
async def test_a_bedrock_stage_routes_through_the_router() -> None:
    """End to end through the layer a run actually calls, not the provider alone."""
    cfg = default_config()
    cfg.providers["bedrock"] = ProviderConfig(type="bedrock", region=REGION)
    cfg.routing["analysis"] = f"bedrock:{DEFAULT_BEDROCK_MODEL}"

    router = ModelRouter(cfg)
    provider, client = _provider_with(_message('{"routed": true}'))
    router.register("bedrock", provider)

    response = await router.complete(
        "analysis", CompletionRequest(messages=[ChatMessage("user", "q")])
    )

    assert response.json() == {"routed": True}
    assert client.messages.calls[0]["model"] == DEFAULT_BEDROCK_MODEL


def _httpx_response(status: int) -> Any:
    import httpx

    return httpx.Response(status_code=status, request=_httpx_request())


def _httpx_request() -> Any:
    import httpx

    return httpx.Request("POST", "https://bedrock-runtime.eu-west-1.amazonaws.com/")
