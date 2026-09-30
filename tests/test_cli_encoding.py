"""The CLI's output survives a stream that is not UTF-8.

On Windows a piped stdout defaults to the ANSI code page, cp1252, which cannot
encode `→` -- a character the harness itself prints. The first unattended run
driven by another program (security-eval) died on exactly that: `supervisor run
--json` finished the run, wrote every event, and then raised while printing the
result, so the caller saw exit 1 and no JSON for a run that had succeeded and
been paid for.

Reproduced in a subprocess with `PYTHONIOENCODING=cp1252`, which forces the same
stream encoding on every platform, so this guards Linux CI as well as Windows.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_json_output_with_non_ascii_survives_a_cp1252_pipe(tmp_path: Path) -> None:
    workspace = tmp_path / "prøve→arbeid"
    workspace.mkdir()
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    env.pop("PYTHONUTF8", None)
    env.pop("SUPERVISOR_HOME", None)

    proc = subprocess.run(
        [sys.executable, "-m", "supervisor_harness.cli", "init", "--host", "claude",
         "--json", "-w", str(workspace)],
        env=env, capture_output=True, check=False,
    )

    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    printed = json.loads(proc.stdout.decode("utf-8"))
    assert Path(printed["workspace"]) == workspace.resolve()


def test_human_output_with_non_ascii_survives_a_cp1252_pipe(tmp_path: Path) -> None:
    workspace = tmp_path / "prøve→arbeid"
    workspace.mkdir()
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    env.pop("PYTHONUTF8", None)

    proc = subprocess.run(
        [sys.executable, "-m", "supervisor_harness.cli", "init", "--host", "claude",
         "-w", str(workspace)],
        env=env, capture_output=True, check=False,
    )

    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert "→" in proc.stdout.decode("utf-8")
