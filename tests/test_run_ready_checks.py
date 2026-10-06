"""The checks have to be able to run where the work is.

Found preparing the first envelope-mode runs on real repositories, before any
model had been asked anything:

* a TypeScript app with a Python sub-project was given `pytest` at its root,
  because the search for `test_*.py` ran first and found the sub-project's;
* a Python project whose suite passes in its own virtualenv failed at import
  under the `pytest` on PATH, which belonged to a different Python;
* a run's own worktree (batch D) holds only what git tracks, so it has no
  `node_modules`, and every JavaScript check failed there for that reason alone.

Each would have failed every task in those repositories for a reason that had
nothing to do with the work, and envelope approval would have reported the
model as the problem.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from supervisor_harness.core import worktree
from supervisor_harness.core.dod import (
    detect_test_command,
    pytest_argv,
    shell_split,
    unsafe_command,
)


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def fake_venv(root: Path, name: str = ".venv", *, with_pytest: bool = True) -> Path:
    bindir = root / name / ("Scripts" if shutil.os.name == "nt" else "bin")  # type: ignore[attr-defined]
    python = bindir / ("python.exe" if shutil.os.name == "nt" else "python")  # type: ignore[attr-defined]
    write(root, {str(python.relative_to(root)): ""})
    if with_pytest:
        write(root, {str((bindir / ("pytest.exe" if shutil.os.name == "nt" else "pytest"))  # type: ignore[attr-defined]
                         .relative_to(root)): ""})
    return python


# -- which runner ----------------------------------------------------------------


def test_a_javascript_app_with_a_python_subproject_runs_its_own_tests(tmp_path: Path) -> None:
    write(tmp_path, {
        "package.json": json.dumps({"scripts": {"test": "vitest run"}}),
        "data-pipeline/pyproject.toml": "",
        "data-pipeline/tests/test_units.py": "def test_x():\n    assert True\n",
        "tests/e2e.spec.ts": "",
    })
    assert detect_test_command(tmp_path) == "npm test --silent"


def test_a_python_project_is_tested_in_its_own_virtualenv(tmp_path: Path) -> None:
    write(tmp_path, {"pyproject.toml": ""})
    python = fake_venv(tmp_path)
    command = detect_test_command(tmp_path)

    assert shell_split(command)[0] == str(python)
    assert unsafe_command(command) is None, "a criterion command the harness will run"
    assert pytest_argv(command) is not None, "fails_before can read its results"


@pytest.mark.parametrize("name", [".venv", "venv"])
def test_either_conventional_virtualenv_name_is_found(tmp_path: Path, name: str) -> None:
    write(tmp_path, {"setup.cfg": ""})
    python = fake_venv(tmp_path, name)
    assert shell_split(detect_test_command(tmp_path))[0] == str(python)


def test_a_virtualenv_without_pytest_is_not_used(tmp_path: Path) -> None:
    write(tmp_path, {"pyproject.toml": ""})
    fake_venv(tmp_path, with_pytest=False)
    assert detect_test_command(tmp_path) == "pytest -q"


def test_a_bare_tests_directory_still_means_pytest(tmp_path: Path) -> None:
    write(tmp_path, {"tests/test_it.py": "def test_x():\n    assert True\n"})
    assert detect_test_command(tmp_path) == "pytest -q"


def test_a_tests_directory_without_python_tests_is_not_pytest(tmp_path: Path) -> None:
    write(tmp_path, {"tests/readme.md": ""})
    assert detect_test_command(tmp_path) == ""


# -- the run's worktree gets its own dependencies --------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def node_repo(tmp_path: Path) -> Path:
    """A project with one dependency, local so that installing it needs no network."""
    root = tmp_path / "repo"
    write(root, {
        "package.json": json.dumps({"name": "x", "version": "1.0.0",
                                    "scripts": {"test": "node -e 0"},
                                    "dependencies": {"dep": "file:packages/dep"}}),
        "packages/dep/package.json": json.dumps({"name": "dep", "version": "1.0.0"}),
        "index.js": "module.exports = 1;\n",
        ".gitignore": "node_modules/\n",
    })
    npm = shutil.which("npm")
    if npm is not None:
        subprocess.run([npm, "install", "--package-lock-only", "--no-audit", "--no-fund"],
                       cwd=root, check=True, capture_output=True)
    _git(root, "init", "-q")
    for key, value in (("user.name", "Owner"), ("user.email", "owner@localhost"),
                       ("commit.gpgsign", "false")):
        _git(root, "config", key, value)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")
    return root


@pytest.mark.skipif(shutil.which("npm") is None or shutil.which("git") is None,
                    reason="needs npm and git")
def test_a_fresh_worktree_has_the_projects_dependencies_installed(node_repo: Path) -> None:
    tree = node_repo.parent / "wt"
    assert worktree.create(node_repo, tree, "supervisor/run_x",
                           _git(node_repo, "rev-parse", "HEAD")) is None
    note = worktree.prepare(tree)

    assert "installed" in note, note
    assert (tree / "node_modules").is_dir()
    assert not (node_repo / "node_modules").exists(), "the owner's tree is untouched"


def test_a_worktree_with_no_lockfile_is_left_alone(tmp_path: Path) -> None:
    assert worktree.prepare(tmp_path) == ""


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_installed_dependencies_are_never_committed_on_the_runs_branch(node_repo: Path) -> None:
    """A repository that does not ignore node_modules would get every package."""
    base = _git(node_repo, "rev-parse", "HEAD")
    tree = node_repo.parent / "wt"
    assert worktree.create(node_repo, tree, "supervisor/run_y", base) is None
    (tree / ".gitignore").unlink()  # this repository does not ignore them
    write(tree, {"node_modules/left-pad/index.js": "module.exports = 2;\n",
                 "index.js": "module.exports = 3;\n"})

    closed = worktree.close(node_repo, tree, base, "the run's change")

    assert closed.commit, closed.note
    committed = _git(node_repo, "show", "--name-only", "--format=", closed.commit).split()
    assert "index.js" in committed
    assert not [path for path in committed if path.startswith("node_modules")], committed


# -- a model's paths, placed in the tree -----------------------------------------


def test_a_path_missing_its_prefix_is_placed_at_the_only_file_it_can_mean(
    tmp_path: Path,
) -> None:
    """Measured: a local model drew `core/timing.py` for `src/pkg/core/timing.py`."""
    from supervisor_harness.core.placement import placed_in_tree

    write(tmp_path, {"src/pkg/core/timing.py": "", "src/pkg/core/other.py": "",
                     "node_modules/x/core/timing.py": ""})
    paths, notes = placed_in_tree(["core/timing.py", "src/pkg/core/other.py", "src/**"],
                                  tmp_path)

    assert paths == ["src/pkg/core/timing.py", "src/pkg/core/other.py", "src/**"]
    assert len(notes) == 1 and "only file it can mean" in notes[0]


def test_a_path_that_could_mean_several_files_or_none_is_left_and_said(tmp_path: Path) -> None:
    from supervisor_harness.core.placement import placed_in_tree

    write(tmp_path, {"a/util.py": "", "b/util.py": "", "tests/test_a.py": ""})
    paths, notes = placed_in_tree(["util.py", "tests/test_new.py", "reporting/ledger.py"],
                                  tmp_path)

    assert paths == ["util.py", "tests/test_new.py", "reporting/ledger.py"]
    assert any("any of 2" in n for n in notes)
    assert any("directory that does not exist" in n for n in notes)
    assert not any("test_new.py" in n for n in notes), "a new file in a real directory is fine"


async def test_the_plans_envelope_is_placed_before_it_is_granted(supervisor, fake) -> None:  # type: ignore[no-untyped-def]
    from supervisor_harness.models import RunMode

    plan = fake._planning(None)
    plan["envelope_paths"] = ["auth/login.py", "tests/**"]
    fake.overrides["planning"] = plan

    response = await supervisor.run("Review the login handler", mode=RunMode.REPORT)
    state = supervisor.store.load_state(response.run_id)

    assert state.envelope is not None
    assert "src/auth/login.py" in state.envelope.paths
    assert any("only file it can mean" in n.text for n in state.notes)


async def test_a_task_that_declared_no_scope_was_not_narrowed(supervisor, fake) -> None:  # type: ignore[no-untyped-def]
    """Measured: every task in a real run declared none, and every one was sent to the owner."""
    from supervisor_harness.models import RunMode

    synthesis = fake._synthesis(None)
    synthesis["tasks"][0].pop("scope_paths")
    fake.overrides["synthesis"] = synthesis

    response = await supervisor.run("Add rate limiting", mode=RunMode.EXECUTE,
                                    auto_approve=True)
    (task,) = supervisor.store.load_state(response.run_id).tasks.values()
    assert task.scope.paths, "it inherited the envelope"
    assert task.clamped == []


async def test_a_tasks_scope_is_placed_too(supervisor, fake) -> None:  # type: ignore[no-untyped-def]
    from supervisor_harness.models import RunMode

    synthesis = fake._synthesis(None)
    synthesis["tasks"][0]["scope_paths"] = ["auth/login.py", "tests/**"]
    fake.overrides["synthesis"] = synthesis

    response = await supervisor.run("Add rate limiting", mode=RunMode.EXECUTE,
                                    auto_approve=True)
    (task,) = supervisor.store.load_state(response.run_id).tasks.values()
    assert "src/auth/login.py" in task.scope.paths
