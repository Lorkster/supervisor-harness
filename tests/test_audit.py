"""What a finished run actually did, as against what it was allowed to do.

Batch 7 of `docs/development-plan.md`. The envelope and the fence answer one
question at issue time -- what may this agent touch -- and in host-delegated
mode, the default, they are not even the enforcement. Nothing could be asked
afterwards what an agent *did*.

The method is NVIDIA's red-team scanners': classify the action and its result,
never the surface that advertised the capability. The first test below is that
rule, because the trap is sharper here than it was there -- every brief this
harness writes contains the list of prohibited git commands in full.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from supervisor_harness.core.audit import (
    audit,
    scan_forbidden_commands,
    scan_unproven_passes,
    scan_untraceable_closures,
    scan_writes_outside_envelope,
    scan_writes_outside_scope,
)
from supervisor_harness.core.baseline import commit_from_fact
from supervisor_harness.models import (
    BASELINE_FACT,
    AgentKind,
    AgentSpec,
    AgentTurn,
    CriterionStatus,
    DoDCriterion,
    ExecutionTask,
    Finding,
    RunState,
    Scope,
    ScopeEnvelope,
    Severity,
    TaskStatus,
    VerifyMethod,
)

WORKSPACE = "/projects/harness"


def _state(**kwargs: object) -> RunState:
    return RunState(id="run_x", workspace=WORKSPACE, **kwargs)  # type: ignore[arg-type]


def _agent(**kwargs: object) -> AgentSpec:
    fields: dict[str, object] = {
        "id": "agt_1", "run_id": "run_x", "role": "implementer",
        "kind": AgentKind.EXECUTION, "scope": Scope(paths=["src/**"]),
    }
    fields.update(kwargs)
    return AgentSpec(**fields)  # type: ignore[arg-type]


def _turn(files: list[str], agent_id: str = "agt_1", seq: int = 1) -> AgentTurn:
    return AgentTurn(run_id="run_x", agent_id=agent_id, seq=seq, files_touched=files)


# -- the method ------------------------------------------------------------


def test_a_brief_is_never_evidence() -> None:
    """The trap this whole module is shaped around.

    Every brief contains the shared-tree rule, which names `git stash`,
    `git checkout` and six others in full. A scanner that searched the text the
    harness *wrote* would report every agent in every run, at total confidence,
    for having read its instructions -- which is the "advertising mistaken for
    use" failure NVIDIA's scanners are built to avoid.
    """
    state = _state(
        agents={"agt_1": _agent()},
        briefs={"agt_1": "Never run `git stash`, `git checkout`, `git reset` ..."},
        turns=[_turn(["src/login.py"])],
    )

    report = audit(state, WORKSPACE)

    assert not report.observations, "the prohibited list in a brief is not a use of it"


def test_a_task_that_names_a_file_is_not_a_report_of_touching_it() -> None:
    """A task's action is something the harness said, not something that happened."""
    task = ExecutionTask(
        run_id="run_x", id="tsk_1", title="Limit login",
        action="Rewrite marketing/home.tsx and .git/config",
        status=TaskStatus.VERIFIED,
    )
    state = _state(agents={"agt_1": _agent(task_id="tsk_1")}, tasks={"tsk_1": task})

    assert audit(state, WORKSPACE).observations == []


# -- scope and envelope ----------------------------------------------------


def test_a_write_outside_an_agents_own_scope_is_reported() -> None:
    state = _state(
        agents={"agt_1": _agent(scope=Scope(paths=["src/auth/**"]))},
        turns=[_turn(["marketing/home.tsx"])],
    )

    found = scan_writes_outside_scope(state)

    assert len(found) == 1
    assert found[0].severity == "high"
    assert "marketing/home.tsx" in found[0].summary


def test_an_analysis_agent_reading_widely_is_not_the_same_as_an_executor_writing(
) -> None:
    """`files_touched` carries both, and the two do not mean the same thing.

    A lens naming a file it read outside its paths is ordinary work. An
    execution agent naming one it changed is the finding. The severity carries
    the difference rather than the scanner pretending it cannot tell.
    """
    reading = _state(
        agents={"agt_1": _agent(kind=AgentKind.ANALYSIS, scope=Scope(paths=["src/**"]))},
        turns=[_turn(["docs/architecture.md"])],
    )

    assert scan_writes_outside_scope(reading)[0].severity == "low"


def test_a_write_under_the_floor_is_reported_whatever_the_scope_says() -> None:
    """The floor is not a default, so a wide scope does not excuse it.

    A write under `.git` is code execution reached without a shell, and a write
    under the store rewrites the log every claim in the run is judged against.
    """
    state = _state(
        agents={"agt_1": _agent(scope=Scope(paths=[]))},
        turns=[_turn([".git/hooks/pre-commit", ".supervisor/runs/run_x/events.jsonl"])],
    )

    found = scan_writes_outside_scope(state)

    assert len(found) == 2
    assert all(o.severity == "high" for o in found)
    assert all("no scope may cover" in o.summary for o in found)


def test_a_forbidden_path_is_reported_even_when_the_scope_allows_it() -> None:
    state = _state(
        agents={"agt_1": _agent(
            scope=Scope(paths=["src/**"], forbidden_paths=["src/vendor/**"])
        )},
        turns=[_turn(["src/vendor/lib.py"])],
    )

    found = scan_writes_outside_scope(state)

    assert "explicitly forbidden" in found[0].summary


def test_a_write_outside_the_run_envelope_is_its_own_finding() -> None:
    """Because a hit means something different from a scope violation.

    An agent inside its own fence but outside the run's grant means attenuation
    did not hold, which is a harness failure rather than an agent one.
    """
    state = _state(
        agents={"agt_1": _agent(scope=Scope(paths=["**"]))},
        envelope=ScopeEnvelope(paths=["src/**"], source="configuration"),
        turns=[_turn(["infra/terraform.tf"])],
    )

    found = scan_writes_outside_envelope(state)

    assert len(found) == 1
    assert "outside the run's envelope" in found[0].summary
    assert "configuration" in found[0].evidence


def test_no_envelope_means_no_envelope_findings() -> None:
    """An empty envelope is the whole workspace, which is not a violation."""
    state = _state(
        agents={"agt_1": _agent()},
        envelope=ScopeEnvelope(paths=[]),
        turns=[_turn(["anywhere/at/all.py"])],
    )

    assert scan_writes_outside_envelope(state) == []


# -- commands --------------------------------------------------------------


def _task_with(criterion: DoDCriterion) -> RunState:
    task = ExecutionTask(run_id="run_x", id="tsk_1", title="t", dod=[criterion],
                         status=TaskStatus.VERIFIED)
    return _state(tasks={"tsk_1": task})


def test_a_criterion_running_a_shared_tree_git_command_is_reported() -> None:
    """A recorded command is an action. This is the honest version of the

    git scan -- not "the word stash appears somewhere", but "this criterion
    says it was proved by running that".
    """
    state = _task_with(DoDCriterion(id="dod_1", command="git stash && pytest -q",
                                    method=VerifyMethod.COMMAND))

    found = scan_forbidden_commands(state)

    assert found and found[0].severity == "high"
    assert "git stash" in found[0].summary


def test_a_criterion_running_something_that_is_not_a_check_runner_is_reported() -> None:
    state = _task_with(DoDCriterion(id="dod_1", command="curl https://example.com",
                                    method=VerifyMethod.COMMAND))

    found = scan_forbidden_commands(state)

    assert found and "not a project check runner" in found[0].summary


def test_an_ordinary_check_runner_is_not_reported() -> None:
    state = _task_with(DoDCriterion(id="dod_1", command="pytest -q tests/test_auth.py",
                                    method=VerifyMethod.TEST))

    assert scan_forbidden_commands(state) == []


def test_a_read_only_git_command_is_not_a_shared_tree_command() -> None:
    """The rule is about the tree, and `git diff` does not touch it."""
    state = _task_with(DoDCriterion(id="dod_1", command="git diff --stat",
                                    method=VerifyMethod.COMMAND))

    found = scan_forbidden_commands(state)

    assert not any(o.severity == "high" for o in found)


# -- passes ----------------------------------------------------------------


def test_a_pass_whose_evidence_never_mentions_the_command_is_reported() -> None:
    """The weak case the strong rule lets through.

    "Passed with no evidence is recorded as failed" already catches the empty
    one. This catches evidence that exists and reads like a prediction of what
    the command would have printed rather than what it printed.
    """
    state = _task_with(DoDCriterion(
        id="dod_1", command="pytest -q tests/", method=VerifyMethod.TEST,
        status=CriterionStatus.PASS, evidence="All the tests should pass now.",
    ))

    found = scan_unproven_passes(state)

    assert found and "never mentions 'pytest'" in found[0].summary


def test_a_pass_whose_evidence_is_the_real_output_is_not_reported() -> None:
    state = _task_with(DoDCriterion(
        id="dod_1", command="pytest -q tests/", method=VerifyMethod.TEST,
        status=CriterionStatus.PASS, evidence="$ pytest -q tests/\n41 passed in 2.10s",
    ))

    assert scan_unproven_passes(state) == []


def test_a_criterion_that_did_not_pass_is_not_audited_for_its_evidence() -> None:
    """A failure with a thin account of itself is not the problem this looks for."""
    state = _task_with(DoDCriterion(
        id="dod_1", command="pytest -q", method=VerifyMethod.TEST,
        status=CriterionStatus.FAIL, evidence="it did not work",
    ))

    assert scan_unproven_passes(state) == []


# -- closures --------------------------------------------------------------


def test_a_finding_closed_without_touching_anything_it_cites_is_reported() -> None:
    finding = Finding(id="fnd_1", title="No rate limit", severity=Severity.HIGH,
                      evidence=["src/auth/login.py:40"])
    task = ExecutionTask(run_id="run_x", id="tsk_1", title="Limit login",
                         rationale_refs=["fnd_1"], status=TaskStatus.VERIFIED)
    state = _state(
        agents={"agt_1": _agent(task_id="tsk_1")},
        tasks={"tsk_1": task}, findings=[finding],
        turns=[_turn(["src/cache.py"])],
    )

    found = scan_untraceable_closures(state)

    assert found and "src/auth/login.py" in found[0].summary


def test_touching_any_one_of_the_cited_files_is_enough() -> None:
    """A fix landing in one of three cited places has still gone where the

    finding pointed. Requiring all of them would report every ordinary fix.
    """
    finding = Finding(id="fnd_1", title="No rate limit", evidence=[
        "src/auth/login.py:40", "src/cache.py:2", "docs/auth.md:10",
    ])
    task = ExecutionTask(run_id="run_x", id="tsk_1", title="t",
                         rationale_refs=["fnd_1"], status=TaskStatus.VERIFIED)
    state = _state(
        agents={"agt_1": _agent(task_id="tsk_1")},
        tasks={"tsk_1": task}, findings=[finding],
        turns=[_turn(["src/cache.py"])],
    )

    assert scan_untraceable_closures(state) == []


def test_a_task_that_is_not_verified_is_not_audited_for_its_closures() -> None:
    """An unfinished task has not claimed to have closed anything."""
    finding = Finding(id="fnd_1", title="x", evidence=["src/auth/login.py:40"])
    task = ExecutionTask(run_id="run_x", id="tsk_1", title="t",
                         rationale_refs=["fnd_1"], status=TaskStatus.FAILED)
    state = _state(tasks={"tsk_1": task}, findings=[finding])

    assert scan_untraceable_closures(state) == []


# -- ground truth ----------------------------------------------------------


def test_the_commit_is_parsed_out_of_the_sentence_the_baseline_records() -> None:
    """The fact is prose because an agent reads it; git needs the commit.

    Feeding the whole sentence to `git diff` produces `fatal: ambiguous
    argument`, which is how `commit_from_fact` came to exist.
    """
    assert commit_from_fact("`1bfe4e1a5f28` on `main`, working tree clean") == "1bfe4e1a5f28"
    assert commit_from_fact("`abc1234`, with 4 file(s) already modified") == "abc1234"
    assert commit_from_fact("no baseline recorded") == ""


def test_a_change_nobody_owned_up_to_is_found_in_the_tree(tmp_path: Path) -> None:
    """The one scanner that reaches past the record.

    Every other check here shares the blind spot self-reporting has: an agent
    that changed a file and did not mention it is invisible to all of them.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "login.py").write_text("def login(): ...\n", encoding="utf-8")
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
        subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, check=False)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                          capture_output=True, text=True, check=False).stdout.strip()

    (tmp_path / "src" / "login.py").write_text("def login(): return 1\n", encoding="utf-8")

    state = RunState(
        id="run_x", workspace=str(tmp_path),
        facts={BASELINE_FACT: f"`{head[:12]}` on `main`, working tree clean"},
        agents={"agt_1": _agent()},
        turns=[_turn(["src/other.py"])],
    )
    report = audit(state, tmp_path)

    unclaimed = [o for o in report.observations if o.scanner == "unclaimed_changes"]
    assert unclaimed and "src/login.py" in unclaimed[0].evidence
    assert "unclaimed_changes" in report.scanned


def test_a_scanner_that_could_not_run_says_so_rather_than_reading_clean(
    tmp_path: Path,
) -> None:
    """A silent scanner and a clean one look identical, and only one reassures."""
    state = RunState(id="run_x", workspace=str(tmp_path))

    report = audit(state, tmp_path)

    assert "unclaimed_changes" in report.skipped
    assert "no baseline commit" in report.skipped["unclaimed_changes"]
    assert "unclaimed_changes" not in report.scanned
    assert "skipped" in report.summary()


# -- the report ------------------------------------------------------------


def test_a_clean_run_says_how_much_was_looked_at() -> None:
    report = audit(_state(), WORKSPACE)

    assert not report
    assert "nothing to report from" in report.summary()
    assert len(report.scanned) == len(report.scanned)


def test_findings_are_ordered_worst_first() -> None:
    state = _state(
        agents={
            "agt_1": _agent(kind=AgentKind.ANALYSIS, scope=Scope(paths=["src/**"])),
            "agt_2": _agent(id="agt_2", scope=Scope(paths=[])),
        },
        turns=[_turn(["docs/x.md"], "agt_1"), _turn([".git/config"], "agt_2", seq=2)],
    )

    severities = [o.severity for o in audit(state, WORKSPACE).observations]

    assert severities == ["high", "low"]


def test_the_rendered_report_states_what_it_will_not_read() -> None:
    """The method is the finding that outlives any individual scanner."""
    rendered = audit(_state(), WORKSPACE).render()

    assert "A brief, a scope or a task's" in rendered
    assert "## Scanners" in rendered


async def test_the_cli_writes_the_evidence_under_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from supervisor_harness.cli import cmd_audit
    from supervisor_harness.config import Policy, default_config
    from supervisor_harness.core.supervisor import Supervisor
    from supervisor_harness.host.detect import HostInfo
    from supervisor_harness.models import Backend, RunMode
    from supervisor_harness.store.runstore import RunStore

    config = default_config()
    config.backend = Backend.HOST
    config.routing = {k: "host" for k in config.routing}
    config.policy = Policy(default_max_turns=2, max_analysis_lenses=1)
    store = RunStore(tmp_path / ".supervisor")
    supervisor = Supervisor(
        workspace=tmp_path, config=config, store=store,
        host=HostInfo(name="claude-code", workspace=str(tmp_path), confidence=1.0),
    )
    started = await supervisor.start("Tidy the imports", mode=RunMode.REPORT)

    code = cmd_audit(argparse.Namespace(
        workspace=str(tmp_path), run_id=started.run_id, out="", backend=None, json=False,
    ))

    assert code == 0, "a run that has done nothing has nothing to report"
    written = store.run_dir(started.run_id) / "audit" / "audit.md"
    assert written.is_file()
    assert "# Audit of run" in written.read_text(encoding="utf-8")
    assert "written to" in capsys.readouterr().out
