"""Bedrock, in the mode the harness actually supports today.

Issue #31 asks for Bedrock as a provider, and it conflates two cases with very
different costs. This file covers the cheap half: the harness in its default
host-delegated mode is *not in the model path at all*, so a user running Claude
Code against Bedrock -- ``CLAUDE_CODE_USE_BEDROCK=1``, ``AWS_REGION`` and
friends -- already has a working harness, and those variables are consumed by
the host rather than by anything here.

That claim was a reading of the code, not a tested property, and it rests on two
things worth pinning:

* **A Bedrock model id contains a colon.** ``us.anthropic.claude-sonnet-4-5-
  20250929-v1:0`` is one identifier, but every routing string in the harness is
  ``provider:model`` and is split on a colon. Reading ``partition`` and
  concluding it is fine is exactly the kind of two-line reading this project has
  been wrong about before.
* **The delegated path must touch no provider code**, which is an assertion in
  the README until something fails when it stops being true.

Autonomous Bedrock -- SigV4, and the dependency it costs -- is item 1b and is
deliberately not here. What *is* here is that asking for it today fails by name.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from supervisor_harness.config import (
    Policy,
    ProviderConfig,
    default_config,
    load_config,
)
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.host.detect import HostInfo
from supervisor_harness.models import Backend, RunMode, Usage
from supervisor_harness.providers import router as router_module
from supervisor_harness.providers.base import (
    CompletionRequest,
    CompletionResponse,
    Provider,
    ProviderError,
)
from supervisor_harness.providers.router import ModelRouter, build_provider
from supervisor_harness.store.runstore import RunStore

from .conftest import FakeProvider
from .test_host_delegation import PROMPT, HostSimulator

# A real Bedrock inference-profile id. The trailing ``:0`` is part of the id.
BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


# -- a colon in the model id is not a provider separator ---------------------


def test_a_bedrock_model_id_keeps_its_colon() -> None:
    """``provider:model`` splits on the *first* colon, so the id survives whole.

    A ``split(":")`` here would resolve the model to
    ``us.anthropic.claude-sonnet-4-5-20250929-v1`` and drop the version, which
    Bedrock rejects -- and it would do it silently, as a routing miss rather
    than a parse error.
    """
    cfg = default_config()
    cfg.routing["analysis"] = f"anthropic:{BEDROCK_MODEL}"

    binding = cfg.binding_for("analysis")

    assert binding.provider == "anthropic"
    assert binding.model == BEDROCK_MODEL


def test_a_colon_bearing_binding_round_trips_through_its_own_ref() -> None:
    """``ModelBinding.ref()`` joins with a colon, so it must re-parse to itself.

    The drift journal records which model checked a turn by calling ``ref()``,
    and ``supervisor providers`` prints it. If a ref cannot be parsed back, the
    string the harness shows the user is not the route it took.
    """
    cfg = default_config()
    cfg.routing["drift"] = f"anthropic:{BEDROCK_MODEL}"

    once = cfg.binding_for("drift")
    cfg.routing["drift"] = once.ref()
    twice = cfg.binding_for("drift")

    assert once.ref() == f"anthropic:{BEDROCK_MODEL}"
    assert (twice.provider, twice.model) == (once.provider, once.model)


def test_a_stage_route_from_the_environment_keeps_its_colon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SUPERVISOR_ROUTE_*`` is the documented one-off override.

    It is the most likely way someone points a single stage at a Bedrock id, and
    it reaches ``_parse_ref`` by a different route than a config file does.
    """
    monkeypatch.setenv("SUPERVISOR_HOME", str(tmp_path))
    monkeypatch.setenv("SUPERVISOR_ROUTE_ANALYSIS", f"anthropic:{BEDROCK_MODEL}")

    binding = load_config(workspace=tmp_path).binding_for("analysis")

    assert binding.provider == "anthropic"
    assert binding.model == BEDROCK_MODEL


class RecordingProvider(Provider):
    """Answers ``{}`` and remembers which model it was asked for."""

    name = "recording"

    def __init__(self) -> None:
        self.models: list[str] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.models.append(request.model)
        return CompletionResponse(
            text="{}", model=request.model, provider=self.name, usage=Usage()
        )


class AlwaysFails(Provider):
    """A primary route that is down, so the fallback chain is exercised."""

    name = "down"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise ProviderError(self.name, "unreachable", retryable=False)


async def test_a_colon_bearing_fallback_route_keeps_its_colon() -> None:
    """The fallback chain parses refs itself, in a second place.

    ``ModelRouter.complete`` re-partitions every ``|`` fallback rather than
    reusing ``_parse_ref``, so fixing one site would not fix the other. A
    Bedrock id behind a ``|`` is the ordinary shape -- a hosted model with a
    cheaper local fallback, or the reverse.
    """
    cfg = default_config()
    cfg.providers["down"] = ProviderConfig(type="host")
    cfg.providers["recording"] = ProviderConfig(type="host")
    cfg.routing["analysis"] = f"down:whatever|recording:{BEDROCK_MODEL}"

    router = ModelRouter(cfg)
    recording = RecordingProvider()
    router.register("down", AlwaysFails())
    router.register("recording", recording)

    await router.complete(
        "analysis", CompletionRequest(messages=[], system="", model="unset")
    )

    assert recording.models == [BEDROCK_MODEL]


# -- asking for autonomous Bedrock today fails by name -----------------------


def test_bedrock_as_a_provider_type_is_refused_by_name() -> None:
    """Item 1b is not built, and the failure has to say so rather than misroute.

    ``build_provider`` ends in a raise rather than a default, so a config that
    says ``"type": "bedrock"`` cannot quietly become an Anthropic client posting
    an ``x-api-key`` header at an endpoint that wants SigV4.
    """
    with pytest.raises(ValueError) as caught:
        build_provider("mybedrock", ProviderConfig(type="bedrock"))

    message = str(caught.value)
    assert "bedrock" in message
    assert "mybedrock" in message


# -- the delegated path is not in the model path at all ----------------------


@pytest.fixture
def no_provider_may_be_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trip on any attempt to build a provider, call one, or open a connection.

    Three tripwires rather than one, because they fail for different reasons: a
    stage that resolves off ``host`` builds a provider, a stage that is host but
    is completed anyway calls the router, and a provider constructed elsewhere
    opens an HTTP client. Any one of them firing means the harness put itself in
    the model path, and the environment variables that configure Bedrock for the
    host would no longer be the whole story.
    """

    def refuse_build(*args: object, **kwargs: object) -> Provider:
        raise AssertionError(f"the delegated path built a provider: {args!r}")

    async def refuse_complete(*args: object, **kwargs: object) -> CompletionResponse:
        raise AssertionError(f"the delegated path called a model: {args[1:]!r}")

    def refuse_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("the delegated path opened an HTTP client")

    monkeypatch.setattr(router_module, "build_provider", refuse_build)
    monkeypatch.setattr(ModelRouter, "complete", refuse_complete)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", refuse_client)


async def test_a_delegated_run_completes_without_touching_provider_code(
    workspace: Path, fake: FakeProvider, no_provider_may_be_reached: None
) -> None:
    """A whole run, planning to report, with every model call refused.

    This is the property the Bedrock answer rests on: in host-delegated mode the
    harness never reaches a provider, so ``CLAUDE_CODE_USE_BEDROCK`` and the AWS
    credentials are entirely the host's business and the harness has nothing to
    add. The run has to *complete*, not merely avoid the tripwires -- a run that
    fails early would satisfy the tripwires while proving nothing.
    """
    cfg = default_config()
    cfg.backend = Backend.HOST
    cfg.routing = {k: "host" for k in cfg.routing}
    cfg.policy = Policy(default_max_turns=3, execution_max_turns=3, max_analysis_lenses=3)

    store = RunStore(workspace / ".supervisor")
    host = HostInfo(name="claude-code", workspace=str(workspace), confidence=1.0)
    supervisor = Supervisor(workspace=workspace, config=cfg, store=store, host=host)

    simulator = HostSimulator(supervisor, fake)
    final = await simulator.drive(await supervisor.start(PROMPT, mode=RunMode.EXECUTE))

    assert final.action == "complete", final.message
    assert {"planning", "analysis", "synthesis", "execution", "verification"} <= set(
        simulator.dispatched
    )
    assert fake.calls == [], "the fake provider was reached on a delegated run"


async def test_a_bedrock_route_does_not_quietly_become_the_host_route(
    workspace: Path,
) -> None:
    """Routing a stage at a Bedrock id must not silently resolve to the host.

    ``_parse_ref`` defaults an empty provider to ``host``, so a malformed ref is
    indistinguishable from an intentional delegation. Pinning that a Bedrock ref
    resolves *off* host keeps a future parse regression from presenting as a
    quietly delegated stage that the user believes is running on Bedrock.
    """
    cfg = default_config()
    cfg.routing = {k: "host" for k in cfg.routing}
    cfg.routing["analysis"] = f"anthropic:{BEDROCK_MODEL}"

    router = ModelRouter(cfg, host_name="claude-code")

    assert router.is_host("planning")
    assert not router.is_host("analysis")
    assert router.binding("analysis").model == BEDROCK_MODEL
