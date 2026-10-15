"""A requirement the plan left untested goes to the nearest tester after its builder.

Minesweeper run 18 verified every task and failed five of the owner's hidden
browser tests. The plan, sent back once for it, still gave no task the tests of
R11 to R50: its two testing tasks "delivered" R52 instead. None of batch L's
checks or batch M's fence applied, and the browser tests asserted that one cell
animates where the requirement says every revealed cell does, in order.
"""

from __future__ import annotations

from supervisor_harness.core.requirements import attach_requirements
from supervisor_harness.models import AcceptanceTests, ExecutionTask, Requirement, Scope


def _run18() -> tuple[dict[str, ExecutionTask], list[Requirement]]:
    scaffold = ExecutionTask(title="Scaffold", requirements=["R1"],
                             scope=Scope(paths=["package.json"]))
    engine = ExecutionTask(title="Engine", requirements=["R11"], depends_on=[scaffold.id],
                           scope=Scope(paths=["src/game/engine.ts"]))
    page = ExecutionTask(title="Page", requirements=["R26"], depends_on=[engine.id],
                         scope=Scope(paths=["src/main.ts"]))
    unit = ExecutionTask(title="Unit tests", requirements=["R52"], depends_on=[engine.id],
                         scope=Scope(paths=["tests/**"]),
                         acceptance=[AcceptanceTests(["R52"], "npm test")])
    e2e = ExecutionTask(title="Browser tests", requirements=["R52"], depends_on=[page.id],
                        scope=Scope(paths=["e2e/**"]),
                        acceptance=[AcceptanceTests(["R52"], "npm run test:e2e")])
    gate = ExecutionTask(title="Final gate", requirements=["R51"],
                         depends_on=[unit.id, e2e.id, page.id], scope=Scope(paths=["dist/**"]),
                         acceptance=[AcceptanceTests(["R1", "R51"], "npm run check")])
    tasks = {t.title: t for t in (scaffold, engine, page, unit, e2e, gate)}
    ledger = [Requirement(id=rid, quote=f"requirement {rid}")
              for rid in ("R1", "R11", "R26", "R51", "R52")]
    return tasks, ledger


def test_each_goes_to_the_nearest_tester_after_its_builder() -> None:
    tasks, ledger = _run18()

    _, notes = attach_requirements(list(tasks.values()), ledger, {})

    assert "R11" in tasks["Unit tests"].acceptance[0].requirements, "it waits on the engine"
    assert "R11" not in tasks["Browser tests"].acceptance[0].requirements, "farther away"
    assert "R26" in tasks["Browser tests"].acceptance[0].requirements, (
        "the gate also waits on the page directly: the earlier in the plan")
    assert "R26" not in tasks["Final gate"].acceptance[0].requirements
    assert any("tests R11, which the plan left untested: the nearest testing task after "
               "'Engine'" in n for n in notes[tasks["Unit tests"].id])


def test_then_batch_l_and_m_apply_to_it() -> None:
    tasks, ledger = _run18()
    attach_requirements(list(tasks.values()), ledger, {})

    e2e = tasks["Browser tests"]
    assert any(c.statement == "A test is marked [R26]" for c in e2e.dod)
    assert any(c.statement.startswith("The tests marked R26") for c in e2e.dod), "the review"
    assert e2e.scope.unread_paths == ["src/main.ts"], "the page's code not shown"
    assert "e2e/**" in tasks["Page"].scope.forbidden_paths


def test_what_is_left_as_it_is() -> None:
    tasks, ledger = _run18()
    ledger.append(Requirement(id="R60", quote="built by nobody"))
    ledger.append(Requirement(id="R61", quote="not tested"))
    tasks["Engine"].requirements.append("R61")
    tasks["Final gate"].requirements.append("R62")
    ledger.append(Requirement(id="R62", quote="no tester waits on its builder"))
    tasks["Final gate"].acceptance = []
    reasons = {"set_aside": [{"requirement": "R11", "reason": "later"}],
               "not_tested": [{"requirement": "R61", "reason": "by construction"}]}

    attach_requirements(list(tasks.values()), ledger, reasons)

    tested = {rid for t in tasks.values() for g in t.acceptance for rid in g.requirements}
    assert not tested & {"R11", "R60", "R61", "R62"}
