"""Finding the host, reading what it declares, and routing a stage to a model.

Finding **Q-C4** of `docs/quality-assessment.md`: `agents/registry.py` at 57%,
`providers/router.py` at 67%, `host/detect.py` at 71%. Three modules that decide
what a run can *do* before any model is asked anything -- which agents exist,
which host is driving, and where a stage's work is sent.

Two things in here read the world outside the test, and both are neutralised
rather than tolerated:

* `detect_host` scores the ambient environment, and this suite is frequently run
  *inside* one of the hosts it detects. A test that does not clear `CLAUDECODE`
  passes for the wrong reason on the machine it was written on and fails in CI.
* `discover_host_agent_files` reads `~/.claude/agents`, so a developer's own
  agent definitions would otherwise appear in the results.

Both are the same mistake the Bedrock batch made with `AWS_REGION`, which is why
they are called out here rather than quietly handled.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness.agents.registry import (
    AgentRegistry,
    discover_host_agent_files,
)
from supervisor_harness.agents.roles import ROLES_BY_ID
from supervisor_harness.config import ProviderConfig, default_config
from supervisor_harness.host.detect import CLAUDE_CODE, CURSOR, UNKNOWN, HostInfo, detect_host
from supervisor_harness.models import Usage
from supervisor_harness.providers.base import (
    ChatMessage,
    CompletionRequest,
    CompletionResponse,
    Provider,
    ProviderError,
)
from supervisor_harness.providers.router import ModelRouter

HOST_VARS = (
    "SUPERVISOR_HOST", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_VERSION", "CURSOR_TRACE_ID", "CURSOR_AGENT", "CURSOR_WORKSPACE",
    "TERM_PROGRAM", "__CFBundleIdentifier",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """No ambient host. Every one of these is set by some real environment."""
    for var in HOST_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture
def elsewhere_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory with no agent definitions in it."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


# -- detecting the host -----------------------------------------------------


def test_an_explicit_override_beats_every_other_signal(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    """`SUPERVISOR_HOST` exists for CI and for tests, and must be unconditional."""
    clean_env.setenv("SUPERVISOR_HOST", "some-other-host")
    clean_env.setenv("CLAUDECODE", "1")

    host = detect_host(tmp_path)

    assert host.name == "some-other-host"
    assert host.confidence == 1.0
    assert host.evidence == ["SUPERVISOR_HOST"]


def test_no_signal_at_all_is_unknown_rather_than_a_guess(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    host = detect_host(tmp_path)

    assert host.name == UNKNOWN
    assert host.confidence == 0.0


def test_the_environment_identifies_each_host(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    clean_env.setenv("CLAUDECODE", "1")
    assert detect_host(tmp_path).name == CLAUDE_CODE

    clean_env.delenv("CLAUDECODE")
    clean_env.setenv("CURSOR_TRACE_ID", "abc")
    assert detect_host(tmp_path).name == CURSOR


def test_a_directory_only_breaks_a_tie_and_does_not_win_alone(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    """A repository can carry config for a host nobody is running.

    `.cursor/` in a checkout is evidence about the repository, not about who is
    driving -- so it must not outrank an environment variable that is.
    """
    (tmp_path / ".cursor").mkdir()
    clean_env.setenv("CLAUDECODE", "1")

    host = detect_host(tmp_path)

    assert host.name == CLAUDE_CODE
    assert "dir:.cursor" in host.evidence, "the weaker evidence should still be recorded"


def test_the_delegation_hint_tells_each_host_how_to_spawn() -> None:
    """It goes into a brief verbatim, so an empty or generic one misdirects."""
    assert "Task tool" in HostInfo(name=CLAUDE_CODE).delegation_hint
    assert "background agent" in HostInfo(name=CURSOR).delegation_hint
    assert HostInfo(name=UNKNOWN).delegation_hint.strip()


# -- reading what the workspace declares ------------------------------------


def _agent_file(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(body, encoding="utf-8")


def test_frontmatter_is_read_including_both_spellings_of_a_tool_list(
    tmp_path: Path, elsewhere_home: Path
) -> None:
    """`tools` and `allowed-tools`, inline-list and comma forms, all in use."""
    _agent_file(
        tmp_path / ".claude" / "agents", "explorer",
        "---\nname: Explore\ndescription: 'Read-only search'\n"
        "tools: [Read, Grep]\nmodel: sonnet\n---\nbody text\n",
    )
    _agent_file(
        tmp_path / ".claude" / "agents", "planner",
        "---\nname: Plan\nallowed-tools: Read, Grep, Glob\n---\nplans things\n",
    )

    found = {a.name: a for a in discover_host_agent_files(tmp_path, HostInfo(name=CLAUDE_CODE))}

    assert found["Explore"].tools == ["Read", "Grep"]
    assert found["Explore"].model == "sonnet"
    assert found["Explore"].description == "Read-only search"
    assert found["Plan"].tools == ["Read", "Grep", "Glob"]
    # No frontmatter description: the body stands in, so a bare file still says
    # something a matcher can work with.
    assert found["Plan"].description == "plans things"


def test_a_file_without_frontmatter_is_still_an_agent(
    tmp_path: Path, elsewhere_home: Path
) -> None:
    _agent_file(tmp_path / ".claude" / "agents", "bare", "just a description\n")

    found = discover_host_agent_files(tmp_path, HostInfo(name=CLAUDE_CODE))

    assert [a.name for a in found] == ["bare"]
    assert found[0].description == "just a description"


def test_cursor_modes_are_read_from_json(tmp_path: Path, elsewhere_home: Path) -> None:
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "modes.json").write_text(
        json.dumps({"modes": [{"name": "Reviewer", "description": "reviews", "model": "gpt"},
                              {"id": "Fixer"},
                              "not a dict"]}),
        encoding="utf-8",
    )

    found = {a.name: a for a in discover_host_agent_files(tmp_path, HostInfo(name=CURSOR))}

    assert found["Reviewer"].description == "reviews"
    assert "Fixer" in found, "an entry with only an id is still a mode"
    assert len(found) == 2, "the non-dict entry should be skipped, not crash the run"


def test_malformed_modes_json_does_not_end_the_run(
    tmp_path: Path, elsewhere_home: Path
) -> None:
    """The file is someone else's; unreadable is a normal state, not an error."""
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "modes.json").write_text("{not json", encoding="utf-8")

    assert discover_host_agent_files(tmp_path, HostInfo(name=CURSOR)) == []


def test_the_workspace_wins_over_the_user_profile(
    tmp_path: Path, elsewhere_home: Path
) -> None:
    """Both directories are read, and the same name must resolve once."""
    _agent_file(tmp_path / ".claude" / "agents", "shared",
                "---\nname: Shared\ndescription: from the workspace\n---\n")
    _agent_file(elsewhere_home / ".claude" / "agents", "shared",
                "---\nname: Shared\ndescription: from the home directory\n---\n")

    found = discover_host_agent_files(tmp_path, HostInfo(name=CLAUDE_CODE))

    assert [a.description for a in found] == ["from the workspace"]


# -- matching a role to something that can run it ---------------------------


def _registry(tmp_path: Path, *declared: Any) -> AgentRegistry:
    """A registry over an empty workspace, with only what the host declares.

    `elsewhere_home` is what keeps the developer's own `~/.claude/agents` out of
    `file_agents`; without it these matches would depend on whose machine ran
    them.
    """
    return AgentRegistry(tmp_path, HostInfo(name=CLAUDE_CODE), host_declared=list(declared))


def _declared(name: str, description: str = "") -> dict[str, str]:
    return {"name": name, "description": description}


def test_a_role_hint_is_preferred_over_a_description_overlap(
    tmp_path: Path, elsewhere_home: Path
) -> None:
    registry = _registry(
        tmp_path,
        _declared("general-purpose", "does anything"),
        _declared("Plan", "software architect"),
    )

    match = registry.match(ROLES_BY_ID["architecture"])

    assert match is not None
    assert match.name == "Plan"


def test_a_bare_name_is_a_declaration(tmp_path: Path, elsewhere_home: Path) -> None:
    """`["general-purpose"]` is the shape people actually write.

    It used to raise `AttributeError: 'str' object has no attribute 'get'` from
    inside a comprehension, naming neither the flag nor the entry at fault.
    """
    registry = _registry(tmp_path, "general-purpose", {"no": "name"}, 17)

    assert [a.name for a in registry.host_agents] == ["general-purpose"]


def test_a_description_overlap_matches_when_no_hint_does(
    tmp_path: Path, elsewhere_home: Path
) -> None:
    registry = _registry(tmp_path, _declared("auditor", "a security specialist"))

    match = registry.match("security")

    assert match is not None and match.name == "auditor"


def test_a_general_purpose_agent_is_the_last_resort(
    tmp_path: Path, elsewhere_home: Path
) -> None:
    registry = _registry(tmp_path, _declared("general-purpose", "does anything"))

    match = registry.match("data")

    assert match is not None and match.name == "general-purpose"


def test_nothing_matches_rather_than_something_wrong(
    tmp_path: Path, elsewhere_home: Path
) -> None:
    """`None` means "brief it generically", which is always correct.

    Returning an unrelated agent instead would hand a data-analysis brief to
    whatever happened to be first.
    """
    assert _registry(tmp_path, _declared("unrelated", "makes coffee")).match("data") is None
    assert _registry(tmp_path).match("security") is None
    assert _registry(tmp_path, _declared("general-purpose")).match("no-such-role") is None


def test_file_agents_are_used_only_when_the_host_declares_nothing(
    tmp_path: Path, elsewhere_home: Path
) -> None:
    """A file is a hint; a host-declared agent can actually be spawned."""
    _agent_file(tmp_path / ".claude" / "agents", "plan",
                "---\nname: Plan\ndescription: software architect\n---\n")

    from_files = _registry(tmp_path)
    match = from_files.match("architecture")

    assert match is not None and match.name == "Plan"
    assert match.spawnable is False, "a file is not something the host can spawn by name"

    # A host-declared agent takes precedence over the same file.
    declared = _registry(tmp_path, _declared("Plan", "software architect"))
    assert declared.match("architecture").spawnable is True


# -- routing a stage to a model ---------------------------------------------


class Recorder(Provider):
    name = "recorder"

    def __init__(self, fail_times: int = 0, error: ProviderError | None = None) -> None:
        self.calls = 0
        self.fail_times = fail_times
        self.error = error

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error or ProviderError(self.name, "transient", retryable=True)
        return CompletionResponse(text="{}", model=request.model, provider=self.name,
                                  usage=Usage())


def _router(**providers: ProviderConfig) -> ModelRouter:
    cfg = default_config()
    cfg.providers.update(providers)
    return ModelRouter(cfg)


async def test_a_retryable_failure_is_retried_on_the_same_provider() -> None:
    router = _router(rec=ProviderConfig(type="host"))
    provider = Recorder(fail_times=1)
    router.register("rec", provider)
    router.config.routing["analysis"] = "rec:m"

    await router.complete("analysis", CompletionRequest(messages=[ChatMessage("user", "q")]))

    assert provider.calls == 2, "a retryable error should buy exactly one more attempt"


async def test_a_non_retryable_failure_falls_through_without_retrying() -> None:
    router = _router(rec=ProviderConfig(type="host"), alt=ProviderConfig(type="host"))
    first = Recorder(fail_times=9, error=ProviderError("rec", "bad request", retryable=False))
    second = Recorder()
    router.register("rec", first)
    router.register("alt", second)
    router.config.routing["analysis"] = "rec:m|alt:m"

    await router.complete("analysis", CompletionRequest(messages=[ChatMessage("user", "q")]))

    assert first.calls == 1, "a non-retryable error must not be retried"
    assert second.calls == 1


async def test_an_unconfigured_fallback_is_skipped_rather_than_fatal() -> None:
    """A route naming a provider nobody configured should not sink the stage."""
    router = _router(alt=ProviderConfig(type="host"))
    second = Recorder()
    router.register("alt", second)
    router.config.routing["analysis"] = "ghost:m|alt:m"

    await router.complete("analysis", CompletionRequest(messages=[ChatMessage("user", "q")]))

    assert second.calls == 1


async def test_a_disabled_provider_is_refused_by_name() -> None:
    router = _router(off=ProviderConfig(type="host", enabled=False))

    with pytest.raises(ValueError) as caught:
        router.provider("off")

    assert "off" in str(caught.value) and "disabled" in str(caught.value)


async def test_every_route_failing_names_the_chain_it_tried() -> None:
    """The message is what a user sees when a run dies; a bare failure is unhelpful."""
    router = _router(rec=ProviderConfig(type="host"), alt=ProviderConfig(type="host"))
    router.register("rec", Recorder(fail_times=9))
    router.register("alt", Recorder(fail_times=9))
    router.config.routing["analysis"] = "rec:m|alt:m"

    with pytest.raises(ProviderError) as caught:
        await router.complete(
            "analysis", CompletionRequest(messages=[ChatMessage("user", "q")]), retries=0
        )

    message = str(caught.value)
    assert "analysis" in message
    assert "rec -> alt" in message


async def test_health_reports_each_provider_without_failing_on_any_of_them() -> None:
    """`supervisor providers` must answer even when a provider is broken.

    An exception here would make the command that diagnoses a misconfiguration
    the second casualty of it.
    """
    class Exploding(Provider):
        name = "exploding"

        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            raise NotImplementedError

        async def available(self) -> bool:
            raise RuntimeError("boom")

    router = _router(
        ok=ProviderConfig(type="host"),
        off=ProviderConfig(type="host", enabled=False),
        bad=ProviderConfig(type="host"),
    )
    router.register("ok", Recorder())
    router.register("bad", Exploding())

    health = await router.health()

    assert health["off"] == {"enabled": False}
    assert health["ok"]["available"] is True
    assert health["bad"]["available"] is False
    assert "boom" in health["bad"]["error"]


async def test_closing_the_router_closes_every_provider_it_built() -> None:
    class Closable(Provider):
        name = "closable"

        def __init__(self) -> None:
            self.closed = False

        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            raise NotImplementedError

        async def aclose(self) -> None:
            self.closed = True

    router = _router()
    first, second = Closable(), Closable()
    router.register("a", first)
    router.register("b", second)

    await router.aclose()

    assert first.closed and second.closed
    assert router._providers == {}


async def test_a_provider_that_raises_on_close_does_not_stop_the_others() -> None:
    """`aclose` runs at the end of every run; one bad provider must not leak the rest."""
    class Rude(Provider):
        name = "rude"

        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            raise NotImplementedError

        async def aclose(self) -> None:
            raise RuntimeError("no")

    router = _router()
    router.register("rude", Rude())

    await router.aclose()   # gather(..., return_exceptions=True)

    assert router._providers == {}


def test_the_registry_describes_what_it_found(
    tmp_path: Path, elsewhere_home: Path
) -> None:
    """`supervisor providers` and the run's own record both read this."""
    _agent_file(tmp_path / ".claude" / "agents", "plan", "---\nname: Plan\n---\n")
    registry = _registry(tmp_path, _declared("general-purpose"))

    described: dict[str, Any] = registry.describe()

    assert described["host"] == CLAUDE_CODE
    assert described["host_declared"] == ["general-purpose"]
    assert {"name": "Plan", "source": "claude-code-file"} in described["from_files"]
    assert described["builtin_roles"], "the built-in roles always work and must be listed"
    assert len(registry.all()) > len(registry.host_agents)
