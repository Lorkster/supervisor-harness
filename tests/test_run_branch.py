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


# -- deleting a run gives back what it held in git -------------------------------


def _held(store: RunStore, repo: Path, run_id: str, branch: str, path: str = "") -> None:
    """A run whose state says it works on ``branch``, in a worktree at ``path``."""
    from supervisor_harness.models import RunState, RunWorktree
    from supervisor_harness.serde import to_jsonable
    from supervisor_harness.store.events import EventType

    session = store.create(RunState(id=run_id, prompt=PROMPT, workspace=str(repo)))
    session.emit(EventType.WORKTREE_OPENED, {"worktree": to_jsonable(RunWorktree(
        path=path, branch=branch, base=git(repo, "rev-parse", "HEAD")))})


def _branches(repo: Path) -> list[str]:
    return git(repo, "branch", "--list", "--format=%(refname:short)").splitlines()


async def test_deleting_a_run_takes_its_branch_and_says_how_to_put_it_back(
    repo: Path, config: HarnessConfig, writing: Writing,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """`supervisor delete` left every run's branch behind, for a hand-run `git branch -D`."""
    from supervisor_harness.cli import main

    monkeypatch.delenv("SUPERVISOR_HOME", raising=False)
    sup = _supervisor(repo, config, writing)
    response = await sup.run(PROMPT, mode=RunMode.EXECUTE, auto_approve=True)
    wt = sup.store.load_state(response.run_id).worktree
    assert wt is not None and wt.commit and wt.branch in _branches(repo)
    sup.store.close()
    capsys.readouterr()

    assert main(["delete", response.run_id, "-w", str(repo)]) == 0
    out = capsys.readouterr().out

    assert wt.branch not in _branches(repo), "the run's branch went with the run"
    restore = f"git branch {wt.branch} {wt.commit[:12]}"
    assert restore in out, out
    git(repo, *restore.split()[1:])
    assert git(repo, "rev-parse", wt.branch) == wt.commit, "and the note can put it back"


def test_deleting_a_run_mid_execution_unregisters_its_worktree(repo: Path) -> None:
    """The worktree is inside the run's directory: deleting that left git a stale entry."""
    from supervisor_harness.cli import _git_releaser

    store = RunStore(repo / ".supervisor")
    tree = store.run_dir("run_A") / "worktree"
    assert worktree.create(repo, tree, worktree.branch_for("run_A"), "HEAD") is None
    _held(store, repo, "run_A", worktree.branch_for("run_A"), str(tree))
    assert len(git(repo, "worktree", "list").splitlines()) == 2

    notes: dict[str, str] = {}
    assert store.delete_run("run_A", _git_releaser(notes, unmerged=True)) is True

    assert len(git(repo, "worktree", "list").splitlines()) == 1, "no stale registration"
    assert worktree.branch_for("run_A") not in _branches(repo)
    assert "removed its worktree" in notes["run_A"]
    assert not (store.runs_dir / "run_A").exists()


def test_a_cutoff_sweep_deletes_merged_branches_and_keeps_unmerged_work(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Nobody named those runs: work the owner has not merged is not the sweep's to drop."""
    import json

    from supervisor_harness.cli import main

    monkeypatch.delenv("SUPERVISOR_HOME", raising=False)
    store = RunStore(repo / ".supervisor")
    merged, unmerged = worktree.branch_for("run_A"), worktree.branch_for("run_B")
    git(repo, "branch", merged)
    git(repo, "checkout", "-q", "-b", unmerged)
    (repo / "change.txt").write_text("the run's work\n", encoding="utf-8")
    git(repo, "add", "change.txt")
    git(repo, "commit", "-q", "-m", "work")
    git(repo, "checkout", "-q", "-")
    for run_id, branch in (("run_A", merged), ("run_B", unmerged)):
        _held(store, repo, run_id, branch)
        snapshot = store.runs_dir / run_id / "state.json"
        data = json.loads(snapshot.read_text(encoding="utf-8"))
        data["updated_at"] = "2020-01-01T00:00:00Z"
        snapshot.write_text(json.dumps(data), encoding="utf-8")

    store.close()
    capsys.readouterr()

    assert main(["delete", "--older-than", "180", "--keep-last", "0", "-w", str(repo)]) == 0
    out = capsys.readouterr().out

    assert store.list_run_ids() == []
    assert merged not in _branches(repo)
    assert unmerged in _branches(repo)
    assert f"run_B: kept branch {unmerged}" in out and f"git branch -D {unmerged}" in out, out


def test_a_branch_the_harness_did_not_make_is_never_deleted(repo: Path) -> None:
    """A state naming the owner's own branch -- hand-edited, or corrupt -- costs them nothing."""
    from supervisor_harness.cli import _git_releaser

    store = RunStore(repo / ".supervisor")
    git(repo, "branch", "feature")
    _held(store, repo, "run_A", "feature")

    notes: dict[str, str] = {}
    assert store.delete_run("run_A", _git_releaser(notes, unmerged=True)) is True

    assert "feature" in _branches(repo)
    assert "run_A" not in notes
