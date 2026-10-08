"""Materialise a case's workspace in a temporary directory, and clean it up after.

A git fixture is checked out as a detached worktree of the repository, so its
history -- the baseline a run would take -- is there as it would be in a run.
A URL is cloned once into a cache beside the evaluation's own store.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

from .cases import Case, CaseError


def _git(*args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell, no model input
        ["git", *args], cwd=cwd, capture_output=True, text=True,  # noqa: S607 - on PATH
        timeout=600, check=False)
    if done.returncode != 0:
        raise CaseError(f"git {' '.join(args)}: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def _repository(repo: str, cache: Path) -> Path:
    resolved = os.path.expandvars(repo)
    if "${" in resolved or "$" in resolved.split("/")[0]:
        raise CaseError(f"repo {repo!r} names an environment variable that is not set")
    local = Path(resolved)
    if local.is_dir():
        return local
    clone = cache / hashlib.sha256(resolved.encode()).hexdigest()[:16]
    if not clone.is_dir():
        clone.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "--quiet", resolved, str(clone))
    return clone


@contextlib.contextmanager
def materialise(case: Case, cache: Path) -> Iterator[Path]:
    """The case's workspace, for the length of the block."""
    fx = case.fixture
    root = Path(tempfile.mkdtemp(prefix=f"eval-{case.id[:24]}-"))
    workspace = root / "ws"
    repo: Path | None = None
    try:
        if fx.kind == "git":
            repo = _repository(fx.repo, cache)
            _git("worktree", "add", "--quiet", "--detach", str(workspace), fx.commit, cwd=repo)
        elif fx.kind == "dir":
            base = case.source.parent if case.source else Path.cwd()
            shutil.copytree(base / fx.path, workspace)
        else:
            workspace.mkdir()
            for rel, content in fx.files.items():
                target = workspace / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        if fx.patch:
            base = case.source.parent if case.source else Path.cwd()
            # Outside a repository `git apply` patches the directory it runs in.
            _git("apply", "--whitespace=nowarn", str((base / fx.patch).resolve()),
                 cwd=workspace)
        yield workspace
    finally:
        if repo is not None:
            with contextlib.suppress(CaseError):
                _git("worktree", "remove", "--force", str(workspace), cwd=repo)
        shutil.rmtree(root, ignore_errors=True)
