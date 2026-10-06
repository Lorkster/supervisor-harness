"""A run's own branch: an execute-mode run works in a worktree, never in the owner's tree.

The agent here writes through the harness's own toolbox, exactly as an autonomous
agent does, so what is being tested is where the harness roots that toolbox --
not where a test helper happens to put a file. Every test uses a real git
repository: the property is about what git ends up holding, and only git can
say.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from supervisor_harness.config import HarnessConfig
from supervisor_harness.core import worktree
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.host.detect import HostInfo
from supervisor_harness.models import AgentKind, RunMode
from supervisor_harness.providers.router import ModelRouter
from supervisor_harness.store.runstore import RunStore

from .conftest import FakeProvider

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

PROMPT = "Add rate limiting to the public login endpoint so credential stuffing is blocked"
LIMITED = (
    "RATE_LIMIT = 10\n\ndef login(request):\n    # account_key is derived below\n"
    "    account_key = request.form['email']\n    return check(account_key)\n"
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def make_repository(root: Path) -> None:
    """The conftest workspace, as a committed repository with its own identity."""
    git(root, "init", "-q")
    git(root, "config", "user.name", "Owner")
    git(root, "config", "user.email", "owner@localhost")
    git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text(".supervisor/\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "baseline")


class Writing(FakeProvider):
    """An execution agent that changes `login.py` through its tools, then reports."""

    def _execution(self, request: Any) -> dict[str, Any]:
        if "Tool results" in request.messages[-1].content or len(request.messages) > 1:
            return super()._execution(request)
        return {"tool_calls": [{"tool": "write_file",
                                "args": {"path": "src/auth/login.py", "content": LIMITED}}]}


@pytest.fixture
def writing() -> Writing:
    return Writing()


def _supervisor(workspace: Path, config: HarnessConfig, fake: FakeProvider) -> Supervisor:
    host = HostInfo(name="test-host", workspace=str(workspace), confidence=1.0)
    router = ModelRouter(config, host_name=host.name)
    router.register("fake", fake)
    return Supervisor(workspace=workspace, config=config,
                      store=RunStore(workspace / ".supervisor"), host=host, router=router)


@pytest.fixture
def repo(workspace: Path) -> Path:
    make_repository(workspace)
    return workspace


# -- the property --------------------------------------------------------------


async def test_the_work_is_on_the_runs_branch_and_the_owners_tree_is_untouched(
    repo: Path, config: HarnessConfig, writing: Writing,
) -> None:
    before = (repo / "src" / "auth" / "login.py").read_text(encoding="utf-8")
    sup = _supervisor(repo, config, writing)

    response = await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = sup.store.load_state(response.run_id)
    wt = state.worktree

    assert response.action == "complete", response.message
    assert (repo / "src" / "auth" / "login.py").read_text(encoding="utf-8") == before
    assert git(repo, "status", "--porcelain") == "", "the owner's tree has no changes"

    assert wt is not None and wt.branch == worktree.branch_for(state.id)
    assert wt.commit, wt.note
    assert "RATE_LIMIT = 10" in git(repo, "show", f"{wt.branch}:src/auth/login.py")
    assert git(repo, "rev-parse", f"{wt.branch}~1") == git(repo, "rev-parse", "HEAD"), (
        "the branch is one commit on top of the baseline"
    )
    assert wt.removed and not Path(wt.path).exists(), "the worktree is gone, the branch stays"
    assert len(git(repo, "worktree", "list").splitlines()) == 1

    assert response.detail["branch"] == wt.branch
    assert response.detail["commit"] == wt.commit
    assert "## Where the changes are" in response.report_markdown
    assert f"git merge {wt.branch}" in response.report_markdown
    assert sup.status(state.id)["worktree"]["branch"] == wt.branch


async def test_the_owners_uncommitted_work_stays_theirs(
    repo: Path, config: HarnessConfig, writing: Writing,
) -> None:
    (repo / "NOTES.md").write_text("my unfinished thoughts\n", encoding="utf-8")
    sup = _supervisor(repo, config, writing)

    response = await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    wt = sup.store.load_state(response.run_id).worktree

    assert (repo / "NOTES.md").read_text(encoding="utf-8") == "my unfinished thoughts\n"
    assert wt is not None
    assert "NOTES.md" not in git(repo, "show", "--name-only", "--format=", wt.branch)
    assert "uncommitted changes" in wt.note


# -- when there is no branch, or it cannot be made ------------------------------


async def test_a_workspace_without_git_executes_in_place_and_says_so(
    workspace: Path, config: HarnessConfig, writing: Writing,
) -> None:
    sup = _supervisor(workspace, config, writing)
    response = await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    wt = sup.store.load_state(response.run_id).worktree

    assert response.action == "complete", response.message
    assert wt is not None and not wt.branch and "not a git repository" in wt.note
    assert "RATE_LIMIT" in (workspace / "src" / "auth" / "login.py").read_text(encoding="utf-8")
    assert "In the workspace itself" in response.report_markdown


async def test_a_branch_that_cannot_be_made_stops_the_run_before_any_work(
    repo: Path, config: HarnessConfig, writing: Writing, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carrying on in place would put the work exactly where the policy said not to."""
    monkeypatch.setattr(worktree, "create", lambda *args: "the disk is full")
    sup = _supervisor(repo, config, writing)

    response = await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    state = sup.store.load_state(response.run_id)

    assert response.action == "failed"
    assert "the disk is full" in response.message
    assert not [a for a in state.agents.values() if a.kind is AgentKind.EXECUTION]
    assert git(repo, "status", "--porcelain") == ""


async def test_work_that_cannot_be_committed_is_kept_in_the_worktree(
    repo: Path, config: HarnessConfig, writing: Writing,
) -> None:
    """Removing the worktree would delete the run's only copy of its work."""
    git(repo, "config", "user.name", "")
    git(repo, "config", "user.useConfigOnly", "true")
    sup = _supervisor(repo, config, writing)

    response = await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    wt = sup.store.load_state(response.run_id).worktree

    assert wt is not None and not wt.commit and not wt.removed
    assert "could not commit" in wt.note
    assert "RATE_LIMIT" in (Path(wt.path) / "src" / "auth" / "login.py").read_text(
        encoding="utf-8")


# -- where it does not apply ----------------------------------------------------


async def test_report_mode_never_makes_a_branch(
    repo: Path, config: HarnessConfig, writing: Writing,
) -> None:
    """security-eval runs report mode; nothing about it changes."""
    sup = _supervisor(repo, config, writing)
    response = await sup.run(PROMPT, mode=RunMode.REPORT)

    assert sup.store.load_state(response.run_id).worktree is None
    assert git(repo, "branch", "--list", "supervisor/*") == ""


async def test_it_can_be_turned_off(
    repo: Path, config: HarnessConfig, writing: Writing,
) -> None:
    config.policy.execution_worktree = False
    sup = _supervisor(repo, config, writing)
    response = await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)

    assert sup.store.load_state(response.run_id).worktree is None
    assert "RATE_LIMIT" in (repo / "src" / "auth" / "login.py").read_text(encoding="utf-8")


async def test_the_harness_checks_the_work_where_the_work_is(
    repo: Path, config: HarnessConfig, writing: Writing,
) -> None:
    """A criterion only the run's branch can satisfy is proven by the harness, there."""
    synthesis = writing._synthesis(None)  # type: ignore[arg-type]
    synthesis["tasks"][0]["dod"][1]["expect"] = "src/auth/login.py: RATE_LIMIT"
    writing.overrides["synthesis"] = synthesis
    sup = _supervisor(repo, config, writing)

    response = await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    task = next(iter(sup.store.load_state(response.run_id).tasks.values()))
    inspection = next(c for c in task.dod if "RATE_LIMIT" in c.expect)

    assert inspection.status.value == "pass", inspection.evidence
    assert inspection.verified_by == "harness"
    assert "RATE_LIMIT" not in (repo / "src" / "auth" / "login.py").read_text(encoding="utf-8")


async def test_a_host_delegated_run_is_not_given_a_branch_yet(
    repo: Path, fake: FakeProvider,
) -> None:
    """The harness cannot fence a host's own tools, so it does not promise a worktree."""
    from supervisor_harness.config import Backend, Policy, default_config

    from .test_host_delegation import HostSimulator

    cfg = default_config()
    cfg.backend = Backend.HOST
    cfg.routing = {k: "host" for k in cfg.routing}
    cfg.policy = Policy(default_max_turns=3, execution_max_turns=3, max_analysis_lenses=3)
    sup = Supervisor(workspace=repo, config=cfg, store=RunStore(repo / ".supervisor"),
                     host=HostInfo(name="claude-code", workspace=str(repo), confidence=1.0))
    final = await HostSimulator(sup, fake).drive(await sup.start(PROMPT, mode=RunMode.EXECUTE))

    assert final.action == "complete", final.message
    assert sup.store.load_state(final.run_id).worktree is None
