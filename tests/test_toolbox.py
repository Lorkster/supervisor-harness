"""The tools themselves: what they return when they are allowed to work.

Finding **Q-C5** of `docs/quality-assessment.md`: `core/tools.py` sat at 79%,
and the shape of the gap was the interesting part. The fence's *decisions* --
`_scope_refusal`, the floor, the executable allow-list -- are thoroughly covered
by `test_hardening.py`, which is where every refusal this module has ever
learned to make is pinned.

What was uncovered was the other half: **the tools that consult the fence**.
`_walk`, `list_files`, `read_file`, `search`, `write_file`, `run_command` and
the `call` dispatcher between them accounted for 60 of the 65 unreached lines.
So the module knew what to forbid and nothing checked what it did when it
allowed.

That matters beyond coverage arithmetic. A truncation that silently drops
results, a window that returns the wrong lines, or a dispatcher that routes
`write_file` to `read_file` would all pass every refusal test in the suite: the
fence is not consulted differently, it simply guards an operation that answers
wrongly. These are the tests for the answering.
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path

import pytest

from supervisor_harness.config import Policy
from supervisor_harness.core.tools import (
    MAX_LIST,
    MAX_MATCHES,
    MAX_READ_LINES,
    Toolbox,
)
from supervisor_harness.models import AgentKind, AgentSpec, Scope


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A small workspace with the shapes the walk has to make decisions about."""
    (tmp_path / "src" / "auth").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / "src" / "auth" / "login.py").write_text(
        "def login(request):\n    return check(request)\n", encoding="utf-8"
    )
    (tmp_path / "src" / "app.py").write_text("ROUTES = ['/login']\n", encoding="utf-8")
    (tmp_path / "docs" / "readme.md").write_text("# docs\n", encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    return tmp_path


def _box(tree: Path, **policy: object) -> Toolbox:
    return Toolbox(tree, Policy(**policy))  # type: ignore[arg-type]


def _agent(kind: AgentKind, scope: Scope | None = None) -> AgentSpec:
    return AgentSpec(id="agt_1", role="implementer", kind=kind, scope=scope or Scope())


# -- what the walk includes, and what it silently drops ---------------------


def test_the_walk_skips_vcs_directories_and_binaries(tree: Path) -> None:
    """Both exclusions are invisible in the result, so nothing else asserts them.

    A `.git/config` reaching `search` would put repository configuration into an
    agent's context; a PNG would put bytes there. Neither produces an error --
    they would just quietly appear.
    """
    listed = _box(tree).list_files().output

    assert "src/auth/login.py" in listed
    assert "docs/readme.md" in listed
    assert ".git/config" not in listed
    assert "logo.png" not in listed


def test_list_files_filters_by_glob(tree: Path) -> None:
    listed = _box(tree).list_files("src/**/*.py").output

    assert "src/auth/login.py" in listed
    assert "docs/readme.md" not in listed


def test_list_files_says_how_many_it_did_not_show(tree: Path) -> None:
    """Truncation that does not announce itself reads as an empty directory.

    An agent told 200 files exist when there are 260 draws conclusions about
    what a codebase contains from a list that stopped.
    """
    for i in range(MAX_LIST + 12):
        (tree / f"gen_{i:04d}.py").write_text("x = 1\n", encoding="utf-8")

    result = _box(tree).list_files()
    lines = result.output.splitlines()

    assert result.ok
    assert len(lines) == MAX_LIST + 1, "the tail line is missing or the cap moved"
    assert lines[-1].startswith("... and ")
    assert "more" in lines[-1]


def test_list_files_reports_no_match_rather_than_nothing(tree: Path) -> None:
    result = _box(tree).list_files("nothing/matches/*.rs")

    assert result.ok
    assert result.output == "(no files matched)"


# -- read_file: the window, and what it says about the rest -----------------


def test_read_file_numbers_lines_from_the_requested_start(tree: Path) -> None:
    (tree / "many.txt").write_text("\n".join(f"line {i}" for i in range(1, 31)), encoding="utf-8")

    result = _box(tree).read_file("many.txt", start=10, limit=3)

    assert result.ok
    body = result.output.splitlines()
    assert body[0] == "many.txt"
    assert body[1].strip().startswith("10  line 10")
    assert body[3].strip().startswith("12  line 12")
    assert "... (18 more lines)" in result.output


def test_read_file_does_not_claim_more_lines_at_the_end_of_a_file(tree: Path) -> None:
    """The suffix is computed, not appended -- and off-by-one here is invisible."""
    (tree / "short.txt").write_text("a\nb\nc\n", encoding="utf-8")

    result = _box(tree).read_file("short.txt")

    assert result.ok
    assert "more lines" not in result.output


def test_read_file_clamps_a_hostile_window(tree: Path) -> None:
    """`start` and `limit` come from a model, so both are bounded on the way in."""
    (tree / "many.txt").write_text("\n".join(str(i) for i in range(1, 51)), encoding="utf-8")
    box = _box(tree)

    first = box.read_file("many.txt", start=0, limit=1).output.splitlines()[1]
    assert first.strip().startswith("1 "), "start=0 should clamp to the first line"
    assert box.read_file("many.txt", start=-5, limit=1).ok
    assert len(
        box.read_file("many.txt", limit=MAX_READ_LINES * 10).output.splitlines()
    ) <= MAX_READ_LINES + 2


def test_read_file_distinguishes_absent_from_forbidden(tree: Path) -> None:
    """Two different mistakes; an agent that conflates them retries the wrong one."""
    box = _box(tree)

    outside = box.read_file("../secrets.txt")
    absent = box.read_file("src/nope.py")

    assert not outside.ok and "outside the workspace" in outside.output
    assert not absent.ok and "does not exist" in absent.output


# -- search -----------------------------------------------------------------


def test_search_reports_file_line_and_text(tree: Path) -> None:
    result = _box(tree).search("def login")

    assert result.ok
    assert result.output.startswith("src/auth/login.py:1: def login(request):")


def test_search_is_case_insensitive_and_glob_scoped(tree: Path) -> None:
    box = _box(tree)

    assert "login.py" in box.search("DEF LOGIN").output
    assert box.search("def login", glob="docs/**/*").output == "(no matches)"


def test_an_invalid_regex_is_refused_rather_than_raised(tree: Path) -> None:
    """The pattern comes from a model, and `re.error` would end the agent's turn."""
    result = _box(tree).search("unclosed (group")

    assert not result.ok
    assert "invalid pattern" in result.output


def test_search_stops_at_the_match_cap(tree: Path) -> None:
    """The cap is enforced in two places -- inside a file and across files.

    Both are needed and only one is obvious: a single file with a thousand hits
    exercises the inner break, and many files with one hit each exercise the
    outer one.
    """
    (tree / "dense.txt").write_text("needle\n" * (MAX_MATCHES * 3), encoding="utf-8")
    inner = _box(tree).search("needle")
    assert len(inner.output.splitlines()) == MAX_MATCHES

    (tree / "dense.txt").unlink()
    for i in range(MAX_MATCHES + 10):
        (tree / f"f_{i:03d}.txt").write_text("needle\n", encoding="utf-8")
    outer = _box(tree).search("needle")
    assert len(outer.output.splitlines()) == MAX_MATCHES


# -- write_file -------------------------------------------------------------


def test_write_file_creates_missing_parents(tree: Path) -> None:
    """An agent asked to add a test writes to a directory that does not exist yet."""
    result = _box(tree).write_file("tests/unit/test_login.py", "def test(): pass\n")

    assert result.ok
    assert (tree / "tests" / "unit" / "test_login.py").read_text(encoding="utf-8")
    assert "wrote tests/unit/test_login.py" in result.output


def test_write_file_reports_the_byte_count_it_wrote(tree: Path) -> None:
    result = _box(tree).write_file("src/new.py", "abc")

    assert result.ok
    assert "(3 bytes)" in result.output


def test_a_scope_with_no_paths_does_not_bound_the_write(tree: Path) -> None:
    """An empty `paths` is "unset", not "nothing" -- the floor is what holds.

    `if scope.paths and not matches_any(...)` is the line that decides this, and
    reading it the other way would refuse every write by an agent whose task
    named no paths.
    """
    result = _box(tree).write_file("src/app.py", "X = 1\n", Scope(paths=[]))

    assert result.ok


def test_a_forbidden_path_outranks_an_allowed_one(tree: Path) -> None:
    scope = Scope(paths=["src/**"], forbidden_paths=["src/auth/**"])
    box = _box(tree)

    assert box.write_file("src/app.py", "X = 1\n", scope).ok
    refused = box.write_file("src/auth/login.py", "X = 1\n", scope)

    assert not refused.ok
    assert "forbidden path" in refused.output


# -- run_command ------------------------------------------------------------


def test_run_command_is_off_unless_policy_turns_it_on(tree: Path) -> None:
    result = _box(tree).run_command("python noop.py")

    assert not result.ok
    assert "command execution is disabled" in result.output
    assert "policy.allow_command_execution" in result.output


# Every command below runs a *file* in the workspace rather than `python -c`.
# That is not a workaround: the fence refuses inline source to every agent, so a
# test written with `-c` tests the refusal it already has and never reaches the
# execution path this section exists to cover. It is also how these tests stay
# honest about what an agent can actually run.


def test_run_command_reports_the_exit_code_and_output(tree: Path) -> None:
    """`ok` follows the exit code, which is what a definition of done reads."""
    (tree / "ok.py").write_text("print('hello')\n", encoding="utf-8")
    box = _box(tree, allow_command_execution=True)

    result = box.run_command("python ok.py")

    assert result.ok, result.output
    assert "exit=0" in result.output
    assert "hello" in result.output


def test_a_failing_command_is_reported_rather_than_raised(tree: Path) -> None:
    (tree / "boom.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    box = _box(tree, allow_command_execution=True)

    result = box.run_command("python boom.py")

    assert not result.ok
    assert "exit=3" in result.output


def test_a_command_that_overruns_its_timeout_is_stopped(tree: Path) -> None:
    """The bound has to stop the command, not merely report that it should have.

    Writing this found a real defect. `run_command` used `shell=True`, so the
    timeout killed the *shell*; on Windows `cmd /c` is a different process from
    the program it launched, which kept running and held the pipes open, so
    `subprocess.run` blocked until the program finished and only then raised.

    Measured before the fix: a 20-second sleep under a **1-second** timeout
    returned `TimeoutExpired` after **20.1 seconds**, and after 1.0 without the
    shell. `policy.command_timeout_seconds` was therefore not a bound at all --
    an agent could hold a run open as long as it liked while the harness
    reported that it had been stopped.

    So the elapsed time is the assertion. Checking only for "timed out" passes
    on the broken version, which is exactly how this survived.
    """
    (tree / "slow.py").write_text("import time; time.sleep(30)\n", encoding="utf-8")
    box = _box(tree, allow_command_execution=True, command_timeout_seconds=1)

    started = time.monotonic()
    result = box.run_command("python slow.py")
    elapsed = time.monotonic() - started

    assert not result.ok
    assert "timed out" in result.output
    # A 15x margin on a 1s bound against a 30s command: loose enough for a
    # loaded CI box, tight enough that a return to `shell=True` cannot pass.
    assert elapsed < 15, f"the timeout did not bound the command: {elapsed:.1f}s"


def test_run_command_does_not_go_through_a_shell(tree: Path) -> None:
    """The structural half, because the timing half needs a slow machine to fail.

    The fence refuses every unquoted metacharacter and glob before this point,
    so the command is a program and its arguments, and a shell adds nothing but
    the process-tree problem above. `dod.verify_command` has always run its
    commands this way; this brings the toolbox into line with it.
    """
    code = [
        line for line in inspect.getsource(Toolbox.run_command).splitlines()
        if not line.lstrip().startswith("#")
    ]

    assert any("shell=False" in line for line in code)
    # Comment lines are excluded on purpose: the method's own comment explains
    # what `shell=True` did wrong, and a naive substring search over the whole
    # source therefore fails against correct code. Prose that quotes the thing
    # it forbids has now caught this project out twice.
    assert not any("shell=True" in line for line in code)


# -- dispatch ---------------------------------------------------------------


def test_call_routes_each_tool_to_the_operation_of_that_name(tree: Path) -> None:
    """A dispatcher that crossed two wires would still pass every fence test."""
    box = _box(tree)
    agent = _agent(AgentKind.ANALYSIS)

    assert box.call("list_files", {"pattern": "src/**/*.py"}, agent).tool == "list_files"
    assert "login.py" in box.call("list_files", {"pattern": "src/**/*.py"}, agent).output
    assert box.call("read_file", {"path": "src/app.py"}, agent).output.startswith("src/app.py")
    assert "src/auth/login.py:1" in box.call("search", {"pattern": "def login"}, agent).output


def test_call_coerces_the_arguments_a_model_actually_sends(tree: Path) -> None:
    """`start` and `limit` arrive as strings or nulls often enough to matter."""
    (tree / "many.txt").write_text("\n".join(str(i) for i in range(1, 21)), encoding="utf-8")
    box = _box(tree)

    result = box.call("read_file", {"path": "many.txt", "start": "5", "limit": None},
                      _agent(AgentKind.ANALYSIS))

    assert result.ok
    assert result.output.splitlines()[1].strip().startswith("5  5")


def test_an_unknown_tool_is_named_in_its_own_refusal(tree: Path) -> None:
    result = _box(tree).call("delete_everything", {}, _agent(AgentKind.EXECUTION))

    assert not result.ok
    assert "no such tool" in result.output
    assert "delete_everything" in result.output


@pytest.mark.parametrize("kind", [AgentKind.ANALYSIS, AgentKind.VERIFICATION, AgentKind.SYNTHESIS])
def test_only_execution_agents_reach_write_or_run_through_dispatch(
    tree: Path, kind: AgentKind
) -> None:
    """The refusal is `call`'s, before the operation is consulted at all.

    `test_hardening.py` pins that these agents are refused; this pins *where*.
    If the check moved inside `write_file`, a direct call would bypass it -- and
    `run_command` has already had exactly that defect once.
    """
    box = _box(tree, allow_command_execution=True)
    agent = _agent(kind)

    write = box.call("write_file", {"path": "src/app.py", "content": "X = 1\n"}, agent)
    run = box.call("run_command", {"command": "python noop.py"}, agent)

    assert not write.ok and "may not modify files" in write.output
    assert not run.ok and "may not run commands" in run.output
    assert (tree / "src" / "app.py").read_text(encoding="utf-8") == "ROUTES = ['/login']\n"
