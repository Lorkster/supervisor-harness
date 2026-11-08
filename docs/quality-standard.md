# The standard this codebase is held to

Ten criteria and the gates that enforce them. **This is the live document**: it
describes what CI checks today, and it is maintained.

Where the criteria came from, what was measured against them, and the seventeen
findings that closing the gap produced is
[`history/quality-assessment.md`](history/quality-assessment.md) — a closed
record, written in the present tense of 2026-09-02 and left that way.

*Last verified on 2026-11-08 against the working tree of branch
`review/code-and-docs-2026-11` (the commit is still `8f34f4e`): the table below
against [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) and
[`pyproject.toml`](../pyproject.toml), line for line. The suite was 1,760 tests
that day (1,402 at `8f34f4e`; the code review added the `test_review2026_*`
files).*

---

## The criteria

Written before anything was measured, deliberately. Criteria invented while
looking at a codebase produce a description of it, not a judgement of it.

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
   The bar is the sabotage check: disable the mechanism, and something must go
   red.
10. The boundaries a user actually touches are tested. An untested entry point
    is untested software however well covered its internals are.

---

## The gates

Every one of these runs in CI, in one of two shapes. The **test matrix** runs the
whole suite on ubuntu × windows, each on Python 3.11, 3.12, 3.13 and 3.14: 3.11
is the floor `pyproject.toml` declares, and 3.14 is what the harness is
developed on. Several guarantees hold on only one of those operating systems,
which is why the matrix is not a formality. The **lint job** runs once, on
ubuntu and Python 3.13. Two more workflows stand beside them: a `bedrock` job
that installs the extra and fails if its tests were skipped, and
[`audit.yml`](../.github/workflows/audit.yml), which runs `pip-audit` over what a
user installs, on every change to `pyproject.toml` and weekly.

| what | how | where | gate |
| --- | --- | --- | --- |
| Tests | `python -m pytest -q`, and `supervisor --help` before it, since nothing in the suite goes through the console script | matrix | all pass |
| Coverage | `pytest --cov=supervisor_harness --cov-fail-under=92` | lint job | a **floor**; fails if it drops |
| Types | `mypy` with `strict = true`, over `src/supervisor_harness` and `tools` (config in `pyproject.toml`) | lint job | **zero**, with no `type: ignore` in either |
| Lint | `ruff check .` against the configured rule set, tests and tools included | lint job | **zero** |
| Complexity | `C901` in that rule set, `max-complexity = 15` | lint job | **zero above 15** |
| Architecture | [`tests/test_architecture.py`](../tests/test_architecture.py) — criteria 1 and 3, executed, and that no `Phase` is declared that nothing outside `models.py` names | matrix | zero cycles |
| Emission | the same file — no synchronous `emit()` inside an `async def` | matrix | zero, package-wide |
| Enforce versus observe | [`tests/test_enforce_versus_observe.py`](../tests/test_enforce_versus_observe.py) — no enforcing handler fails open; every broad `except` is on a declared observing surface | matrix | zero undeclared |
| Doc references | `tools/check_doc_refs.py`, which also checks the README's layout listing | lint job | zero stale citations |
| Dependencies | `pip-audit` over `.[mcp,bedrock]` as a user resolves it | `audit.yml` | no known vulnerability |

`ruff` and `mypy` are pinned to a minor range in `pyproject.toml` (`ruff` 0.16.x,
`mypy` 1.x): both gate at zero, so an unbounded pin would let a new release turn
an unrelated pull request red. Upgrading either is a deliberate act whose new
findings are read and dealt with.

Not every criterion has a gate of its own. Criterion 6 is held by
[`tests/test_resource_lifetime.py`](../tests/test_resource_lifetime.py), in the
matrix; criterion 10 by the tests of the entry points (`test_mcp_server.py`,
`test_cli.py`, `test_cli_commands.py`), also in the matrix; criterion 5 by `ruff`'s
`RUF100` (an unused `# noqa`). Criterion 9, the sabotage check, is a practice and not a
gate: nothing in CI disables a mechanism to see whether something goes red, and
the batches that closed the gap ([history](history/quality-assessment.md)) did it
by hand.

A floor rather than a target, throughout. The question these answer is "has this
change made things worse", which is answerable; "is the number high enough" is
not.

## The lint rule set, and why it is that one

```toml
select = ["E", "F", "W", "I", "UP", "B", "SIM", "RUF", "S", "BLE", "TRY004", "C901"]
line-length = 100
```

Chosen by measuring candidates rather than by preference; the counts are in the
[assessment](history/quality-assessment.md#how-the-ruff-rule-set-was-chosen).
Three decisions worth keeping visible:

- **Line length 100.** The code is written to about 96 columns, and ruff's
  default 88 flagged 593 lines — a number that says the default is wrong for
  this codebase, not that the codebase is wrong.
- **`S` (bandit) is in.** For a project whose subject is constraining what
  agents may execute, subprocess and injection rules earn their place, and they
  make the `# noqa: S603` directives already in this codebase mean something.
- **`PL` and `ISC` are out.** Of the 247 findings they add, `PLR2004` (magic
  values — almost all thresholds read from policy) and `PLC0415` (imports not at
  top level — deliberate here; `providers/bedrock.py` depends on being one) are
  noise for this codebase.

## Adding a gate

A gate is worth adding when the regression it catches is **silent** — when the
wrong behaviour still returns the right answer, or still passes every
behavioural test. That is the common thread through all of them: a package cycle
resolves fine until it does not, a synchronous `emit` in an async function is
merely slow, a full-log rescan returns the correct result and only costs time,
and an enforcing check that fails open reads exactly like one that allowed the
operation.
