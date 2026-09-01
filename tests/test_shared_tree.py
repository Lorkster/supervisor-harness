"""The working tree several agents share, and the commit they measure against.

Both halves come from one supervised run. Nine agents worked in a single tree,
separated only by their path scopes; one of them ran ``git stash`` and
``git stash pop`` across the whole tree -- including files other agents were
part-way through writing -- to get itself a clean lint baseline. It disclosed
this and nothing was lost, but a path scope does not constrain git, so nothing
except timing had prevented it.

The same run gave every task the criterion "the existing test suite still
passes" while nine agents wrote into that tree, so each verifier measured a
different tree: 87, 99 and 100 tests were reported for the same suite, and each
verifier then had to explain why its own number differed from its neighbours'.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from supervisor_harness.agents.brief import (
    build_analysis_brief,
    build_execution_brief,
    build_verification_brief,
)
from supervisor_harness.agents.roles import ROLES_BY_ID
from supervisor_harness.config import Policy
from supervisor_harness.core.baseline import BASELINE_FACT, git_baseline
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.core.tools import Toolbox, tree_wide_git
from supervisor_harness.models import (
    AgentKind,
    AgentSpec,
    DoDCriterion,
    ExecutionTask,
    RunState,
    Scope,
    VerifyMethod,
)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

# Every one of these acts on the whole tree or moves HEAD, so no path scope can
# express what it is allowed to touch.
TREE_STATE_COMMANDS = [
    "git stash",
    "git stash pop",
    "git checkout .",
    "git checkout -- src",
    "git -C . reset --hard",
    "git clean -fd",
    "git rebase main",
    "git commit -am wip",
    "make && git reset --hard",
]


def _repo(path: Path) -> str:
    """A git repository with one commit, returning its full sha."""
    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
        )

    run("init", "-q")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "test")
    (path / "kept.txt").write_text("committed\n", encoding="utf-8")
    run("add", "kept.txt")
    run("commit", "-qm", "baseline")
    return run("rev-parse", "HEAD").stdout.strip()


# --------------------------------------------------------------------------
# Enforcement
# --------------------------------------------------------------------------


def test_no_agent_may_change_the_shared_trees_git_state(tmp_path: Path) -> None:
    """Refused for the fenced agent and the unfenced one alike.

    This rule is about the tree being shared, not about any agent's scope, so it
    holds for an agent that declared none.

    It used to be provable through ``run_command`` by using an unscoped agent,
    which had nothing else standing in its way. Now that the executable
    allow-list is universal, ``git`` is refused to *every* agent before this
    rule is consulted, so the end-to-end call can only show the command was
    refused -- not which rule refused it. The rule itself is therefore asserted
    directly. That is the honest shape: it is a second lock now, kept so that
    loosening the allow-list later cannot silently reopen the shared tree.
    """
    box = Toolbox(tmp_path, Policy(allow_command_execution=True))
    scoped = AgentSpec(
        id="b", kind=AgentKind.EXECUTION, scope=Scope(paths=["src/auth/**"])
    )
    unscoped = AgentSpec(id="c", kind=AgentKind.EXECUTION, scope=Scope())

    for command in TREE_STATE_COMMANDS:
        for agent in (scoped, unscoped):
            result = box.call("run_command", {"command": command}, agent)
            assert not result.ok, command
            assert "exit=" not in result.output, command
        # The second lock, asserted where it can still be seen on its own.
        refusal = tree_wide_git(command)
        assert refusal is not None, command
        assert "working tree" in refusal, command


@needs_git
def test_a_refused_stash_leaves_a_peers_uncommitted_work_alone(tmp_path: Path) -> None:
    """The refusal is what saves the file, not the agent's good judgement."""
    _repo(tmp_path)
    peer_work = tmp_path / "kept.txt"
    peer_work.write_text("committed\nwritten by another agent\n", encoding="utf-8")

    box = Toolbox(tmp_path, Policy(allow_command_execution=True))
    unscoped = AgentSpec(id="c", kind=AgentKind.EXECUTION, scope=Scope())

    assert not box.call("run_command", {"command": "git stash"}, unscoped).ok
    assert "written by another agent" in peer_work.read_text(encoding="utf-8")


@needs_git
def test_reading_git_state_is_not_what_this_rule_refuses(tmp_path: Path) -> None:
    """``git status`` and ``git diff`` inspect; they do not change the tree.

    The distinction still holds where it is made -- ``tree_wide_git`` passes a
    read straight through -- but no agent can reach one any more, because the
    executable allow-list refuses ``git`` outright and is now universal. That is
    a real cost of making the fence universal, and it is recorded here rather
    than left for someone to discover: an agent cannot see its own diff.

    It is the price of the allow-list rather than an oversight. ``git`` cannot
    be narrowed to its read-only subcommands by name, because
    ``git -c alias.s='!sh -c ...' s`` runs anything at all, so admitting
    ``git status`` means fencing git's flags too. Nothing in the harness
    consumes a diff: a turn's ``files_touched`` is the agent's own report.
    """
    _repo(tmp_path)
    box = Toolbox(tmp_path, Policy(allow_command_execution=True))
    unscoped = AgentSpec(id="c", kind=AgentKind.EXECUTION, scope=Scope())

    for command in ("git status --porcelain", "git diff", "git log --oneline -1"):
        # The shared-tree rule does not object to a read.
        assert tree_wide_git(command) is None, command
        # The allow-list does, for every agent, scoped or not.
        result = box.call("run_command", {"command": command}, unscoped)
        assert not result.ok, command
        assert "may not run 'git'" in result.output, command


# --------------------------------------------------------------------------
# What the briefs say
# --------------------------------------------------------------------------


def _run(facts: dict[str, str] | None = None) -> RunState:
    return RunState(prompt="Add rate limiting to the login endpoint", facts=facts or {})


def _task() -> ExecutionTask:
    return ExecutionTask(
        title="Rate-limit the login endpoint",
        action="Add a per-account limiter",
        motivation="Credential stuffing is unbounded",
        dod=[
            DoDCriterion(
                id="dod_1",
                statement="the existing test suite still passes",
                method=VerifyMethod.COMMAND,
                command="pytest -q",
            )
        ],
    )


def test_every_brief_forbids_tree_wide_git_operations() -> None:
    """Stated, not merely enforced: a host-run agent uses the host's own tools.

    The harness can refuse the command only for an agent it drives itself. In
    host-delegated mode -- the default -- the brief is the whole of the control,
    so the prohibition has to be in the text of every brief rather than only in
    the toolbox.
    """
    run = _run()
    agent = AgentSpec(id="a", kind=AgentKind.EXECUTION, scope=Scope(paths=["src/**"]))
    briefs = [
        build_analysis_brief(run, agent, ROLES_BY_ID["technical"], [], {}),
        build_execution_brief(run, agent, _task(), ROLES_BY_ID["implementer"], [], {}),
        build_verification_brief(run, agent, _task(), {}),
    ]
    for brief in briefs:
        for command in ("git stash", "git checkout", "git clean", "git reset", "git rebase"):
            assert f"`{command}`" in brief, command
        assert "does not constrain git" in brief


def test_the_briefs_name_the_baseline_a_criterion_is_measured_against() -> None:
    run = _run({BASELINE_FACT: "`a344b0d`, working tree clean when the run started"})
    agent = AgentSpec(id="a", kind=AgentKind.EXECUTION, scope=Scope())

    for brief in (
        build_execution_brief(run, agent, _task(), ROLES_BY_ID["implementer"], [], {}),
        build_verification_brief(run, agent, _task(), {}),
    ):
        assert "a344b0d" in brief
        assert "baseline plus your own diff" in brief
        # And what to do with a failure that is not this task's.
        assert "belongs to a peer" in brief


def test_a_run_without_a_baseline_says_so_rather_than_inventing_one() -> None:
    brief = build_verification_brief(
        _run(), AgentSpec(id="a", kind=AgentKind.VERIFICATION), _task(), {}
    )
    assert "no recorded baseline commit" in brief


# --------------------------------------------------------------------------
# Where the baseline comes from
# --------------------------------------------------------------------------


@needs_git
def test_the_baseline_is_the_commit_the_run_started_from(tmp_path: Path) -> None:
    sha = _repo(tmp_path)
    baseline = git_baseline(tmp_path)
    assert sha[:12] in baseline
    assert "clean" in baseline

    # The harness's own run directory is written into the workspace before this
    # is taken, and a workspace that has not ignored it must not be reported as
    # dirty on the strength of the harness's own files.
    (tmp_path / ".supervisor" / "runs").mkdir(parents=True)
    (tmp_path / ".supervisor" / "runs" / "events.jsonl").write_text("{}", encoding="utf-8")
    assert "clean" in git_baseline(tmp_path)

    # A tree that was already dirty is not "baseline plus this run's work", and
    # the fact says so rather than letting an agent discover it as an anomaly.
    (tmp_path / "kept.txt").write_text("changed before the run\n", encoding="utf-8")
    assert "already modified" in git_baseline(tmp_path)


def test_a_workspace_that_is_not_a_repository_has_no_baseline(tmp_path: Path) -> None:
    assert git_baseline(tmp_path) == ""


@needs_git
async def test_a_run_records_its_baseline_and_puts_it_in_the_briefs(
    supervisor: Supervisor,
) -> None:
    """Recorded once, at the start, before any agent has written anything."""
    sha = _repo(Path(supervisor.workspace))

    response = await supervisor.start("Add rate limiting to the login endpoint")
    state = supervisor.store.load_state(response.run_id)

    assert sha[:12] in state.facts[BASELINE_FACT]
    assert state.briefs, "no brief was rendered"
    assert all(sha[:12] in brief for brief in state.briefs.values())
