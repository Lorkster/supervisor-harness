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
| 1 | Give the execution fence a floor | 1 | **done** — `fix/execution-fence-floor` |
| 2 | Make the log survive a torn write and a bad payload | 5 | **done** — `fix/log-durability-and-fold-containment` |
| 3 | Make the snapshot answerable to the log | 2 (+2 dup) | **done** — `fix/snapshot-watermark-and-atomic-write` |
| 4 | Stop the phase machine issuing the same work twice | 3 | **done** — `fix/phase-machine-double-dispatch` |
| 5a | Fix the tool-round loop | 2 | **done** — `fix/tool-round-loop` |
| 5b | Project turns and notes into RunState | 2 (+1 dup) | **done** — `fix/project-turns-and-notes` |
| 6 | Supervise verification, enforce the whole budget | 2 (+2 dup) | **done** — `fix/verification-turns-and-budget` |
| 7 | Harden the sandbox and the config trust boundary | 4 (+1 dup) | **done** — `fix/sandbox-and-config-trust` |
| 8 | Retention and index convergence | 6 | **done** — `fix/retention-and-index-convergence` |
| 9a | Event-sourcing and resume fidelity | 3 (+1 dup) | **done** — `fix/event-sourcing-and-resume-fidelity` |
| 9b | Collapse the backend split, and the dead paths | 5 | **done** — same branch |
| 9c | Split the module | 1 | **not scheduled** — see below |

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

**Landed on `fix/phase-machine-double-dispatch`.** The phase stays CREATED until
the plan lands, so an `advance` during planning re-offers planning;
`_apply_plan` keeps a fleet that is already running rather than adding to it;
`_stale_report_reason` refuses a report from an agent outside
ACTIVE_AGENT_STATUSES or past its turn budget, as a response rather than a
failure; and the synthesis DONE check moved *before* `_stage_agent`. Three tests
in `tests/test_host_delegation.py`, all failing against the previous commit. 186
tests pass, 6/6 under parallel load.

**Two things worth knowing before touching this code again.** The DONE guard
stays at the synthesis call site rather than moving into `_stage_agent`, which
would be tidier and wrong: the checkpointer takes a new agent per remediation
iteration under the same role, so refusing every finished stage agent centrally
would break the remediation loop. And `_stage_agent` accepts `**extra` and never
reads it — the `iteration=iteration` the checkpointer passes does nothing, and
role alone is that agent's whole identity. Not a finding, not fixed here.

**Reproductions worth keeping.** The double fleet: `start`, then `advance`
*before* reporting the planner, then report and advance. Against the unfixed
code the first `advance` hands back `analysis` packets and the run ends with four
analysis agents against `max_analysis_lenses = 3`. The synthesis respawn needs a
run sitting in SYNTHESIZING whose synthesizer is already DONE — which
`_report_stage` produces whenever `_apply_synthesis` raises after the status is
set, since it is not wrapped and the transition is the last thing it does. The
test reconstructs that by writing the phase back, because a normal report always
transitions away.

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

**Landed on `fix/tool-round-loop`.** The last pass is the answering round: the
nudge goes into the history before the call rather than after the loop has ended,
and tools are not executed there. Results accumulate for the length of a turn and
are replaced by the directive at the end of it, capped by `TOOL_ECHO_CHARS` and
`TOOL_RESULT_CHARS`. Two tests in `tests/test_supervision.py`, both failing
against the previous commit. 211 pass, 6/6 under parallel load.

**A trap in testing it.** The obvious script — six tool-calling answers, then a
real one — *passes against the unfixed code*, because the seventh call returns an
answer whether or not the agent was ever asked for one. The test answers
conditionally on seeing the nudge instead. Third time in this project a first
draft of a test proved nothing; checking every new test against the previous
commit is what catches it.

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

**Landed on `fix/project-turns-and-notes`.** `RunState` gains `turns`, `notes`
and `task_notes`; all three rescans are gone. Turns are kept whole — including
the findings and messages their own events also project — because a
half-populated `AgentTurn` is a trap and the log holds the whole thing anyway.
`status` reports the last `STATUS_NOTE_LIMIT` notes plus the total. Six tests
across `test_fold.py`, `test_hardening.py` and `test_abandonment.py`, all failing
against the previous commit. 192 tests pass, 6/6 under parallel load.

**Measured, since the point was cost.** One `_previous_turns` call, and the run
it implies at one call per supervised turn:

| turns | log rescan | state read | per run |
|---|---|---|---|
| 50 | 10.4 ms | 0.002 ms | 0.5 s → 0.0001 s |
| 200 | 38.1 ms | 0.009 ms | 7.6 s → 0.0018 s |
| 600 | 112.8 ms | 0.034 ms | 67.7 s → 0.0205 s |

`state.json` for a complete run grows 124,060 → 139,064 bytes (12%).
`core/supervisor.py` is nine lines *longer*, not shorter: three loops came out
and the docstrings explaining what replaced them went in. Batch 9 does not
inherit a smaller module from this.

**Two things done that were not asked for.** `turn_counts` and `usage` are
running totals in the TURN_RECORDED branch, so a replay doubled them even after
batch 2 made the lists idempotent; `_upsert` now reports whether the item was
new. That matters because batch 4 made `turn_counts` a bound on accepting
reports, so an inflated count locks a working agent out. And the absent rescan is
asserted structurally, because a reintroduced one returns the right answer and
only costs time — no behavioural test would catch it coming back.

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

**Landed on `fix/verification-turns-and-budget`.** `_assess_drift` is the half of
`_supervise` both report paths share; verification turns are recorded and
assessed before the verdict is applied. Measured on a complete run: three
verification agents, 0/3 with a turn recorded and 0/3 assessed → 3/3 on both.
`decide_directive` takes the agent's accumulated usage and checks all four
ceilings; `max_tool_calls` had no parameter and now has one. Every schema an
agent or stage reports through carries an optional `usage` object — without it
`state.usage` stayed empty on the host backend and three ceilings were
unenforceable in principle. Six tests, all failing against the previous commit.
198 pass, 6/6 under parallel load.

**A budget corrected rather than a behaviour loosened.** `Budget(max_turns=3)`
for verification was unreachable — `_report_verification` settles the task and
ends the agent on its first report. It is 1 now: verification is a single
judgement by design, and a verifier given more turns would be negotiating with
itself over a verdict it has already reached.

**Why the verifier gets an assessment but not a directive.** It is settled by its
own verdict, so a directive would compute an instruction nothing acts on — the
pattern batch 4 and `fnd_01M130M6QP8NP2` are both about. The assessment is still
worth having: a verifier judging outside its task is exactly the drift to catch.

**One trap, for whoever writes the next structural test.**
`test_the_turn_context_is_built_from_the_workspace_recorded_on_the_run` pinned
`TurnContext` to `_supervise` by name and broke on the move. Widening it to the
whole class was the *wrong* fix: `workspace=str(self.workspace)` is correct at
run creation, where the process's workspace is the one being recorded, so the
class-wide assertion caught a legitimate use. It now locates the method by what
it builds.

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

**Landed on `fix/sandbox-and-config-trust`.** `_walk` skips symlinked files and
resolves the rest to check containment; `params` joins
`PROTECTED_PROVIDER_KEYS`; the quality bars, checkpoint pass mark and drift
thresholds join `PROTECTED_SETTINGS`; `_artifact_name` reduces a name to one
component; the store writes its own `.gitignore` and narrows POSIX permissions;
`store/redaction.py` filters credential shapes at both emit boundaries. 201 pass
locally with 2 skipped, 203 on CI where symlinks can be made.

**The symlink findings are now demonstrated, not argued.** They had been open
against code paths since the original review because the host could not create a
symlink. A throwaway branch reverted `_walk`'s containment, kept the tests, and
ran on CI: `search` returned `link.txt:1: SUPERSECRET-canary-value` on **both**
Linux and Windows runners — GitHub's Windows images have the symlink privilege
the development machine lacks, so both platforms prove it.

**One test is a guard, not a demonstration**, and the difference was only visible
because of that experiment: `test_search_does_not_read_through_a_symlinked_
directory` passes even with the containment reverted, because `rglob` on these
Python versions does not descend through a directory link, so the file is never
walked. The `resolve()` check is there because `**` and symlinks changed
behaviour in 3.13 and the walk should be correct either way.

**The line the config boundary now draws:** a workspace may tune how much work
the harness does — budgets, parallelism, turn counts, routing — and may not tune
how sceptical it is about work done on it. A repository shipping a config could
previously set `require_security_review: false` or `checkpoint_threshold: 0.0`.

**`routing` and `roles` stay settable**, though the finding names them. Both have
a legitimate per-project use that protecting them would cost, and neither reaches
credentials or the scrutiny bars. A judgement, recorded rather than implied.

**Redaction is narrow on purpose.** Fixed credential prefixes and `Authorization`
headers only — no entropy heuristics. This harness is routinely pointed at code
*about* credential handling, where an aggressive redactor would rewrite the lines
a finding is about. It is a backstop, not containment, and should not be widened
into one.

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

**Landed on `fix/retention-and-index-convergence`.** `RunStore.delete_run` and
`purge(older_than_days, keep_last)`; `RunIndex.delete_run` and `prune`, called by
a full reindex; the lessons row stamped with the run that learned it;
`sync_run(state, events)` with `events` required; `add_lesson` under the event
log's `FileLock`, with capped `occurrences`; `prune_lessons` and an age cap in
`lessons_for`; `Lesson.workspace` recorded and used to rank. `supervisor delete`
and `supervisor prune-lessons` expose it — a retention feature nobody can invoke
is a retention feature nobody has. Eight tests in `tests/test_store.py`. 209
pass, 6/6 under parallel load.

**The policy question is answered: keep the library global, bound it, attribute
it.** A lesson learned in one project is worth having in the next — that is what
the library is for — so it stays cross-workspace, and the openness is what got
fixed: an age cap, an occurrence cap, and the originating workspace on every
lesson so the cross-project influence path is auditable. `lessons_for` ranks
local experience above borrowed at equal strength rather than choosing between
them.

**One test is probabilistic, and it is worth saying so.** The concurrent-writer
test caught the unlocked read-modify-rewrite in 1 of 10 runs at four writers.
Raised to six writers it catches it in 7 of 10, and still runs in under a second.
It is a good guard, not a proof: a green run of it does not establish the lock is
there.

### 9a · Event-sourcing and resume fidelity — done

`fnd_01M130M6QPAMQN` (+ dup `fnd_01M13MPPWMTK0S`), `fnd_01M1309W63ZWYS`,
`fnd_01M13MPPWMSR5X`; narrows `fnd_01M130P3E5SCF8`.

`_merge_into` makes the fold update tasks and agents in place, so the object a
caller holds *is* the object in `RunState` and the mutate-then-emit pattern the
whole module uses stops detaching the two. `_spawn` returns the state's own
specs. `_check_resume_fidelity` records, once per run, when the resuming process
is not the one the run started under. `_record_turn` batches a turn into one
`emit_many`: measured 9 lock acquisitions for a turn carrying eight findings,
now 1. `RunSession.reload` (no caller) and `_stage_agent`'s unread `**extra` are
removed. 216 tests pass, 6/6 under parallel load.

**Half of `fnd_01M130P3E5SCF8` stays open, deliberately.** The lock is acquired
with a spin-sleep from inside the async loop, so parallel autonomous agents
serialise on it. Moving the emit to a thread does *not* fix that: the
`RunSession` is shared across the agents `asyncio.gather` runs together, so
offloading trades a latency problem for a data race on `RunState`. Closing it
means giving the session its own lock — a design change, not a fix.

### 9b · Collapse the backend split, and the dead paths — done

`fnd_01M130M6QPAW1S`, `fnd_01M13MPPWMJRWX`, `fnd_01M13MPPWM98M1`,
`fnd_01M130M6QP8NP2`, `fnd_01M13MPPWMT7T3`. Decisions rather than defects, and
the decisions taken were:

**`_delegated` keying on routing is correct; the docstring was wrong.** `run`
refuses an autonomous run with any stage routed to the host, so routing and
backend cannot disagree that way; in the other direction they legitimately do — a
host-backend run routing `drift` to a model provider is supported, and there the
right question is the one the code asks. Drift escalation is a property of the
*stage's routing*; tool use, wall-clock budgets and failure capture are
properties of the *backend*. The docstring called both the latter. Corrected;
code untouched.

**The six inline backend branches stay.** No defect, and no third backend to
survive. If the module is ever split (9c) this is part of that — a symptom of the
file's size, not a problem of its own.

**An agent can now ask the supervisor.** `Blackboard.route` always accepted a
message addressed to the supervisor and stored it, and nothing read it;
`status_after` already mapped ANSWER to RUNNING, so the design anticipated this
and only the connection was missing. `answer_from_record` answers from the run's
own record — brief, scope, definition of done, established facts, peers'
findings — and says plainly when the record does not cover a question rather than
inventing one. No model call: the supervisor is authoritative about the run and
ignorant about the world, and this keeps it judging rather than analysing. An
answer never displaces a correction; it changes the directive's kind only when
the assessment said CONTINUE.

`Blackboard`'s unused instance state is removed. 219 tests pass, 6/6 under
parallel load.

**Worth knowing:** the first version of the wiring shipped a `NameError` — a
`SUPERVISOR` constant used and never imported — and the full suite passed anyway,
because nothing exercised the new path yet. Ruff's `F821` caught it. New code
whose tests come after it is code with no tests.

### 9c · Split the module — not scheduled

The module-size half of `fnd_01M1309W6321FP`. `core/supervisor.py` is 2115 lines
and there is no natural stopping point, so this is left as its own decision
rather than carried as pending work. Every other batch has already taken what it
needed from that file; splitting it is a refactor with no defect behind it.

### 9 · Collapse the backend split, then the module (original scope)

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

## Accounting

Reconciled against the findings document on 2026-09-01 by diffing the ids in its
`Open (44)` table against every id named in a commit's `Closes:` line, because
the running totals in earlier summaries had drifted from the ids.

- **11 distinct findings remained** at that point, not 10. Batch 9 holds **9
  distinct + 1 duplicate** (`fnd_01M130M6QPAMQN` / `fnd_01M13MPPWMTK0S`), where
  this table previously said 8 + 2.
- `fnd_01M1309W6321FP` appears in batch 5b's `Closes:` line, but only its rescan
  half was closed. The module-size half is batch 9's, and the id being named as
  closed is why the count slipped. A `Closes:` line for a half-closed finding is
  the mistake; say which half.

## Conventions

- One batch, one branch, one PR, in the existing style: `fix/<short-theme>`.
- Every fix carries a test that fails against the previous commit. Several
  findings were settled by running code rather than by reading it; keep that bar.
- No `FIXED` claim without a file and line that carries the fix. That is the rule
  the first revision of the findings document broke.
- Diff ruff findings against `main` rather than comparing totals. An unchanged
  count once hid six new findings behind six fixed ones.
- Reconcile finding ids against commit `Closes:` lines rather than trusting a
  running total. That caught two miscounts in this document.

---

# After the findings: what comes next, and why

The 37 findings are closed. This section records what the next piece of work is,
and — more importantly — why it was chosen over the alternative, so that a
session picking this up does not have to re-derive the choice or silently make a
different one.

## The framing

The assessment below borrows a structure from an external article on a
"reasoning control plane" (DZone, 2026) — an argument that multi-agent systems
need a layer governing reasoning itself, described along four dimensions: a
shared semantic context, agent-to-agent access controls that attenuate as
authority is delegated, observability over non-deterministic decisions, and
deterministic guardrails outside the model's reasoning context. Its summary of
that last one, *"reasoning proposes, policy disposes"*, is the harness's own
design stated in four words.

It is borrowed as a way of seeing the harness from outside, not as a standard to
conform to. What follows is our assessment against it, with the evidence.

## Where the harness actually stands

**Deterministic guardrails — strongest.** Batches 1 and 7 are this dimension.
The execution fence floor (`VCS_DIRS`, `STORE_DIRS` in `core/tools.py`),
`PROTECTED_SETTINGS` and `PROTECTED_PROVIDER_KEYS` in `config.py`, the mandatory
quality bars in `core/dod.py`, `verify_command`'s executable allow-list. Batch
7's rule — a workspace may tune how much work the harness does, not how sceptical
it is about work done on it — is the same principle in the harness's own words.

**Observability — the material is complete, the rendering is absent.** After 5b,
`RunState` carries turns, notes, task notes, drift assessments, directives,
inboxes, and the three damage signals (`orphaned_events`, `rejected_events`,
`damaged_lines`). Every input to every decision is on the log. What is missing is
a reader that assembles them: nothing answers "why was *this* directive issued to
*this* agent" in one place. That is a view over complete data.

**Shared semantic context — partial.** `RunState.shared_context` and
`RunState.facts` are event-sourced (`CONTEXT_SET`), reach briefs through
`render_context`, and since 9b the supervisor answers an agent's questions from
them. But they are free text, unversioned, and nothing checks that two agents
mean the same thing by a term. `detect_contradictions` compares findings, not
vocabulary.

**Agent-to-agent access controls — weakest.** `RunState` has no scope field. Every
`Scope` is supplied independently by the synthesis model, per task
(`phases.py:591`), and nothing anywhere compares one scope to a wider one. There
is no run-level envelope, no attenuation on spawn, and no time bound on a scope.
The batch-1 floor is the only universal bound. *This claim is from a reading of
the code and a grep; verify it before designing against it.*

## The choice: scope provenance, not the decision journal

Both dimension 2 and dimension 3 are worth closing. Scope provenance goes first,
for three reasons in ascending order of weight.

**The cheap one is that costs are asymmetric.** The decision journal is a
read-side addition over data that already exists; it will be exactly as easy in
six months. The envelope changes how authority flows and touches spawn, approval
and the fence — and doing it while the fence work of batches 1 and 7 is still
fresh is cheaper than doing it cold.

**The structural one is that the gaps are different in kind.** Dimension 3 is a
missing *view* over complete data: nothing is lost while it is absent, and it can
be added at any point. Dimension 2 is a missing *fact*. No envelope is ever
established, so there is nothing to render, enforce or audit — and authority that
was never recorded cannot be audited retroactively. A run completed today leaves
no evidence of what it was entitled to touch.

**The decisive one is that we have already settled this exact principle, and
applied it to only half the problem.** Batch 7 established that the subject of a
judgement may not set the terms of it: a repository shipping a config cannot
lower the bar its own changes are judged against. Scope is the same class of
problem and is still open — the synthesis model draws the fence that its own
tasks then run inside, and the harness enforces that fence immaculately.
Batches 1 and 7 hardened *enforcement* of a scope; neither asked where the scope
came from or who was entitled to grant it.

That the original review produced 88 findings and not one about scope provenance
is itself evidence the gap is structural rather than incidental. Five analysis
lenses each took the scope as given, exactly as the code does.

## What the decision journal would be, when it is taken up

`supervisor explain <run> [agent]`: for each directive, the brief the agent was
given, the turns before it, the inbox it carried, the drift assessment and its
signals, the directive chosen and its rationale, and — since 9b — any question
answered from the run's record. All of it is already in `RunState`. No new
events, no design risk, no model call.

---

# Scope provenance: the run envelope — done

Landed on `feat/scope-envelope-and-attenuation`. The section above chose this
over the decision journal; this records what it turned out to be.

## What the reading got right, and the one thing it missed

Every claim in the assessment above verified. `RunState` had no scope field
(`models.py`), `_spawn` related no child scope to a spawner's, nothing anywhere
compared one scope to a wider one — `core/paths.py` matched a *path* against a
pattern and stopped there — and both scope sources were models: `phases.py:158`
for a lens, `phases.py:591` for a task.

One thing the assessment did not name, found while verifying it.
`build_verification_agent` gave every verifier `Scope(out_of_scope=…)` with
**empty `paths`**, and `core/tools.py` reads an empty `paths` as "the whole
workspace". The verifier was therefore the *least* fenced agent in every run:
free to write anywhere outside the batch-1 floor, and exempt from the executable
allow-list a scoped agent is held to — while judging an agent that was fenced. It
now carries the task's own paths.

## The shape of it

**The envelope is established twice, and narrows.** `Supervisor.start` emits
`ENVELOPE_SET` from `policy.scope_envelope` before any model has been asked
anything; `_apply_plan` emits a second one, beside the `RUN_MODE_SET` it already
emitted, carrying the intersection with what the plan proposed. Two events rather
than one so the log carries the provenance and not just the answer, and so an
envelope exists on every path out of `start` — including the one where planning
is abandoned and the derived lens plan runs instead.

`policy.scope_envelope` is deliberately **not** in `PROTECTED_SETTINGS`. The
default is already the widest an envelope can be, so a workspace file can only
narrow it, and a workspace narrowing what the harness may touch inside it is the
direction batch 7's rule permits.

**Attenuation is in `_spawn`, not in the four builders.** Every agent in a run
passes through that one method, so a builder that forgets is a builder that
*proposes* too much rather than one that *grants* too much. The ceilings are the
run envelope, the task's scope when the agent works on a task, and the spawning
agent's scope where `AgentSpec.parent_agent_id` names one — which today is the
verifier, pointed at the executor whose work it judges. A narrowing is never a
refusal, and is always a note on the log naming which ceiling bit.

A task's scope is *also* clamped where it is proposed, so the scope the user
reads at approval is the scope that will be enforced, and the narrowing arrives
in the `task_notes` the approval response already carries.

**`RunState.envelope` is `ScopeEnvelope | None`.** `None` means no envelope was
ever established — a run recorded before this existed — which is a different fact
from an envelope naming the whole workspace, and is reported as one.
`supervisor status` prints all three states.

## The sentinel, which is the part most likely to be got wrong later

An empty pattern list means "the whole workspace" everywhere downstream. So an
intersection that comes out empty **cannot** be written `[]`: that would widen
the scope it was computed to narrow, and a task proposed entirely outside the
envelope would become the least fenced agent in the run. `narrow_globs` returns
`[paths.NOTHING]` instead, and `path_matches` refuses that value explicitly
rather than leaving it to `fnmatch` — so a file genuinely named `<nothing>` does
not match the pattern that exists to match nothing.

## The honest limits of `pattern_within`

Sound, not complete. `True` is a proof; `False` means "not provably contained",
which is the safe direction because every caller narrows on `False`. It decides
three cases — a concrete path (exactly, via `path_matches`), identity, and a
literal prefix lying under a directory pattern's base on a path boundary — and
refuses two: containment needing wildcard compared against wildcard
(`src/auth/*.py` inside `src/**/*.py` holds, and it says no), and containment in
a *union* rather than in one member (`src/*` inside `{src/a*, src/b*}` depends on
what is on disk, and a fence that changes meaning when a directory is created is
not a fence).

`*` and `**` are named as universal. Without that, an envelope written the
natural way — `["**"]` — would have contained nothing at all, because neither
pattern has a directory base to reason about.

## Whether the user's approval may widen the envelope — no

Both answers were defensible. The recorded one, with the argument, is in
`core/envelope.py`'s module docstring; in short:

A `scope_paths` modification at approval is clamped like any other scope and the
clamp is recorded. A per-task approval is a decision about *that task*, and if it
could move a run-level bound then the bound would only ever be as strong as the
most permissive task anyone approved — which is not a bound. It would also be
invisible: the envelope would have to be reconstructed afterwards from the union
of every per-task edit, which is the "authority that was never recorded cannot be
audited" problem the envelope exists to close.

The cost is real and is not hidden. If the plan draws the envelope too narrowly,
every task is clamped and the user cannot widen it from the approval prompt; what
they can do is start the run with a wider envelope, which is visible from the
beginning and applies to everything. The rejected alternative — a distinct
`widen_envelope` act at approval — buys back a restart at the price of making the
run's grant something that changes shape midway through the run.

## Verification

30 new tests in `tests/test_scope_envelope.py`; 249 pass in total.

A module that did not previously exist makes "fails against the previous commit"
cheap and nearly meaningless — the whole file fails to import, which says nothing
about any individual test. So each mechanism was instead put back the way it was,
one at a time, and the tests were checked for noticing:

| mechanism disabled | test that went red |
|---|---|
| attenuation in `_spawn` | `…analysis_lens_scoped_outside_the_envelope_is_narrowed_at_spawn` |
| verifier scope left empty | *(none alone — see below)* |
| clamp when a task is proposed | `…task_proposed_outside_the_envelope_runs_against_the_intersection` |
| clamp on an approval modification | `test_approval_cannot_widen_the_envelope` |
| plan allowed to widen configuration | `…configuration_bounds_the_run_even_when_the_plan_asks_for_more` |
| `ENVELOPE_SET` not folded | seven, including both resume tests |
| envelope absent from `status` | `test_status_reports_the_envelope` |
| `NOTHING` left to `fnmatch` | `…empty_intersection_is_written_as_nothing_not_as_empty` |
| empty intersection spelled `[]` | the same |

The verifier row is the interesting one. Two mechanisms hold that property now —
the builder copies the task's paths, and attenuation would narrow an empty scope
to the same ceiling — so the test goes red only when *both* are removed, which
was confirmed. That is defence in depth working rather than a vacuous test, and
the test's docstring says so.

Three tests are marked in their docstrings as **guards rather than proofs**: the
two that pin containment the predicate declines to decide, and the one that
asserts which patterns survive a narrowing. Those pin today's decision procedure,
not a necessary answer — a sharper `pattern_within` would correctly make them
fail, and should.

Ruff was diffed against `main` by rule and file, not by total: 58 findings before
and 58 after, with no new pair. Two were introduced and fixed on the way
(`UP037` in `core/envelope.py`, `I001` from the new import in
`core/supervisor.py`).

## Not done, deliberately

- **No time bound on a scope.** The assessment above named it alongside the
  envelope. It is a different mechanism — an envelope is about extent, a bound is
  about duration — and nothing in the harness currently expires anything.
- **`core/supervisor.py` is still one module** (9c), now a little over 2200
  lines. Attenuation added one method and two helpers to it rather than being
  spread across the four agent builders, which is the right trade for a fence but
  does make the module larger.
- **The open half of `fnd_01M130P3E5SCF8`** is untouched.
