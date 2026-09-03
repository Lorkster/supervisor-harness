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
| Coverage | `pytest --cov=supervisor_harness --cov-fail-under=92` | a **floor**; fails if it drops |
| Types | `mypy --strict` (config in `pyproject.toml`) | **zero**, strict since Q-T2 |
| Lint | `ruff check` against the configured rule set | **zero**, since 4b-4 |
| Complexity | `C901`, in that rule set | **zero above 15**, since Q-A2 |
| Architecture | `tests/test_architecture.py` — criteria 1 and 3, executed | zero cycles |
| Emission | `tests/test_architecture.py` — no sync `emit()` inside an `async def` | zero, package-wide |
| Doc references | `tools/check_doc_refs.py` | zero |

Two notes on what changed in the instruments themselves:

- **Ruff is configured at last** (`[tool.ruff]` in `pyproject.toml`). It had
  never been, which is why the gate was written to tolerate a baseline.
- **`tools/ruff_diff.py` had a real flaw**, found by being the first change to
  touch ruff configuration: it ran `ruff check` separately in each tree, so each
  used *its own* config. Any pull request changing the rule set therefore
  compared two different rule sets — reporting every newly-enabled rule as a
  regression, and, worse, reporting nothing at all for a change that *disabled*
  a rule. Both trees were then measured with the working tree's configuration.
  Recorded as **Q-Q4**, closed in the 4a batch.

  *The tool was removed in 4b-4.* It existed to tolerate a baseline, and once
  Q-Q1 drove that baseline to zero a plain `ruff check` was strictly stronger.
  Keeping a second, weaker gate that can never fire first is the kind of dead
  code this assessment exists to find.

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

**Q-A1 · A package cycle between `core` and `agents`. — CLOSED (4b-1)**
`core` imported `agents.registry`, `agents.roles` and `agents.brief`; `agents`
imported back into `core` in exactly one place, for the constant
`BASELINE_FACT`. One constant made the cycle.

`BASELINE_FACT` now lives in `models`, beside the `RunState.facts` dict it keys.
That is its right home independently of the cycle: `core.baseline` writes the
value and `agents.brief` reads it, so a constant owned by either package makes
the other import across a boundary.

A re-export from `core.baseline` was tried first and dropped — it left an unused
import for the linter and kept alive the exact import path that caused the
cycle. `tests/test_architecture.py` now executes criteria 1 and 3 directly, so
**item 2 cannot reintroduce a cycle while moving imports around**, which is the
thing worth having before a split.

**Q-A2 · Complexity concentrated outside the module that was split. — CLOSED**
Every function this finding named, and two more:

| function | was | now |
| --- | ---: | --- |
| `store/events.py` `_apply` — the fold | **46** | a dispatch table of 26 handlers |
| `core/journal.py` `build_journal` | 26 | a `_JournalBuilder` with a method each |
| `core/phases.py` `final_report_markdown` | 25 | one function per section |
| `core/journal.py` `_render_episode` | 21 | one function per part |
| `mcp_server.py` `build_server` | 17 | three grouped registrars |
| `core/blackboard.py` `answer_from_record` | 17 | one function per source |

**`C901` is now enabled, bounded at 15, at zero.** The number is chosen rather
than inherited: ruff's default of 10 would leave nine functions between 11 and 13
outstanding, and gating on a bound the codebase does not meet is the tolerated
baseline criterion 4 rejects. At 15 nothing above can return. Lower it when
those nine are dealt with; do not raise it.

**One attempt was abandoned after measuring it.** `build_journal` was first
rewritten with nested handler closures, which turned the 9-way `elif` chain into
a table and made the metric **worse** — 26 → 27 — because C901 counts a nested
`def` toward its enclosing function. The builder object is the version that
actually reduces it, and the same effect explains why `build_server` measured 17
while every tool in it is trivial.

*Still open, and recorded rather than closed:* nine functions between 11 and 13
— `validate_criteria` (13), `_drive_agent` (13), `detect_contradictions` (12),
`_inline_source_flag` (12), and five at 11.

**Q-A3 · `cli.py`'s complexity. — CLOSED (4b-5)** All four findings are gone;
`ruff check --select C901,PLR0912,PLR0915` is clean on the file.

`cmd_status` (19 branches) and `_print_response` (12) were each a sequence of
independent "print this section if the run has one" blocks, so each section
became its own function. `build_parser`'s 85 flat statements are now grouped by
what a reader is looking for — the commands that drive a run, the ones that read
one back, the ones that maintain the store.

The file grew from 939 to about 1,030 lines doing it. That is the expected trade
and this document's criterion 2 anticipates it: *a module's size is a smell, a
function's complexity is the defect.*

### Testing and coverage

Baseline: **82.25%**, 1,032 of 5,813 statements missing, 335 tests, 2.9
assertions per test. *(After 4b-1: 347 tests.)*

**Q-C1 · `mcp_server.py` was 0% covered. — CLOSED (4b-3)** Now **95%**.

`tests/test_mcp_server.py` calls through `server.call_tool(name, arguments)` --
the entry the host uses -- rather than reaching for the closures inside
`build_server()`. That is the difference between testing this module and testing
the functions it happens to contain: registration, schema generation and
argument coercion are most of what it *does*, and calling the closures directly
skips all three.

A whole run is then driven end to end through the tool surface, following the
loop `INSTRUCTIONS` documents to hosts: start, report each packet, advance,
approve, complete.

What that reached which nothing else did: every tool being registered at all and
carrying a schema the SDK will render; `next_step` guidance on each result;
`_as_dict` accepting a JSON string or JSON embedded in prose, because hosts send
those whatever the schema says; an unusable `mode` degrading to `auto` rather
than raising; the `${workspaceFolder}` template a host passes through
unexpanded; the read-only tools defaulting to the latest run; and `main()`,
which the `supervisor-mcp` console script points at and which CI's entry-point
step does not exercise -- it runs `supervisor --help`, not this one.

Five statements remain uncovered: `if __name__ == "__main__"`, and four lines
inside `supervisor_check_drift`'s model path, which needs a drift stage routed
off `host`.

**Q-C2 · `cli.py` was 42% covered. — CLOSED (4b-5)** Now **80%**, and the
project total went 88.9% → **92.9%** with the floor raised to 92.

`tests/test_cli_commands.py` drives a whole run **through the CLI itself** —
`start`, `report`, `advance`, `approve` — rather than through the Supervisor API
with the CLI pointed at the leftovers. `cmd_report`'s stdin handling,
`cmd_approve`'s decision parsing and `_print_response`'s rendering only run on
that path.

They were written **before** the Q-A3 refactor in the same batch, deliberately:
42% is not enough protection to reshape a 19-branch function behind.

**Q-C3 · The HTTP providers were barely tested. — CLOSED (4b-2)**
`openrouter` 26% → **92%**, `ollama` 27% → **92%**, `anthropic` 30% → **89%**.
`tests/test_http_providers.py` uses `httpx.MockTransport`, so no test opens a
socket, and asserts the two things a provider is for: what reaches the wire (the
schema instruction, the sampling knobs, `response_format`, Ollama's `think:
false`) and what comes back (text, reasoning, model, and **usage**, which is what
every budget and ceiling in the harness is measured in).

One environment trap, the same one the Bedrock batch hit with `AWS_REGION`:
`api_key=""` falls back to `os.environ`, so parametrising over *constructed*
providers built them at collection time, before any fixture could clear
anything. The test then passed or failed according to whether the developer had
a key exported — and it found exactly that on the machine it was written on. The
providers are now built inside the test.

**Q-C4 · Three modules between 57% and 71%. — CLOSED (4b-2)**
`agents/registry.py` 57% → **97%**, `providers/router.py` 67% → **95%**,
`host/detect.py` 71% → **92%**, in `tests/test_discovery_and_routing.py`.

Two of these read the world outside the test and both are now neutralised rather
than tolerated: `detect_host` scores the ambient environment, and this suite is
routinely run *inside* one of the hosts it detects; `discover_host_agent_files`
reads `~/.claude/agents`, so a developer's own definitions would otherwise
appear in the results. Either would pass on the machine it was written on and
fail in CI.

**Q-C5 · `core/tools.py` was 79%. — CLOSED (4b-2)** Now **94%**.

The shape of the gap was the finding. The fence's *decisions* —
`_scope_refusal`, the floor, the executable allow-list — were thoroughly covered
by `test_hardening.py`. What was uncovered was **the tools that consult it**:
`_walk`, `list_files`, `read_file`, `search`, `write_file`, `run_command` and the
`call` dispatcher accounted for 60 of the 65 unreached lines. The module knew
what to forbid, and nothing checked what it did when it allowed.

That distinction matters beyond arithmetic. A truncation that silently drops
results, a window returning the wrong lines, or a dispatcher crossing two wires
would pass every refusal test in the suite — the fence is not consulted
differently, it simply guards an operation that answers wrongly.

**Q-C6 · The suite has never been audited for vacuous tests as a whole. —
`test_hardening.py` DONE (Q-C6a); three modules remain.**
The sabotage check has been applied to each new batch since it was adopted, and
has caught five vacuous tests. Run backwards over `test_hardening.py` — the
oldest module, and the one holding the regressions for the defects the harness
found in itself — it found one more, and one mechanism no test reached at all.

**43 sabotages against all 37 tests, on Windows and on Linux.** Each disables
one named mechanism and runs one test alone. Both platforms, because they
disagree about which of these tests can run: the two symlink tests skip on
Windows for want of `SeCreateSymbolicLinkPrivilege`, and a path that escapes on
one platform need not exist on the other. A one-platform audit would have missed
both findings below — and would have reported the symlink tests as *surviving*
their sabotage, which is what a skipped test looks like from the outside.

**One vacuous test, and it was vacuous on both platforms.**
`test_reads_cannot_escape_the_workspace` asserted that reading
`../../../etc/passwd` was refused. Under pytest's `tmp_path` those three levels
land on `/tmp/etc/passwd`, and on the parent of the Windows temp directory —
neither of which exists. With the containment check deleted from `Toolbox._resolve`
the read was still refused, for not existing, and the assertion passed over it.
The fence could have been removed entirely without that test noticing. It now
escapes to a file the test creates, and asserts *why* the read was refused
rather than only that it was; red under sabotage on both platforms.

**One check that no test reached.** `Toolbox._walk` resolves every file it
yields and drops anything landing outside the workspace. The symlinked-directory
test states in its own docstring that it passes with that check reverted —
`rglob` does not descend through a directory link, so the file is never walked
— and nothing else reached the check either. On Windows, where both symlink
tests skip, the walk's containment was untested outright. A new test hands the
walk that shape directly and goes red on both platforms when the check goes.

**One false alarm, which is the standing cost of this method.**
`test_search_does_not_read_through_a_symlink` survived the removal of its
symlink skip — not because it is hollow but because the resolve check catches
the same file. With both locks off it goes red. Two locks on one door, which is
what the walk's docstring says they are; only the second is load-bearing for
that shape.

**And two tests that are platform-conditional by design**, both correct and
both worth naming because they look vacuous from one side: the `.cmd` shim
resolution test has force only on Windows, and the symlink tests only where a
symlink can be created. The CI matrix runs both, which is what makes them
guarantees rather than decoration.

*Criterion 9. Remaining: `test_supervision.py`, `test_store.py`, and the parts
of `test_fold.py` that predate the bar. Per-module, as before.*

### Code quality

**Q-Q1 · Ruff findings against the configured rule set. — CLOSED (4b-4)**
88 at the assessment, 67 after 4b-1 enabled `BLE`/`TRY004`, and **zero** now.
CI runs `ruff check` and fails on anything.

Most of it was mechanical: 43 lines over the limit, and auto-fixable `UP037`,
`RUF022`, `SIM102`, `SIM105`. Three findings were judgement rather than typing:

- **`SIM905` was wrong here and is suppressed with the reason.** Applied, its
  fix collapsed a readable multi-line block of 87 stopwords into a single
  900-character line. The text is now named before it is split, which satisfies
  the rule and keeps the block. *A linter finding is a question, not an order.*
- **`SIM105` and `S112`/`S110` overlapped**, and `contextlib.suppress` answered
  all three at once while saying what the `try`/`except`/`pass` meant. The
  reason each swallow is deliberate moved from a `# noqa` into a comment.
- **Six were false positives**, suppressed with the reason rather than worked
  around: `S105` on a *shell* token and on `CriterionStatus.PASS`; `S607` on
  `git` resolved through PATH deliberately; `S608` on a constant table name with
  a bound parameter; `B027` on an optional `aclose` hook that is empty on
  purpose.

**The gate changed with it.** `tools/ruff_diff.py` was removed: it existed to
tolerate a baseline, and once there is none a plain `ruff check` is strictly
stronger and cannot be shadowed by a weaker check running first.

**`ruff` and `mypy` are now pinned to a minor range.** A check that gates at
zero is broken by a release that adds a rule, which would turn an unrelated pull
request red; upgrading either should be a deliberate act whose findings someone
reads.

**Q-Q2 · 17 `# noqa` directives for rules that were never enabled — CLOSED
(4b-1).** `E402`, `S602`, `S603`, `S608`, `C901`, `BLE001`: they read as
diligence and suppressed nothing.

Resolved by **turning the rules on rather than deleting the directives**, which
was the better half of the choice this document offered. `BLE` and `TRY004` cost
**zero** new findings — every broad `except` already carried its directive — so
enabling them converted 14 inert comments into real suppressions, and a broad
`except` added from here on is flagged and has to be suppressed deliberately.

The remaining three were genuinely stale and were deleted: `S608` no longer
fires on the interpolation it guarded, and two `BLE001` directives sat on
handlers that *use* their exception, which the rule does not flag. `C901` stays
off — its 16 findings are Q-A2.

Net: RUF100 17 → 0, and the total moved 88 → 74 → **71**, the middle number being
main re-measured under the widened rule set.

**Q-Q3 · SQLite connections were never closed. — CLOSED (4b-1)**
`RunIndex.close()` existed and nothing called it; `RunStore` had no close at
all. A suite run under `-W always` reported **90 unclosed connections** — now
**0**.

The rule the fix follows is **own what you made**. `RunStore` gained `close()`
and context-manager support; the six CLI commands that build a store use `with`;
and `Supervisor` closes its store **only when it constructed one itself**. That
last condition is the whole of the design: closing unconditionally would be a
use-after-close bug for every caller that shares a store, which is most of this
test suite and any embedder running two runs against one store.

`close()` is idempotent and non-terminal — the store reopens on the next
`index()` call. A store is a handle on a directory, not on a connection, and a
terminal close would push a lifetime onto callers who have no interest in one.

Test-side, an autouse fixture in `conftest.py` closes every index a test opened.
Teardown is a fixture's job rather than something three dozen inline call sites
should each remember.

**Q-Q4 · The lint gate compared different rule sets across a configuration
change.** Described above. **Closed in the 4a batch.**

**Q-Q5 · `policy.command_timeout_seconds` did not bound anything. — CLOSED
(4b-2)**

Found by writing the test for it, which is the only way it could have been
found: the old test would have asserted `"timed out" in output`, and that was
already true.

`Toolbox.run_command` ran commands with `shell=True`. The timeout kills the
*shell*; on Windows `cmd /c` is a different process from the program it
launched, which keeps running and holds the pipes open, so `subprocess.run`
blocks until the program finishes and only then reports the timeout. Measured: a
20-second sleep under a **1-second** timeout returned `TimeoutExpired` after
**20.1 seconds**, and after 1.0 without the shell. An agent could hold a run open
for as long as it liked while the harness reported that it had stopped it.

The fix is the one `dod.verify_command` already used: tokenise with
`shell_split`, resolve the executable on PATH, and run with `shell=False`. The
shell was buying nothing — every unquoted metacharacter and glob is refused
before this point, for every agent, so the command is a program and its
arguments. `Toolbox.run_command` was simply the odd one out of the two places
this codebase runs a fenced command.

The regression test asserts **elapsed time**, not the message, because the
message was already right. It runs in 1.05s against a 15s bound, stable across
8 concurrent suites.

### Types

**Q-T1 · Three type errors.** `serde.py:80` (a genuine narrowing bug —
`is_dataclass` admits instances as well as classes), `store/events.py:248`
(variable reuse, not a defect), `mcp_server.py:32` (SDK stub gap).
**Closed in this batch**, so mypy could gate at zero.

**Q-T2 · `mypy --strict`. — CLOSED** `strict = true` is on and at zero.

It cost **25 errors**, not the 14 measured at the assessment: the refactoring
batches added modules, and the count was never re-measured until this one. Every
one was fixed. **No `type: ignore` holds it up** -- there are now *zero* in
`src/`, one fewer than before this batch, which is what makes the gate worth
having rather than a formality.

What the 25 turned out to be:

- **12 "untyped decorator" on the MCP tools**, which looked like an untyped
  dependency and was not. The SDK ships `py.typed` and types `tool` properly;
  *our* `try`/`except ImportError` alias made `_Server` an `Any`, so every
  `@server.tool(...)` was an untyped decorator and all twelve tools under it
  untyped functions. A `TYPE_CHECKING` branch naming the supported SDK, with
  the runtime fallback untouched, fixed all twelve.
- **7 "returning Any"** where a function promised a concrete type. `_enum` is
  now generic, so the three parsers declaring `-> AgentStatus` and friends are
  actually held to it; `serde.to_dict` names the one cast the four
  dataclass-to-dict callers were each making implicitly.
- **3 bare generics**, 2 re-exports (`__all__` on `core/supervisor.py`, which
  also documents that module's surface), and a tuple of lambdas that inferred
  as untyped.

*Criterion 7.*

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
| ~~**4b-1**~~ | ~~Q-Q3, Q-A1, Q-Q2~~ | **Done.** Q-Q3 needed a design decision after all — *which* object closes a shared store — and Q-Q2 was better answered by enabling the rules than by deleting the directives. |
| ~~**4b-2**~~ | ~~Q-C5, Q-C3, Q-C4~~ | **Done.** Coverage 82.3% → **87.0%**, floor raised to 86. Found and fixed a real defect on the way — see Q-Q5. |
| ~~**4b-3**~~ | ~~Q-C1~~ | **Done.** 0% → 95%, driven through `call_tool` rather than the closures. Coverage 87.0% → **88.8%**, floor raised to 88. |
| ~~**4b-4**~~ | ~~Q-Q1~~ | **Done.** All of them to zero in one batch, so the per-rule staging the plan allowed for was not needed. `ruff check` now gates at zero and `ruff_diff.py` is gone. |
| ~~**4b-5**~~ | ~~Q-A3, Q-C2~~ | **Done.** Coverage 42% → 80%, all four complexity findings cleared, and the rendering proved unchanged by diffing 33 CLI invocations before and after. |
| ~~Q-A2~~ | ~~done~~ | Six functions restructured, `C901` gated at 15. Nine functions between 11 and 13 remain, recorded above. |
| ~~Q-T2~~ | ~~done~~ | `mypy --strict` at zero, with no suppression anywhere in `src/`. |
| **Q-C6** | in progress | `test_hardening.py` audited (Q-C6a). Per-module, as the finding says; three modules remain. |

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
