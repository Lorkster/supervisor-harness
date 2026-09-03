# Quality assessment — architecture, code quality, testing

Item **4a** of [`next-three.md`](next-three.md). The scope came with the 9c
decision: *"after all changes we need to make sure the entire application
adheres to best practices for architecture, code quality and testing."*

This is the assessment, not the work. It says what the criteria are, what was
measured against them, what was found, and in what order the findings should be
closed. The work is item **4b**, and it is expected to take several batches.

Findings carry ids (`Q-A1`, `Q-C2`, …) so a commit can say `Closes: Q-C1` and be
reconciled against this document, the way `remediation-plan.md` already works.

**Nothing here is a reading.** Every claim below is a number produced by a tool
in this repository, and the tools are now in CI so the numbers stay honest.

---

## What "best practices" means here, before anything is measured

Written first, deliberately. Assessing a codebase against criteria invented
while looking at it produces a description of the codebase, not a judgement of
it.

**Architecture**
1. Package dependencies form a DAG. No cycles between packages.
2. A module's size is a smell, not a defect; a *function's* complexity is the
   defect. Complexity is measured, not eyeballed.
3. The layers are `store` (durability) → `providers` (model access) →
   `core` (supervision) → the entry points (`cli`, `mcp_server`). Dependencies
   point inward-to-outward once and not back.

**Code quality**
4. The linter is configured, not defaulted. A rule is on because someone decided
   it, and the set is reachable — a permanent backlog is not a standard.
5. Suppressions are meaningful. A `# noqa` for a rule that is not enabled is
   noise that reads as diligence.
6. Resources acquired are released. A leak that only appears under
   `-W always` is still a leak.
7. Types are checked, and the check gates at zero.

**Testing**
8. Coverage is measured and cannot silently fall.
9. Coverage is not the standard — a test that cannot fail is worse than no test.
   The bar is the sabotage check this project already applies: disable the
   mechanism, and something must go red.
10. The boundaries a user actually touches are tested. An untested entry point
    is untested software however well covered its internals are.

---

## The instruments, now in CI

All three landed with this assessment, so every number below can be reproduced
and none of them can rot quietly.

| what | how | gate |
| --- | --- | --- |
| Coverage | `pytest --cov=supervisor_harness --cov-fail-under=82` | a **floor**; fails if it drops |
| Types | `mypy` (config in `pyproject.toml`) | **zero**, from the start |
| Lint | `tools/ruff_diff.py`, now against a configured rule set | **no new** (file, rule) pairs |
| Doc references | `tools/check_doc_refs.py` | zero |

Two notes on what changed in the instruments themselves:

- **Ruff is configured at last** (`[tool.ruff]` in `pyproject.toml`). It had
  never been, which is why the gate was written to tolerate a baseline.
- **`tools/ruff_diff.py` had a real flaw**, found by being the first change to
  touch ruff configuration: it ran `ruff check` separately in each tree, so each
  used *its own* config. Any pull request changing the rule set therefore
  compared two different rule sets — reporting every newly-enabled rule as a
  regression, and, worse, reporting nothing at all for a change that *disabled*
  a rule. Both trees are now measured with the working tree's configuration.
  Recorded as **Q-Q4**, closed here.

---

## Two things the measurement overturned

Worth stating before the findings, because both were assumptions this project's
own planning documents had written down.

**The codebase is almost fully typed.** `next-three.md` said "Type coverage is
unmeasured; there is no `mypy`/`pyright` in CI at all" — true, and it read as a
risk. The reality: **3 errors across 40 files**, all fixed in this batch, so
mypy gates at zero from day one rather than needing a baseline. `--strict` costs
14 more errors in 8 files (`Q-T2`).

**`core/supervisor.py` is well tested, and the complexity is not in it.** This
is the more important correction, because it bears on item 2.

The argument for scheduling this assessment *before* the split was mine, and it
rested on: *"there is no coverage measurement, so nobody knows what fraction of
a 2,584-line module those tests exercise — and the uncovered parts are exactly
where a silent break would hide."* The ordering was still right, for the second
reason given at the time — *how* to split is an architecture question and the
criteria should precede the design call. But the fear behind the first reason
was unfounded and should not be repeated:

| | |
| --- | --- |
| `core/supervisor.py` coverage | **94%** (59 of 952 statements missing) |
| its complexity findings | **3**, worst `_drive_agent` at 14 |
| `store/events.py:99` | **123 statements, 48 branches** — the worst function in the codebase |
| `core/journal.py:130` | complexity **26**, 86 statements |
| `core/phases.py:980` | complexity **25**, 75 statements |

So the split is a **file-size fix, not a complexity fix**. That does not overturn
the decision — the stated reason was the maintainability of a 2,584-line class,
which stands, and a well-covered module is a *better* candidate for a mechanical
move, not a worse one. But it should not be sold as reducing complexity, and the
functions above are a separate and arguably more valuable piece of work.

---

## Findings

### Architecture

**Q-A1 · A package cycle between `core` and `agents`.**
`core` imports `agents.registry`, `agents.roles` and `agents.brief`; `agents`
imports back into `core` in exactly one place — `agents/brief.py:15`, for the
constant `BASELINE_FACT` from `core.baseline`. One constant creates the cycle.
Moving it (to `models`, or to a constants module) breaks it outright.
*Criterion 1. Cost: trivial.*

**Q-A2 · Complexity is concentrated outside the module scheduled for splitting.**
The table above. `store/events.py`'s fold at 48 branches is the extreme; the
journal builder, the final-report renderer and `cli.py`'s largest command follow.
None of these is in `core/supervisor.py`.
*Criterion 2. Cost: medium, and it is real refactoring rather than moving.*

**Q-A3 · `cli.py` is the second-largest module and the least tested of the
large ones** — 939 lines, 582 statements, **42%** covered, four complexity
findings including a 19-branch `cmd_status` and an 85-statement function.
It is also a boundary users touch directly.
*Criteria 2, 3, 10. Cost: medium.*

### Testing and coverage

Baseline: **82.25%**, 1,032 of 5,813 statements missing, 335 tests, 2.9
assertions per test.

**Q-C1 · `mcp_server.py` is 0% covered.** 101 statements, none executed by any
test. This is the boundary Claude Code and Cursor actually talk to — the packets,
the tool surface, the delegation handoff. Everything behind it is well tested;
the thing itself is not tested at all.
*Criterion 10. Cost: medium. **This is the most serious finding here.***

**Q-C2 · `cli.py` is 42% covered** — 339 missing statements, the largest
absolute gap in the codebase. The other user-facing boundary.
*Criterion 10. Cost: medium.*

**Q-C3 · The HTTP providers are barely tested.**
`openrouter` 26%, `ollama` 27%, `anthropic` 30% — request building, response
mapping, error translation and retryability all largely unexercised. The
contrast is instructive: `providers/bedrock.py`, written in the previous batch
to the current bar, is at **93%** with 13 sabotage-checked behaviours. The older
three predate that bar.
*Criteria 8, 9. Cost: low — `test_bedrock_provider.py` is a template.*

**Q-C4 · Three modules between 57% and 71%:** `agents/registry.py` 57%,
`providers/router.py` 67%, `host/detect.py` 71%.
*Criterion 8. Cost: low.*

**Q-C5 · `core/tools.py` is 79%, and the gap is in the fence.** 65 missing
statements concentrated in the workspace-walking and scope-refusal paths. This
module is the deterministic guardrail the whole design rests on, so it should be
the best-covered file in the repository rather than the fifth-worst.
*Criteria 8, 10. Cost: low. **Prioritise above its size.***

**Q-C6 · The suite has never been audited for vacuous tests as a whole.**
The sabotage check has been applied to each new batch since it was adopted, and
has caught five vacuous tests. It has never been run backwards over the tests
that predate it. Unknown, not bad — but unknown.
*Criterion 9. Cost: high; do it per-module alongside the other findings rather
than as a sweep.*

### Code quality

**Q-Q1 · 88 ruff findings against the newly configured rule set.**
The set is `E, F, W, I, UP, B, SIM, RUF, S` at line length 100, chosen by
measurement — see below. Breakdown: 45 `E501`, 17 `RUF100`, 6 `UP037`, 3
`SIM105`, 3 `S105`, 2 each of `S110`/`UP017`/`RUF022`, and seven singles.
*Criterion 4. Cost: low, and largely mechanical.*

**Q-Q2 · 17 of those 88 are `# noqa` directives for rules that were never
enabled** — `E402`, `S602`, `S603`, `S608`, `C901`, `BLE001`. They read as
diligence and suppress nothing. Enabling `S` in this batch makes some of them
meaningful; the rest should be deleted or their rules turned on.
*Criterion 5. Cost: trivial.*

**Q-Q3 · SQLite connections are never closed.** `RunIndex.close()` exists at
`store/index.py:118` and **nothing in the codebase calls it** — not `RunStore`,
which has no close method at all, not `cli.py`, not `mcp_server.py`. A suite run
under `-W always` reports **90 unclosed connections**. Invisible by default,
which is why it has survived; the new coverage job surfaces them, since
pytest-cov enables the warning.

The consequence is worst on Windows, where an open handle blocks deleting the
file — and this is the platform the harness is developed on.
*Criterion 6. Cost: low. **A real defect, not a style finding.***

**Q-Q4 · The lint gate compared different rule sets across a configuration
change.** Described above. **Closed in this batch.**

### Types

**Q-T1 · Three type errors.** `serde.py:80` (a genuine narrowing bug —
`is_dataclass` admits instances as well as classes), `store/events.py:248`
(variable reuse, not a defect), `mcp_server.py:32` (SDK stub gap).
**Closed in this batch**, so mypy could gate at zero.

**Q-T2 · `--strict` costs 14 errors in 8 files.** Not adopted here; a decision
worth taking deliberately once 4b is under way, because strict mode's value is
mostly in *new* code and the cheapest moment to adopt it is when the files are
being touched anyway.
*Criterion 7. Cost: low, but it is a policy choice.*

---

## How the ruff rule set was chosen

By measuring candidates rather than by preference. Findings across
`src`, `tests` and `tools`:

| rule set | @88 (default) | @96 | @100 |
| --- | ---: | ---: | ---: |
| `E,F,W,I,UP,B,SIM,RUF` | 631 | 156 | **83** |
| `+ ISC,C4,PL` | 878 | 403 | 330 |

**Line length 100.** The code is written to about 96 columns. Ruff's default 88
flagged **593 lines** — a number that says the default is wrong for this
codebase, not that the codebase is wrong.

**`S` (bandit) is in.** It costs **8 findings across `src`** — the ~900 it
appears to cost are `S101` (assert) in tests, ignored per-file. For a project
whose subject is constraining what agents may execute, subprocess and
injection rules earn their place, and they make the `# noqa: S603` directives
already written in this codebase mean something.

**`PL` and `ISC` are out**, and the reason is in the numbers: of the 247 they
add, `PLR2004` (86, magic values — almost all thresholds read from policy) and
`PLC0415` (86, imports not at top level — *deliberate* here; `providers/bedrock.py`
depends on being one) are noise for this codebase. The complexity rules within
`PL` were run once to produce Q-A2 and are worth revisiting when that finding is
closed, rather than gating on them now.

---

## The batch plan for 4b

Ordered by value per unit of churn, not by finding number.

| batch | closes | why here |
| --- | --- | --- |
| **4b-1** | Q-Q3, Q-A1, Q-Q2 | The three cheap, self-contained ones. Q-Q3 is a real resource leak; Q-A1 is a one-constant move; Q-Q2 is deletion. Nothing here needs a design decision. |
| **4b-2** | Q-C5, Q-C3, Q-C4 | Coverage where it is cheapest and matters most: the fence first, then the providers using `test_bedrock_provider.py` as the template, then the three mid-range modules. Raises the floor. |
| **4b-3** | Q-C1 | `mcp_server.py` from 0%. Its own batch because testing a protocol boundary needs a harness of its own, and it is the most serious finding. |
| **4b-4** | Q-Q1 | The 88 lint findings, mechanically, once the files have stopped moving. Stage the gate to zero **per rule** as each reaches zero — turning the whole set on at once makes every pre-existing finding a gate failure. |
| **4b-5** | Q-A3, Q-C2 | `cli.py`: complexity and coverage together, since both mean touching the same functions. |
| **later** | Q-A2, Q-C6, Q-T2 | Q-A2 is real refactoring and should follow item 2 so the two do not collide. Q-C6 is best done per-module alongside the batches above rather than as a sweep. Q-T2 is a policy choice, cheapest while files are being touched. |

**Where item 2 (the split) fits.** Between 4b-1 and 4b-2. It needs the cheap
architecture finding closed first (Q-A1, so the package graph is a DAG before
anything moves), and it should precede the coverage work so the new tests are
written against the layout that will survive.

## Definition of done for 4a

- Criteria written down before measurement — above, and dated by this commit.
- Findings with ids, each with its evidence and its criterion — 15 findings,
  four of them closed here.
- A batch plan — above.
- The instruments in CI so none of the numbers can rot: coverage floor, mypy at
  zero, ruff against a configured set, and a gate that no longer compares two
  different rule sets.
