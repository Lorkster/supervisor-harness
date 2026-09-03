"""Who closes what, and when.

Finding **Q-Q3** of `docs/quality-assessment.md`: `RunIndex.close()` existed and
nothing in the codebase called it. `RunStore` had no close at all, so every
store ever built held its SQLite connection until the process ended. A suite run
under ``-W always`` reported 90 unclosed connections; by default
``ResourceWarning`` is ignored, which is how it survived this long.

The rule these tests pin is **own what you made**. A store passed into a
``Supervisor`` belongs to its caller and must not be closed by the supervisor; a
store the supervisor constructs for itself has no other owner and must be. That
distinction is the whole finding -- closing unconditionally would be a
use-after-close bug for every caller that shares a store, which is most of this
test suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from supervisor_harness.config import HarnessConfig
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.host.detect import HostInfo
from supervisor_harness.providers.router import ModelRouter
from supervisor_harness.store.runstore import RunStore

from .conftest import FakeProvider


def test_closing_a_store_closes_its_index(workspace: Path) -> None:
    store = RunStore(workspace / ".supervisor")
    index = store.index()

    assert index.closed is False
    store.close()

    assert index.closed is True


def test_closing_twice_is_not_an_error(workspace: Path) -> None:
    """Teardown paths run more than once; a close that raises is a new failure.

    The test fixture in `conftest.py` closes every index a test opened, which
    means it closes stores that already closed themselves. If the second close
    raised, tidying up would break the tests that tidied up correctly.
    """
    store = RunStore(workspace / ".supervisor")
    index = store.index()

    store.close()
    store.close()
    index.close()

    assert index.closed is True


def test_a_store_still_works_after_being_closed(workspace: Path) -> None:
    """Close releases a connection; it does not end the store's life.

    A store is a handle on a directory, not on a connection. Making close
    terminal would push a lifetime onto every caller -- and `RunStore.discover`
    is called in six CLI commands that have no interest in one.
    """
    store = RunStore(workspace / ".supervisor")
    first = store.index()
    store.close()

    second = store.index()

    assert second is not first
    assert second.closed is False
    assert store.index() is second, "a second call should reuse the reopened index"


def test_the_context_manager_closes_on_the_way_out(workspace: Path) -> None:
    with RunStore(workspace / ".supervisor") as store:
        index = store.index()
        assert index.closed is False

    assert index.closed is True


def test_the_context_manager_closes_when_the_body_raises(workspace: Path) -> None:
    """The CLI commands return early and can raise; both must still close."""
    store = RunStore(workspace / ".supervisor")
    index = store.index()

    with pytest.raises(RuntimeError), store:
        raise RuntimeError("boom")

    assert index.closed is True


# -- own what you made ------------------------------------------------------


async def test_a_supervisor_closes_the_store_it_made(
    workspace: Path, config: HarnessConfig, fake: FakeProvider
) -> None:
    """No caller holds it, so nothing else ever would.

    This is the path the CLI and the MCP server both take: `Supervisor(workspace=...)`
    with no store argument. Before the fix, every `supervisor` command left its
    index connection open until the process ended.
    """
    host = HostInfo(name="test-host", workspace=str(workspace), confidence=1.0)
    router = ModelRouter(config, host_name=host.name)
    router.register("fake", fake)
    supervisor = Supervisor(workspace=workspace, config=config, host=host, router=router)

    index = supervisor.store.index()
    assert supervisor._owns_store is True

    await supervisor.aclose()

    assert index.closed is True


async def test_a_supervisor_does_not_close_a_store_it_was_given(
    workspace: Path, config: HarnessConfig, fake: FakeProvider
) -> None:
    """Closing a borrowed store would be a use-after-close bug for its owner.

    Two supervisors sharing one store is the ordinary shape in this suite, and
    in any embedder that runs more than one run against a single store. If
    `aclose` closed unconditionally, the first supervisor to finish would pull
    the connection out from under the second.
    """
    store = RunStore(workspace / ".supervisor")
    host = HostInfo(name="test-host", workspace=str(workspace), confidence=1.0)
    router = ModelRouter(config, host_name=host.name)
    router.register("fake", fake)
    supervisor = Supervisor(
        workspace=workspace, config=config, store=store, host=host, router=router
    )

    index = store.index()
    assert supervisor._owns_store is False

    await supervisor.aclose()

    assert index.closed is False, "the supervisor closed a store it did not own"
    # And the owner can still use it.
    assert store.index() is index
    store.close()


def test_every_cli_command_that_opens_a_store_closes_it() -> None:
    """A structural check, because the alternative is six behavioural ones.

    Each of these commands builds a store, and several return from inside
    branches -- the shape that made the leak easy to write in the first place.
    `with` is what makes every one of those exits close the connection, so the
    property worth pinning is that the bare construction never comes back.
    """
    source = (
        Path(__file__).resolve().parents[1]
        / "src" / "supervisor_harness" / "cli.py"
    ).read_text(encoding="utf-8")

    bare = [
        line.strip()
        for line in source.splitlines()
        if "RunStore.discover(" in line and not line.lstrip().startswith("with ")
    ]

    assert bare == [], f"CLI commands opening a store outside a `with`: {bare}"
    assert source.count("with RunStore.discover(") == 6
