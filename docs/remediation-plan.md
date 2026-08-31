# Remediation plan — the 37 open findings

A working document, meant to be picked up by a session that was not present when
it was written. It says what is still open, in what order it should be closed,
and what counts as closed for each batch. Update the status column as batches
land; do not rewrite the batch definitions to match what was actually done —
amend them and say why, so the next session can tell a decision from a drift.

## Where the findings themselves live

`.supervisor/runs/run_01M12M8R3MXN1Q/artifacts/findings.md` — 88 findings, each
with a verdict and the evidence that settles it. **That path is untracked**:
`.gitignore` excludes `.supervisor/`, so a fresh clone will not have it. The
`fnd_…` ids below are the durable anchor; the document is the reasoning behind
them. If it is missing, the ids and the mechanisms named here are enough to work
from.

## State as of `262ad79`

The findings document was last re-verified against `d2a7dba`. Five commits have
landed since (`497bf86`, `e0fe0a7`, `9543c05`, `1337e11`, `3067704`) and they
touch only `agents/brief.py`, `agents/registry.py`, `cli.py` and `mcp_server.py`.
Every module the open findings rest on — `store/*`, `models.py`, `config.py`,
`core/{supervisor,tools,dod,phases,drift,blackboard,paths}.py`, `providers/*` —
is unchanged.

**All 44 open findings (37 distinct) still reproduce.** 164 tests pass. There is
no CI.

One item did close: the git-wording inconsistency section 5 of the findings
document recorded as "found while re-verifying, not recorded as a finding" was
fixed by `e0fe0a7`.

## One re-rating

`fnd_01M13MATP0WX63` is filed medium. It is the most serious thing open and is
treated here as the release blocker.

`Scope.paths` and `Scope.forbidden_paths` both default to `[]` (models.py:224)
and are populated from the synthesis model's task scope (phases.py:593). A task
the model gives no scope produces an execution agent with no fence at all:
tools.py reads `if scope.paths and not matches_any(...)`, so empty means
unrestricted; `SKIP_DIRS` is consulted only by `_walk`; and `write_file` creates
intermediate directories. `write_file(".git/hooks/pre-commit", …)` therefore
succeeds — arbitrary code execution at the user's next commit, reached without
the shell, with `policy.allow_command_execution` false throughout. The same hole
lets an execution agent rewrite `.supervisor/runs/*/events.jsonl`, which is the
record the run is judged against.

## Batches

Ordered so that no batch has to re-edit what an earlier one settled. One PR
each, in the style the history already uses.

| # | batch | findings | status |
|---|---|---|---|
| 0 | CI on Linux and Windows | — | **done** — `ci/linux-and-windows-matrix` |
| 1 | Give the execution fence a floor | 1 | **done** — `fix/execution-fence-floor`, uncommitted |
| 2 | Make the log survive a torn write and a bad payload | 5 | **done** — `fix/log-durability-and-fold-containment` |
| 3 | Make the snapshot answerable to the log | 2 (+2 dup) | **done** — `fix/snapshot-watermark-and-atomic-write` |
| 4 | Stop the phase machine issuing the same work twice | 3 | not started |
| 5a | Fix the tool-round loop | 2 | not started |
| 5b | Project turns and notes into RunState | 2 (+1 dup) | not started |
| 6 | Supervise verification, enforce the whole budget | 2 (+2 dup) | not started |
| 7 | Harden the sandbox and the config trust boundary | 4 (+1 dup) | not started |
| 8 | Retention and index convergence | 6 | not started |
| 9 | Collapse the backend split, then the module | 8 (+2 dup) | not started |

Release-blocking, on the reading above: **1, 2, 3, 4**. Code execution past the
fence, two proven silent data losses, permanent unresumability, and duplicated
dispatch. **0** should go first regardless, because 1 and 7 cannot be proven
without it.

---

### 0 · Get CI running on Linux and Windows

No `.github/` exists. The entire review ran on Windows, which is why the two
symlink findings are marked open on code paths rather than on a demonstration —
`WinError 1314` blocks symlink creation for that account — and why the POSIX
branch of `FileLock` is reasoned about rather than executed.

**Done:** matrix job, Python 3.11–3.13, Linux and Windows, existing tests green
on both.

**Landed on `ci/linux-and-windows-matrix`.** `.github/workflows/ci.yml`: 2 OS × 4
Python versions, `fail-fast: false`, plus a `supervisor --help` smoke step
because nothing in the suite goes through the console scripts. The matrix runs
3.11 through **3.14** rather than 3.13 — `requires-python` has no upper bound and
3.14 is what the harness is developed on, so the newest is tested rather than
assumed. No lint gate: `ruff` reports 59 findings on `main` against defaults this
project has never configured, and a gate that is red on arrival tells nobody
anything. 168 tests pass on all eight jobs.

**What it found immediately.** `test_the_wall_clock_bound_abandons_a_silent_agent`
set `agent_timeout_seconds` to 50 ms *before* the run started, so the bound ran
against the test's own setup: reaching the analysis fan-out takes longer than
50 ms on a loaded machine, the agents were abandoned correctly by the mechanism
under test before the test had dispatched them, and `_reach_analysis` failed on a
fan-out of one. Three failures in eight concurrent local runs, none in twelve
unloaded ones — which is how it survived until there was somewhere busy to run
it. The bound is now armed after the run reaches analysis. Nothing about the
harness changed, and the test still fails with the bound disabled, so it has not
gone vacuous.

### 1 · Give the execution fence a floor

`fnd_01M13MATP0WX63`.

A set of paths no agent may write, whatever its scope says, enforced in
`write_file` and in the command path's path-candidate check: version-control
metadata (`.git`, `.hg`, `.svn` — hooks and config are executed by the VCS) and
the harness's own store. Empty `scope.paths` continues to mean "the workspace",
now minus the floor; it does not become "refuse everything", which would break
every task the synthesis model gives no scope.

**Deliberately not in this batch:** tools.py's early return
`if not scope.paths and not scope.forbidden_paths: return None` leaves an
unscoped agent without the executable allow-list, the metacharacter check and
the glob check in the command path. That is documented design, gated behind
`policy.allow_command_execution` (default false, and a PROTECTED_SETTING), and
three existing tests assert it deliberately. Changing it is the policy call
below, not a bug fix.

**Done:** an execution agent with `Scope()` is refused `.git/hooks/pre-commit`
and a write into the store, and is still permitted `src/foo.py`.

**Landed on `fix/execution-fence-floor`.** `VCS_DIRS` / `STORE_DIRS` and
`Toolbox._floor_refusal` in `core/tools.py`, checked in `write_file` before the
scope and in `_floor_command_refusal` before the unscoped early return; every
path segment is checked, so a submodule's `vendor/lib/.git` is covered.
`Toolbox` takes the store root (passed from `Supervisor.__init__`) so a store
relocated inside the workspace by `SUPERVISOR_HOME` is fenced where it actually
is. Four tests in `tests/test_hardening.py`; all four fail against the previous
commit. Reproduced against the pre-fix source first: an unscoped execution agent
wrote `.git/hooks/pre-commit` and `.supervisor/runs/r/events.jsonl` with
`allow_command_execution` false. 168 tests pass.

### 2 · Make the log survive a torn write and a bad payload

`fnd_01M13MPPCA92N8`, `fnd_01M130BWJ4K69H`, `fnd_01M130P3E5HJCN`,
`fnd_01M130P3E5WCN7`, `fnd_01M130P3E5K51P`.

- `append` opens `"a"` and writes without repairing a missing trailing newline
  (eventlog.py:236) — measured to destroy two records, silently. Seek back one
  byte under the lock and append a newline if absent.
- `_last_seq_unlocked` returns `0` for a wholly unparseable log
  (eventlog.py:298), so sequencing restarts at 1 rather than failing closed.
- `read()` has no skipped-line counter; a dropped line is invisible.
- `fold` applies every event with no `try/except` (events.py:234), so one bad
  payload makes a run permanently unresumable.
- `AGENT_STATUS`, `AGENT_DISPATCHED` and `CRITERION_VERIFIED` silently `pass`
  when their target is absent — the same shape as the unknown-type bug `b7c9cf4`
  fixed; use the same remedy.
- The list branches append unconditionally while the dict branches are idempotent
  by assignment. Make it consistent by design rather than per-branch.
- `state.checkpoint_iteration = checkpoint.iteration` should take the maximum.

**Done:** the torn-record reproduction from the findings document reads back
three events, not one; a garbage log raises rather than restarting at seq 1; a
poisoned payload is skipped and counted, and the run still opens.

**Landed on `fix/log-durability-and-fold-containment`.** `_terminate_last_line`
closes an unterminated final record before anything is appended to it (`append`
and `append_many` now write bytes through one `ab+` handle, so the newline
handling is explicit rather than resting on `newline="\n"`). `_last_seq_unlocked`
raises the new `CorruptLog` instead of returning 0, naming the path and line
count, and leaves the file alone; a file of blank lines still starts from zero.
`EventLog.skipped_lines` counts unreadable lines and `RunStore._fold_log` carries
it to `RunState.damaged_lines`. `fold` and `RunSession.emit` share
`_apply_contained`, which records into `RunState.rejected_events`. Absent targets
go to `RunState.orphaned_events`. `_upsert` makes the five list branches
idempotent by id, and `checkpoint_iteration` takes the maximum. All three new
signals are reported by `supervisor status` alongside `unhandled_events`.

Nine tests across `tests/test_eventlog.py` and `tests/test_fold.py`. The six fold
tests fail against the previous commit; the three log tests could not be run
there (the new exception type breaks collection), so both log defects were
reproduced directly instead — the torn append read back `[1]` where `[1, 3]` was
written, and a 300-line garbage log was assigned sequence 1. 177 tests pass, and
6/6 under parallel load.

**Reproductions worth keeping.** A torn record is made with
`handle.write('{"seq": 2, …')` — no trailing newline — between two ordinary
appends. A checkpoint regression needs a *later-seq* event carrying a *lower*
iteration: `fold` sorts by seq, so simply shuffling the list proves nothing, and
a first attempt at that test passed against the unfixed code.

### 3 · Make the snapshot answerable to the log

`fnd_01M130BWJ4VRVH` (+ `fnd_01M13MPQTSJR47`, `fnd_01M130P3E5CV3N`),
`fnd_01M130BWJ4C021`.

`load_state` returns `state.json` whenever it merely parses and reaches the fold
only on a decode error (runstore.py:99) — a stale snapshot wins over a correct
log forever. `save_snapshot` writes through a constant `state.json.tmp`, never
fsyncs and takes no lock (runstore.py:110), which is how a stale one gets made
when two processes report concurrently.

Add `last_seq` to `RunState`, compare it against the log's tail in `load_state`,
fall back to the fold on a mismatch; write the temp file through a unique name
and fsync it. `RunSession.reload` already does the right thing and has no caller
in the store — wire it.

**Done:** a snapshot written from an older fold is rejected in favour of the log;
two concurrent emitters cannot leave a truncated `state.json`.

**Landed on `fix/snapshot-watermark-and-atomic-write`.** `RunState.last_seq` is
maintained by `_apply_contained` and advanced whether or not the event applied;
`EventLog.last_seq()` reads the log's tail without the append lock (append-only
means a race can only make a snapshot look staler, which fails safe);
`load_state` prefers the log whenever the snapshot is behind. `save_snapshot`
writes through a `mkstemp` name, fsyncs before the rename, and returns whether it
landed. Seven tests in `tests/test_fold.py`, all failing against the previous
commit. 183 tests pass, 6/6 under parallel load.

**Two things the finding did not name, both found by writing the concurrency
test.** Windows refuses to replace a file another handle has open, so a
`supervisor status` against a reporting run could make the rename raise — the
rename now waits the reader out, and `load_state` catches `OSError` and falls
back to the log from its side. And a snapshot that cannot be written is no longer
fatal at all: that is the rule `sync_index` already states for the index, and it
became true of the snapshot only once `load_state` could rebuild from the log.
`RunStore.snapshot_error` records why a write did not land.

**Not done, deliberately.** This batch was also supposed to wire
`RunSession.reload`. There is still no caller that needs one — `Supervisor` opens
a fresh session per operation and `emit` keeps state in step within one — so
inventing a call site to close a line item would be worse than leaving it to
batch 9's dead-code decision. It does now go through `_fold_log`.

**Reproductions worth keeping.** The watermark defect shows up as: append two
events, `save_snapshot(fold(log.read_all()[:1]))`, then `load_state` returns the
phase from the *first* event only. The shared-temp-name defect needs two threads
in `save_snapshot` with a payload around 100 KB; on Windows it surfaces as
`WinError 32` naming `state.json.tmp` itself.

### 4 · Stop the phase machine issuing the same work twice

`fnd_01M130P3E56TXV`, `fnd_01M130M6QPC2HG`, `fnd_01M130P3E5HZYQ`.

- `_begin_planning` transitions to `ANALYZING` before returning the planner
  packet (supervisor.py:412). An `advance()` in that window spawns the fallback
  fleet, and the planner's later report calls `_spawn` unconditionally
  (supervisor.py:453) with the transition guard already satisfied, so nothing
  detects the collision. Two full analysis fleets, reachable by an ordinary call
  sequence.
- `report()` checks only that the agent exists (supervisor.py:838) — never its
  status, never whether that turn was already reported — and `seq` comes from
  `turn_counts`, so a replayed report is accepted and counted.
- The `DONE` guard at supervisor.py:510 is dead: `_stage_agent` only ever returns
  agents in `ACTIVE_AGENT_STATUSES`, so a finished synthesizer falls through to a
  fresh spawn on every `advance()`, each with its own packet.

**Done:** `advance()` before the planner reports yields one fleet; a duplicate
`report()` is rejected by name; repeated `advance()` past synthesis spawns
nothing.

### 5a · Fix the tool-round loop

`fnd_01M130M6QP6AYS`, `fnd_01M130M6QPJTJS`.

On the final iteration of `range(MAX_TOOL_ROUNDS + 1)` the nudge is appended to
`history` and the code continues — the loop then ends, the nudge is never sent,
and the tool-call payload falls through to `_record_turn` as if it were the
agent's answer (supervisor.py:1341). Separately, `history` is *replaced* by three
messages each round, discarding every earlier tool result, so an agent cannot
accumulate evidence across rounds.

**Done:** an agent that calls tools on every round is asked once for a final
answer and that answer is what gets recorded; results from round *n* are still
visible at round *n+1*.

### 5b · Project turns and notes into RunState

`fnd_01M13MPQCDSF0A`, `fnd_01M130M6QPGERP` (+ `fnd_01M130P3E5XB3J`), and the
rescan half of `fnd_01M1309W6321FP`.

`NOTE` folds to `pass`; turn bodies are dropped; three call sites re-read the
entire log to compensate (supervisor.py:565, 1754, 1773), one of them once per
supervised turn. Add `turns` and `notes` to `RunState`, delete all three rescans,
and surface notes from `status()`.

Needs batch 3 landed first: this is what makes `state.json` a complete
projection, and it must not be trusted before it carries a watermark.

**Done:** no full `session.events()` rescan remains in `core/supervisor.py`;
`status()` reports the note explaining a failed agent.

### 6 · Supervise verification, and enforce the whole budget

`fnd_01M130M6QPHMY1` (+ `fnd_01M1309W63PF1T`), `fnd_01M130P3E5GKJB`
(+ `fnd_01M130M6QPME80`).

Both `report()` paths short-circuit to `_report_verification` before
`_record_turn`, so a verifier's reasoning, `self_assessment`, `blocked_on` and
usage reach nothing, and `Budget(max_turns=3)` is unreachable. `Budget.exhausted`
accepts tokens and seconds; the only call site passes turns (drift.py:393).
`grep -c usage contracts.py` returns **0**, so no host turn schema asks for usage
and `state.usage` is empty on the default backend — the token ceiling cannot be
enforced there at all until the contract changes.

Depends on 5b.

**Done:** a verification turn appears in the log and in `RunState`; a budget with
a token ceiling stops an agent that exceeds it on both backends.

### 7 · Harden the sandbox and the config trust boundary

`fnd_01M13MATP0KG6X` (+ `fnd_01M13091R272CD`), `fnd_01M13MATP08ZJJ`,
`fnd_01M13MPQTS667A`, `fnd_01M13091R28R46`.

- `_walk` has no `is_symlink()` guard and `search` reads the walked path
  directly, while `read_file` refuses the same path through `_resolve`. The
  inconsistency is what makes it easy to miss.
- `PROTECTED_PROVIDER_KEYS` omits `params`, which router.py:180 merges into
  `extra` and openrouter.py:87 applies *after* `model` and `messages` are set — a
  workspace config substitutes both. `PROTECTED_SETTINGS` leaves `routing`,
  `roles` and the rest of `policy` settable.
- `write_artifact` does not sanitise the name: `"../../escaped.md"` writes
  outside the run directory. Two literal call sites today.
- Nothing redacts secrets before they reach the log, and the store root sits
  inside the analysed workspace with no `.gitignore` written into it.

**Done:** the symlink escape is proven refused **on Linux CI** (needs batch 0); a
workspace config cannot change `providers.*.params`; the artifact traversal
reproduction fails.

### 8 · Retention and index convergence

`fnd_01M130BWJ4JKAY`, `fnd_01M13MPQTSM26K`, `fnd_01M130BWJ404NN`,
`fnd_01M130BWJ4FYZA`, `fnd_01M13MPQTS8N93`, `fnd_01M130BWJ44GM9`.

There is no `delete_run` anywhere; `reindex` iterates surviving runs with no
pruning pass, so a deleted run's prompt and the user's home path stay in
`index.sqlite3` permanently. `lessons.jsonl` is harness-home scoped with no
workspace filter, no age cap and no occurrence cap, and its contents are injected
into later runs' model prompts — a cross-workspace influence path that wants a
deliberate decision, not just a cleanup. `add_lesson` read-modify-rewrites the
whole file with no lock while eventlog.py builds a `FileLock` for exactly this
class of problem. `sync_run(events=None)` wipes projected event rows (measured:
two rows to zero); latent today because both callers pass events, so fix it as a
signature that cannot express the mistake.

**Done:** deleting a run removes it from the index; `reindex` converges on a
second run; a lesson keeps its owning run; concurrent `add_lesson` calls do not
lose each other's writes.

### 9 · Collapse the backend split, then the module

`fnd_01M130M6QPAW1S`, `fnd_01M13MPPWMJRWX`, `fnd_01M13MPPWM98M1`,
`fnd_01M13MPPWMSR5X`, `fnd_01M130M6QP8NP2`, `fnd_01M13MPPWMT7T3`,
`fnd_01M130P3E5SCF8`, `fnd_01M1309W6321FP`, `fnd_01M130M6QPAMQN`
(+ `fnd_01M13MPPWMTK0S`), `fnd_01M1309W63ZWYS`.

Last, because it churns the same lines every earlier batch edits.

`_delegated` inspects only the binding, independent of `state.backend`, across
six sites (supervisor.py:405, 499, 722, 823, 893, 1470); the module docstring
still claims parity is "a property of the backend". A resumed run is judged by
whatever config, bindings and host the *new* process has, with `state.host` never
compared against `self.host`. `Blackboard.supervisor_inbox` has no caller and
`DirectiveKind.ANSWER` is constructed by no code path — decide: wire or delete.
`supervisor.py` is 2016 lines of an 11676-line package.

Mutate-then-emit and *emit replaces the object the caller holds* are the same
inversion seen twice, and belong here rather than in their own batch: the
docstring at supervisor.py:20-21 asserts the invariant the code breaks.

---

## Policy calls still outstanding

Neither is a bug. Both change the shape of a batch, so decide before starting it.

1. **Should an execution agent with no model-supplied scope be permitted to run
   commands at all, rather than merely fenced?** (batch 1, deferred). Making the
   command fence universal would mean an unscoped agent reaches only the check
   runners. Three tests in `tests/test_shared_tree.py` and
   `tests/test_hardening.py` assert the current behaviour deliberately, and the
   module docstring documents it; changing it means changing those first.
2. **Should `lessons.jsonl` stay cross-workspace?** (batch 8). It is currently
   harness-home scoped and injected into later runs' prompts regardless of which
   workspace produced it.

## Conventions

- One batch, one branch, one PR, in the existing style: `fix/<short-theme>`.
- Every fix carries a test that fails against the previous commit. Several
  findings were settled by running code rather than by reading it; keep that bar.
- No `FIXED` claim without a file and line that carries the fix. That is the rule
  the first revision of the findings document broke.
