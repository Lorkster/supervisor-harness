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
| Coverage | `pytest --cov=supervisor_harness --cov-fail-under=88` | a **floor**; fails if it drops |
| Types | `mypy` (config in `pyproject.toml`) | **zero**, from the start |
| Lint | `tools/ruff_diff.py`, now against a configured rule set | **no new** (file, rule) pairs |
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

**Q-A2 · Complexity is concentrated outside the module scheduled for splitting.**
*(Confirmed by the split: `core/supervisor.py` is now 1,652 lines and still holds
its complexity findings. Splitting it did not touch the complexity, exactly as
this finding predicted — the two are independent problems.)*
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

**Q-C2 · `cli.py` is 42% covered** — 339 missing statements, the largest
absolute gap in the codebase. The other user-facing boundary.
*Criterion 10. Cost: medium.*

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
| ~~**4b-1**~~ | ~~Q-Q3, Q-A1, Q-Q2~~ | **Done.** Q-Q3 needed a design decision after all — *which* object closes a shared store — and Q-Q2 was better answered by enabling the rules than by deleting the directives. |
| ~~**4b-2**~~ | ~~Q-C5, Q-C3, Q-C4~~ | **Done.** Coverage 82.3% → **87.0%**, floor raised to 86. Found and fixed a real defect on the way — see Q-Q5. |
| ~~**4b-3**~~ | ~~Q-C1~~ | **Done.** 0% → 95%, driven through `call_tool` rather than the closures. Coverage 87.0% → **88.8%**, floor raised to 88. |
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
