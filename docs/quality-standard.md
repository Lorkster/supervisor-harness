# The standard this codebase is held to

Ten criteria and the gates that enforce them. **This is the live document**: it
describes what CI checks today, and it is maintained.

Where the criteria came from, what was measured against them, and the seventeen
findings that closing the gap produced is
[`history/quality-assessment.md`](history/quality-assessment.md) — a closed
record, written in the present tense of 2026-09-02 and left that way.

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

Every one of these runs in CI, on ubuntu × windows. Several guarantees hold on
only one of those, which is why the matrix is not a formality.

| what | how | gate |
| --- | --- | --- |
| Coverage | `pytest --cov=supervisor_harness --cov-fail-under=92` | a **floor**; fails if it drops |
| Types | `mypy --strict` (config in `pyproject.toml`) | **zero**, with no suppression in `src/` |
| Lint | `ruff check` against the configured rule set | **zero** |
| Complexity | `C901` in that rule set | **zero above 15** |
| Architecture | [`tests/test_architecture.py`](../tests/test_architecture.py) — criteria 1 and 3, executed | zero cycles |
| Emission | the same file — no synchronous `emit()` inside an `async def` | zero, package-wide |
| Enforce versus observe | [`tests/test_enforce_versus_observe.py`](../tests/test_enforce_versus_observe.py) — no enforcing handler fails open; every broad `except` is on a declared observing surface | zero undeclared |
| Doc references | `tools/check_doc_refs.py` | zero stale citations |

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
