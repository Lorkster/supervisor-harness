# The autonomy plan: fewer interruptions, the same bar

*Written 2026-10-05. Live: the work in it is scheduled, not done.*

A working document in the same shape as [`development-plan.md`](development-plan.md):
what each piece of work is, what has been *verified* about it, what is still only
a reading of the source, and what has been decided. It stays in the present tense
of the day it was written. Where a batch finds that the plan was wrong, the
finding goes beneath the batch and the plan's text stays as it was.

It does not replace the nine-batch plan. That plan's batch 9 is still open, and
nothing here depends on it except where noted under [batch G](#batch-g--a-reviewer-that-can-only-veto-conditional).

- [Why this plan exists](#why-this-plan-exists)
- [What was taken from turnstone, and what was not](#what-was-taken-from-turnstone-and-what-was-not)
- [The argument: where a person adds something](#the-argument-where-a-person-adds-something)
- [Constraints from security-eval](#constraints-from-security-eval)
- [Does a UI add value, and where does it go?](#does-a-ui-add-value-and-where-does-it-go)
- [Part 1: the harness](#part-1-the-harness)
- [The gate between the parts](#the-gate-between-the-parts)
- [Part 2: the outer loop](#part-2-the-outer-loop)
- [Decided against](#decided-against)
- [Questions for the owner](#questions-for-the-owner)

---

## Why this plan exists

The question was whether development work can run more autonomously without
losing output quality, and whether the harness's human checkpoints are needed at
all with the right architecture. A local agent gateway (Hermes) had been
frustrating to run, while the same models driven through Claude Code, or through
this harness, worked well. That contrast is the useful part. The difference is
the loop around the model, not the model alone.

The reading that prompted the plan is
[turnstonelabs/turnstone](https://github.com/turnstonelabs/turnstone)
(Apache-2.0 since 1.6.0), a self-hosted orchestration platform for tool-using
agents. Its value here is not its code. It is
[`PRIMER.md`](https://github.com/turnstonelabs/turnstone/blob/main/PRIMER.md),
a plain-language account of what a harness can and cannot promise, which states
one rule more exactly than this repository had.

## What was taken from turnstone, and what was not

Turnstone is a platform: CLI and web UI, cluster routing, RBAC, SSO, Slack and
Discord channels, about 200,000 lines. Almost none of that is relevant. Four
ideas are.

**1. Only the owner widens; judges only tighten.** A model-based check may veto
something the deterministic rules allow, and may never approve something they
refuse. A judge's reasons come from a fixed menu, never free prose. The corollary
is the one this plan is built on: *a fully autonomous run is a run whose owner is
unreachable, so its authority is frozen at launch.* This harness already enforces
the first half: the drift model's second opinion "can lower a score the
heuristics are unsure of, never below what a scope violation alone says". What it
has not done is draw the corollary about approval.

**2. The irreversible line is where the decision belongs.** "Anything
irreversible is decided at the gate. The verifier can reject a bad result; it
cannot unsend the email." A code change on a branch is reversible until it is
merged. So the human decision that cannot be replaced is at merge, and this
repository already has it: every batch ends at a pull request the owner merges.

**3. Declared success is not the same as a correct result.** The primer
separates *success* (the shell said done), *safety* (nothing bad was touched)
and *correctness*, which "no dashboard inside the system can produce; only a
judge outside the run — a test suite, an audit, ground truth — can." Here that
means the person reading each task at approval is doing a quality job, and if
the person goes, something outside the model has to do it instead.

**4. Measure the loop, per model.** `turnstone-eval` scores tool use against
expected and *forbidden* actions, with several runs per case and held-out cases
kept out of tuning. Turnstone also uses it to test whether the *wording* of its
nudges changes what a model does. This harness has two measured cases of a
directive being ignored: the parallel-dispatch instruction (issue #62) and the
architecture lens ignoring a coverage nudge (PR #78).

What was **not** taken is listed under [Decided against](#decided-against): the
per-tool-call judge as a default, Smart Approvals (an LLM approving on
confidence), the in-run prompt optimizer, and the platform.

## The argument: where a person adds something

Today a run pauses at `awaiting_approval` and a person approves, modifies or
rejects each proposed task.

That approval **cannot widen the envelope**. An edited scope is clamped to it
([reasoning-control-plane.md](reasoning-control-plane.md), "Approval may not
widen the envelope"). So as an authority decision, per-task approval adds
nothing the envelope does not already enforce. What it does add is a *quality*
judgement: are these the right tasks, and are these the right definitions of
done? And it is the largest idle in the one run that was measured: at most 83%
of that run's wall clock was inside dispatches, and the biggest gap was a person
reading ([#62](https://github.com/Lorkster/supervisor-harness/issues/62)).

So the plan moves the person rather than removing them:

| Decision | Today | After this plan |
| --- | --- | --- |
| What the run may touch | the configured envelope, narrowed by the plan | the same, **granted once by the owner at launch**, recorded with who and when |
| Whether each task is right | a person reads every task | deterministic gates, plus a verifier that is not a model; a veto-only reviewer if runs show it is needed |
| Anything needing more authority | not possible mid-run | an **escalation**: one task parks and the others continue |
| Whether the result ships | the owner merges a PR | unchanged, and now the main checkpoint |

Per-task approval stays the default. Autonomy is something the owner turns on,
and it is protected so that a workspace cannot turn it on for itself.

**One precondition.** Without a person in the loop, a false stop is no longer
caught; it just becomes a failed task. The turn-budget investigation
([#67](https://github.com/Lorkster/supervisor-harness/issues/67)) has a strong
candidate cause, absolute scope paths, fixed in #75 and #76, but it is not
confirmed against the run that reported it. Batch E's go-live condition includes
re-running `tools/where_the_turns_went.py` on that log.

## Constraints from security-eval

[security-eval](https://github.com/Lorkster/security-eval) depends on this
harness. Every batch below has to respect four things about it.

**1. It reads the harness through a published surface, and that surface is a
contract.** The harness condition runs
`supervisor run TASK --mode report --backend autonomous --yes --json -w TREE`,
then reads `findings`, `status`, `events` and `providers`, all with `--json`.
The baseline condition imports `load_config`, `ChatMessage`,
`CompletionRequest`, `ProviderRefusal` and `ModelRouter` directly. Some of what
it reads is prose. It finds why an agent was stopped by looking for
`finished (stop):` inside a note's text. Nothing in this repository fails if any
of that changes. Batch A makes it fail.

**2. It pins a harness commit, and the pin is part of its study design.** The
study's pre-registration fixes the harness version before the pilot. A harness
change reaches the study only when the group moves the pin on purpose. So every
batch here states whether it **changes the harness condition**, meaning whether
a report-mode run behaves differently afterwards. Most do not: autonomy concerns
execute mode, and security-eval runs report mode. The ones that do (batch H, and
nine-batch 9b) are marked, and the group decides when or whether to take them.

**3. The integrity line holds here too.** security-eval's
[`working-with-regulated-models.md`](https://github.com/Lorkster/security-eval/blob/main/docs/working-with-regulated-models.md)
commits to maximising regulated models' *legitimate* performance and not tuning
toward a conclusion. For this repository that means any change aimed at
detection quality is developed against the harness's own fixtures and
security-eval's *tuning* split, never against held-out targets or adjudicated
results, and is judged on a run the change was not tuned on.

**4. The group's view of the results.** The group has said the harness condition
is not performing well against the attacker proxies (RQ7: of what an unregulated
model finds, how much do the regulated conditions find?). Their specific
comments were not available when this was written, so this plan treats the
claim as a question to measure rather than a defect to fix. The harness's
legitimate levers are exactly the ones its own investigations keep finding:

- agents stopped while working (the recidivist rule, #67);
- agents accepted before reading their scope (the coverage gate, #78);
- synthesis as one judgement (nine-batch 9b);
- and, so far unmeasured, which *role* a given model is weak in.

Batch F produces that last measurement, per model and per role. It is also the
missing half of security-eval's RQ4 (failure attribution: the model or the
harness?). If the gap is the harness, the fix belongs here. If it is refusals or
model capability, the study should say so.

## Does a UI add value, and where does it go?

Two different needs hide in the question.

**Following a run while you watch it.** The host is already the UI. Under Claude
Code, the ledger line prints on every dispatch, and `status` and `explain` answer
questions. A separate UI for a run you are watching would duplicate the terminal.
**Low value.**

**Being the owner of work you are not watching.** Once runs go autonomous, and
especially once a loop runs them overnight, three things need a place to live:
the escalations waiting for an answer, what each run did and what it cost, and
the PRs waiting for review. A terminal you are not looking at is not that place.
**High value, but only once there is an outer loop.** With the harness alone you
start every run yourself, so you are already there.

**Where it goes: the outer loop, built on data the harness publishes.**

- **The harness publishes data, not screens.** `status --json`, `events --json`,
  `findings --json` already exist; batch C adds `escalations --json`. A UI is one
  more consumer of the same published surface security-eval reads, which keeps
  that surface honest. It is also an *observing* surface in batch 8's sense, and
  observers do not belong inside the enforcing core.
- **Answering an escalation is not observing.** It grants authority. So the UI
  never holds that state. It calls the harness's own resolve command, and the
  decision lands on the run's log like any other approval.
- **Offline by default.** Some workspaces are on a machine whose run data may not
  leave it. The precedent is security-eval's `review.html`, a generated page that
  opens with no network. For repositories already on GitHub, the same inbox can
  be draft PRs and issue comments, which reaches a phone without hosting anything.

Recommendation: no UI work until the outer loop exists. Then its first screen is
the escalation inbox, generated offline. Batch L2 below.

---

## Part 1: the harness

Each batch is one PR, branched from main. Batches are never stacked.

### Batch A — The consumer contract

Pin everything security-eval reads, as tests in this repository:

- the `run --json` response fields (`run_id`, `action`, `ledger`, `message`);
- `status --json`: `phase`, and `agents[]` with `kind` and `status`;
- the event shapes it folds: `turn_recorded` usage and `files_read`, and the
  notes that carry refusals;
- `findings --json`, and `providers --json` `routing`;
- the provider-layer names the baseline imports, with the call shapes it uses.

Add a **structured stop reason**: the `agent_status` event for a stopped agent
carries the directive's rationale as a field, and `status --json` shows it. The
`finished (stop):` note stays as it is, so the group can move off it whenever
they like.

*Changes the harness condition:* no. Report-mode behaviour is identical; one
field is added.

> **Done** in [#80](https://github.com/Lorkster/supervisor-harness/pull/80),
> wider than planned in one place. The cause is recorded on *every* transition
> that ends an agent, not only a stop: a fixed `EndCause` (`accept`, `stop`,
> `escalate`, `reported`, `abandoned`, `refused`, `error`, `turn_budget`,
> `remediation`) plus the reason given, shown in `status --json` as `ended`.
> The plan's "the directive's rationale" covered only the directive paths, and
> a refusal or an abandonment is exactly what a consumer most needs to tell
> from a stop. Each mechanism was broken in turn: 9 of 9 turned a test red.

### Batch B — A verifier that is not a model

The owner's own verification bar, made mechanical. Batches 1–8 kept finding
first-draft tests that passed against unfixed code. The fix each time was to run
the new tests against the previous commit and see them go red.

**A new harness-owned criterion, `fails_before`:** *the tests this task adds or
changes fail on the baseline commit, then pass with the change.* The harness
proves it itself:

1. a temporary git worktree at the run's baseline commit;
2. the task's changed test files copied into it;
3. those test modules run there, then in the working tree;
4. a per-test comparison.

It passes only if at least one test that passes now did not pass at the baseline.
It fails, with the test ids named, if every one of them already passed: those
tests do not detect the change. The verdict is an ordinary `criterion_verified`
event, so replay never re-runs it.

Applied as a quality bar where it fits: the task changes behaviour (not a
refactor), the workspace is a git repository with a baseline, and the runner is
pytest. When the harness may not run commands
(`allow_command_execution: false`), the criterion goes to the verifier agent with
the procedure written out, and the verdict is recorded as an agent's claim, as
every delegated criterion already is. A new protected policy key,
`require_fails_before`, turns it off.

**Also in this batch, if a test confirms it:** a criterion the harness proved
`fail` on attempt 1 appears never to be re-checked on attempt 2.
`_verify_mechanically` only looks at unverified criteria, nothing resets them
when a task is reopened, and a verifier's contrary verdict is discarded because
"the mechanical result stands". If true, remediation of a mechanically failed
task cannot succeed. Reading only; the batch starts with a test that shows it.

*Changes the harness condition:* no. Execute mode only.

> **Done** in [#81](https://github.com/Lorkster/supervisor-harness/pull/81),
> with three corrections to this plan.
>
> - **The suspected defect was real**, and a test showed it before the fix:
>   remediation could not pass a criterion the harness had failed. Reopening a
>   task now returns its verdicts to unverified. A second one turned up beside
>   it: a verifier *agreeing* with a mechanical verdict re-attributed it to
>   itself, so the trajectory exported the harness's proof as a model's claim.
> - **The bar is not handed to a verifier agent.** The plan said that without
>   `allow_command_execution` the criterion would go to the verifier with the
>   procedure written out. Reviewed with the owner, that turns the check back
>   into a model's claim, the thing it exists to replace, and adds a worktree
>   procedure to every host-run task. It is added only where the harness can
>   run it, which is also batch E's precondition.
> - **A task that only adds tests** for behaviour that already exists would
>   have been failed: its tests are meant to pass on the baseline. Such a task is
>   judged by its tests passing, read from the tree as well as the agents'
>   reports, so a missed report errs towards running the comparison.
>
> Also found while building it: the baseline side must be given its own
> `PYTHONPATH`, or an editable install makes it test the changed code. This
> repository's own conftest hides that. 19 mechanisms broken in turn, 19 red.

### Batch C — Escalations

Today `DirectiveKind.ESCALATE` ends an agent as `blocked`, and nothing tells
anyone. This batch gives "needs the owner" a destination:

- `escalation_raised` and `escalation_resolved` events. The reason is a fixed
  value: `needs_wider_scope`, `needs_command_execution`, `irreversible_action`,
  `reviewer_veto`, `attempts_exhausted`, `envelope_stale`, `agent_blocked`. An
  agent's own words travel with it as data, labelled as such.
- A parked task does not block its siblings. Its dependents wait.
- `supervisor escalations [RUN] --json` lists open escalations. They are answered
  through `approve`, which records the decision as it records task decisions now.

*Changes the harness condition:* no. A blocked analysis agent ends exactly as
before; the escalation is recorded beside it.

> **Decided by the owner, 2026-10-06:** when an escalation is raised, the run
> does everything else it can and then pauses at a new `awaiting_owner` phase,
> shaped like `awaiting_approval`. Recording escalations without anywhere to
> answer them was the alternative, declined. And a run that has completed is
> never reopened: an open escalation keeps it from completing, and a declined
> task is carried forward in the report, with its definition of done, for a
> later run to start from.
>
> **Done**, with four departures from this plan.
>
> - **Answered through a new `supervisor_resolve`, not `approve`.** `approve`
>   moves the run to execution unconditionally, which is right at
>   `awaiting_approval` and wrong anywhere else; folding a second decision into
>   it would have given one call two meanings. CLI: `supervisor escalations`,
>   `supervisor resolve ESC grant|decline --note`.
> - **One reason, not seven.** Only `agent_blocked` is raised today. The others
>   arrive with the batches that can raise them, so nobody reading the enum has
>   to rule out values nothing produces.
> - **A decline defers rather than fails**, and the final report lists the
>   deferred task with its definition of done as work to carry forward.
> - **Report mode is untouched.** An analysis agent that blocks raises nothing
>   and ends as before, so security-eval's harness condition does not change.
>
> Found on the way: `Supervisor.run` would have looped forever on the new
> action, since it advanced on anything it did not recognise. And both paths
> that end an execution agent sent an escalating agent's task straight to
> verification, where a verifier could pass the work its own agent had said it
> could not finish. `docs/protocol.md` also said there were eleven tools when
> there were twelve: `supervisor_explain` was missing from its table, and is now
> there beside `supervisor_resolve`. Fifteen mechanisms broken in turn: thirteen red, one a
> hang (the `run` loop), and one survivor, the execution-only check, which is
> redundant by construction (an analysis agent has no task to park).

### Batch D — A run on its own branch

An execute-mode run writes into a git worktree on a branch named for the run,
not into the owner's working tree. The run ends with the branch and a summary of
the diff. **The harness never pushes.** Pushing reaches outside the machine,
which is the owner's decision, or a loop's with the owner's standing permission.

This is what makes "reversible until merged" true, rather than true only if the
owner happened to start on a clean tree. It is a run-level worktree. The
per-agent worktree the nine-batch plan declined stays declined: agents in one
run still share one tree, and `core/baseline.py` still exists for that reason.

The autonomous backend comes first. Under a host, the harness cannot fence the
host's own tools, so the packets name the worktree, and drift's scope check
already flags files touched outside it.

*Changes the harness condition:* no. Report mode writes nothing.

> **Done**, for the autonomous backend only, with these decisions made in the
> building.
>
> - **Host-delegated runs are not isolated at all yet**, rather than given a
>   worktree the packets merely name. The plan's own reason applies to the
>   whole of it: the harness cannot fence a host's tools, and a worktree the host
>   may or may not write into is a promise it cannot keep. A test holds that a
>   host-delegated run gets no branch.
> - **The run commits its changes on the branch** when it wraps up, then
>   removes the worktree, so what is left is a commit to diff, merge or drop.
>   If the commit fails (most often no git identity), the worktree is kept with
>   the changes in it, because removing it would delete the only copy.
> - **Fail closed where it can be honoured.** In a git workspace where the
>   branch cannot be made, the run stops before any agent works. In a workspace
>   that is not a git repository there is nothing to branch from, and it works in
>   place as before, saying so in the report.
> - **The owner's uncommitted work is protected, not carried.** The branch
>   starts at the baseline commit; the run's notes say so when the tree was
>   dirty.
> - `policy.execution_worktree`, default on, and not protected: turning it off
>   puts the work back in the owner's own tree, which is the owner's call.
>
> Not yet tested on a repository deep enough to hit Windows' 260-character path
> limit; the worktree sits under `.supervisor/runs/<id>/worktree`, which adds
> about 45 characters to every path. Fourteen mechanisms broken in turn,
> fourteen red.

### Batch E — Approve the envelope, not each task

A protected policy key, `approval: "task" | "envelope"`, default `"task"`.

- In `"envelope"` mode the owner grants the envelope when starting the run
  (`--grant-envelope` on the command line, or a field on `supervisor_start`). The
  grant is recorded with `granted_at` and by whom.
- Tasks inside it that pass every deterministic gate proceed without a pause.
  Anything else becomes an escalation (batch C): a scope the clamp had to
  narrow, a criterion the harness will not run, a reviewer veto (batch G).
- **No autonomy without a verifier that is not a model.** Envelope mode refuses
  to start unless `allow_command_execution` is on, the workspace has a baseline
  commit, and batch D's branch can be created. Without those, nothing checks the
  work except models, and that is the always-open lock again.
- `--yes` keeps its meaning. It approves everything and records nothing about a
  grant. security-eval uses it in report mode, where it never fires.

*Go-live condition, before calling this done:* the turn-budget log re-read
(#67), and three envelope-mode runs on the owner's own repositories that end in
PRs the owner would merge.

*Changes the harness condition:* no. Execute mode only.

> **Built**, and **not yet done** by this plan's own definition: the go-live
> condition above is still open. Neither the #67 log nor three real runs can be
> produced by a pull request; both are the owner's to run. Until then this is
> the mechanism, tested, not a mode anyone should rely on overnight.
>
> What was built, and where it departs from the text above:
>
> - **Command line only** (`supervisor run --grant-envelope`), not a field on
>   `supervisor_start`. Envelope mode needs batch D's branch, which only the
>   autonomous backend has; a host-delegated run could not honour the grant.
> - **The gate is four checks, each able only to send a task to the owner:** the
>   envelope clamped its scope; its definition of done has a defect
>   `validate_criteria` rates high; a mandatory check is one the harness will not
>   run; the plan rated it high or critical risk. The fourth is not in the text
>   above. The rating is the planning model's own, so the only thing a model can
>   do with it is send its own task to the owner, which loses nothing.
> - **The preconditions are wider than the text**: as well as command execution,
>   a baseline commit and the run's branch, envelope mode needs `require_tests`
>   and `require_fails_before`. Without them a code task could reach execution
>   with nothing proving its tests detect the change.
> - **A grant lets a parked task go ahead as it stands.** For a narrowed task
>   that means narrowed: no approval widens the envelope, this one included.
> - **What no person decided is recorded as no person deciding.** A harness
>   approval carries `"by": "envelope"` and the trajectory exports it as policy,
>   not as a person.
> - **Everything refused goes straight to the owner**, rather than through an
>   execute-verify-checkpoint cycle with nothing in it.
>
> The end-to-end test is the whole loop with nobody in it until the end:
> granted at start, approved by the gate, executed on the run's branch, proven
> by the project's own tests and `fails_before` run by the harness, committed,
> and the owner's tree untouched. Fifteen mechanisms broken in turn, fifteen
> red.

> **Found preparing the go-live runs, 2026-10-06**, before any model was asked
> anything. Three things would have failed every task in the owner's other
> repositories for reasons unrelated to the work, and envelope approval would
> have blamed the model:
>
> - **The wrong test runner.** `detect_test_command` searched the whole tree
>   for `test_*.py` before reading the root's own markers, so a TypeScript app
>   with a Python sub-project got `pytest` at its root. Root markers now come
>   first, and the tree search is gone.
> - **The wrong Python.** The `pytest` on PATH is the system Python's; a
>   project's suite passes in its own `.venv` and fails at import outside it.
>   The command now names the project's virtualenv interpreter, absolutely.
> - **A worktree without dependencies.** Batch D's worktree holds only what git
>   tracks, so it had no `node_modules`. It now gets its own from the lockfile
>   (`npm ci`), under the command-execution switch, and they are never
>   committed. Linking the owner's copy was rejected: an agent's `npm install`
>   would then change the owner's dependencies, which is what the branch exists
>   to prevent.
>
> **The #67 log is no longer available**, so that half of the go-live condition
> cannot be met as written. Its purpose -- knowing whether the supervisor stops
> agents that are working -- is served instead by reading
> `tools/where_the_turns_went.py` on each of the go-live runs.

> **Go-live run 1, 2026-10-06** (this repository, item 9a of the nine-batch
> plan, `qwen3.8-code` through Ollama, 10 minutes). Analysis was good: the
> technical lens found where `conc` is computed and that `Span` carries no
> phase. Synthesis cut the item into five sensible tasks. Every one was sent to
> the owner, for three reasons, two of them the harness's:
>
> - **A false positive in the gate.** A task that declared no scope inherits the
>   envelope, and the clamp's note for that was read as the task being narrowed.
>   Only a declared scope can be narrowed now.
> - **The plan's paths named nothing.** The planner drew the envelope as
>   `core/timing.py` for `src/supervisor_harness/core/timing.py`; agents fenced
>   to it could not have written the code. A path that names no file is now
>   placed at the only file it can mean, and noted (`core/placement.py`);
>   otherwise it is kept and the note says it names nothing.
> - **The model's own definitions of done.** Two criteria per task were
>   `method: test` with no command. The gate refusing those is it working.
>
> The run waits at `awaiting_owner`; its escalations are the owner's to answer.

> **Go-live run 2**, the same task after the fixes above. Both held: the plan's
> paths were placed (`core/timing.py` to `src/supervisor_harness/core/timing.py`)
> and no task was counted as narrowed. All four tasks still went to the owner,
> now for one reason: the model's `method: test` criteria named no command, in
> every task, in both runs. A person at approval would have asked for the
> command; nothing in the harness did. Now it does, once: a synthesis whose own
> criteria cannot be enforced is sent back with each criterion named and what
> is wrong with it, and the revision is used whatever it says. The turn audit
> on both runs found **no agent stopped by the supervisor and none out of
> turns**, the question the #67 log was for; it did find every lens told to
> narrow, because the lenses' own scopes had the same missing-prefix paths.
> Those are placed now too.

> **Go-live run 3** (plantsandclimate, task P3-18 of its own plan, 8 minutes).
> A good plan again -- five tasks, a sensible envelope -- and every task sent
> to the owner for the same missing commands. And one finding that is exactly
> what the #67 check existed for: **four of five analysis lenses were stopped by
> the supervisor on their first turn.** The plan had marked the documents the
> task said to read as not to be modified, and an analysis agent's reported
> reads arrive in the same field as an execution agent's writes, so reading
> them was an "uncorrectable" violation. The drift model's own second opinion
> said the agents were working correctly. A forbidden path now constrains only
> an agent that can write.

> **Go-live runs 4 and 5, 2026-10-06**, P3-18 and 9a again after #86. **Tasks
> went ahead within the envelope and executed, in both.** All lenses accepted in
> run 4 (none stopped), and the send-back fired: the revision turned most
> criteria into inspections the harness can check.
>
> *Run 4* (P3-18, 8 tasks, 15 minutes): one approved and executed -- add `test:e2e` to
> `npm run check` -- verified 2/2, and **wrong**. plantsandclimate's CI runs
> `npm run check` before it installs Playwright's browser, so the change would
> break CI. Every check passed because every check was about the text of
> `package.json`. This is batch G's condition, met: a task reached execution
> that a person would have rejected. The run's branch is a proposal, and the
> owner's merge is where it would have been caught -- which is the design. The
> other seven went to the owner for one criterion each, "existing unit tests
> still pass" naming no command, kept through the send-back. The drift judge,
> shown the whole request as "the overall task", also stopped the executed
> task's implementer for "abandoning the primary task" after it had done its
> own.
>
> *Run 5* (9a, 4 tasks, 85 minutes over three remediation rounds): all four approved and dispatched in
> parallel into the run's worktree. One produced good code -- per-phase
> concurrency in `core/timing.py`, six tests, and `fails_before` proved all six
> fail on the baseline and pass with the change -- then failed its full-suite
> criterion on three CLI tests that read the harness's own `SUPERVISOR_HOME`,
> inherited by the check. Without it the suite passes (969). Two tasks named
> files outside the envelope (`core/reporting.py`, `core/supervisor.py`), which
> the plan had drawn from the files the prompt named, not the one it described;
> their agents could not write them, and escalated after ten turns and three
> rounds of remediation. Two implementers cycled read/search/read for six tool
> rounds, which one reported itself and no signal caught -- the evidence batch H
> waits for, though the log does not record arguments, so "identical" is the
> agent's word.
>
> Fixed after them: the drift judge sees an implementer's own task; brief echo
> does not apply to an implementer reporting a change; a one-word action is
> replaced by the title; a whole-suite criterion naming no command runs the
> project's suite, and a criterion that cannot run no longer displaces the
> harness's test bar; checks do not inherit `SUPERVISOR_*`; and a task whose own
> words name a file its scope does not cover goes to the owner before anyone
> works on it. Replayed over runs 2-5, that last check names exactly the four
> tasks that hit the gap, and no other of the 21.

> **Go-live runs 6 and 7, 2026-10-07**, the same two tasks after #87, 14 and 13
> minutes. The scope check fired where it should, in both: P3-18's Playwright
> task (its tests live in `e2e/`, outside the envelope) and 9a's note task
> (`core/supervisor.py`) went to the owner before anyone worked on them. Run 6
> again executed the one task that adds `test:e2e` to `npm run check` -- the
> change batch G exists for, proposed by the plan twice now.
>
> Two harness defects, both measured. *Run 6:* the UX lens read the two
> documents the request named, outside its scope, and was stopped while the
> drift model said it was "correctly performing the required analysis" -- the
> scope signal is a floor the model cannot lower, and a lens reports its reads
> where an implementer reports its writes. A lens reading outside its scope is
> now its own signal, judged rather than floored. *Run 7:* every implementer
> read until its tool rounds ran out, was told only to "answer now with what you
> have", and reported itself blocked -- "need additional turns" with nine of ten
> unused, and one asking whether it might create a test file inside its own
> scope. A blocked report parks the task, so four tasks were parked with nothing
> written. An agent out of tool rounds is now told the turns it has left, and an
> implementer that `blocked` is not for needing more turns; it is also warned
> one round early, while it can still write.
>
> Two model weaknesses remain, and are the planner's: dependent tasks proposed
> without `depends_on` (run 7's predicate task needed the accessor task's code,
> and both were dispatched at once), and a separate "add the tests" task
> alongside tasks that each carry their own.

> **Go-live runs 8 and 9, 2026-10-07**, after #88; 30 and 112 minutes. Both
> fixes held: every lens finished, and implementers kept working instead of
> reporting themselves blocked -- run 9 wrote code and tests across three tasks.
>
> *Run 8* (P3-18): an implementer adding one i18n key read the first page of a
> 483-line `en.json` -- a read stops at a character budget and says where to
> continue -- and wrote back what it had seen plus its key: 142 lines were left,
> in `sv.json` too, and the i18n test file went from 247 lines to 105. Thirty
> tests that pass on the baseline (889 of 889) failed, and the harness's
> full-suite criterion caught it. The implementer called them "pre-existing";
> the drift judge took its word and stopped the next implementer for fixing
> them. The toolbox had no way to change part of a file. The `test:e2e` task
> was escalated this time, its criteria unenforceable.
>
> *Run 9* (9a): one task was **verified 6/6 and did not do what it said**: "emit
> the serialisation note in the supervisor path", proven by six tests of helper
> functions in `timing.py`; the supervisor was never touched. It carried none of
> the harness's own checks: for three of five tasks the bars were judged before
> the task inherited the envelope's paths, so a title without a code word read
> as "does not touch code" -- no test bar, no `fails_before`, no review. Two
> other tasks failed on an end-to-end test the model had written and not made
> pass; two implementers were stopped for repeating themselves, which the
> existing signals now catch.
>
> Fixed after them: `edit_file`, which replaces one exact passage and keeps the
> file's line endings; `write_file` refuses to replace most of an existing file,
> and says to use it; and the bars are added after each task's scope is
> settled. Two more, from the pattern across runs 5-9 rather than one run:
> tasks whose scopes may meet are executed one at a time in the plan's order,
> so a task that builds on another starts on a tree with its code in it
> (`max_parallel_agents` had started every task at once, and implementers in
> three runs stalled on "peer must land X first"); and a failed test or command
> check is run again on the baseline commit, and its evidence says whether the
> failures are this run's -- which is what run 8's judge needed instead of the
> implementer's "pre-existing". Still open, and the strongest case yet for batch
> G: a test criterion is proven by running the test, and nothing checks that
> the test is about what the criterion says.

### Batch F — Does the model do what the harness asks?

An eval in the shape of `turnstone-eval`, aimed at the harness's own directives
rather than at tool use in general:

- small fixture workspaces;
- per case: the directive or brief, the expected behaviour, the *forbidden*
  behaviour (writing outside scope, claiming done with nothing read), several
  runs, and held-out cases;
- covering `refocus`, `deepen` (coverage), `narrow`, parallel dispatch, and
  `fails_before` fixing.

The output is a table of model × role × adherence. It says which roles a local
model can hold and which need a frontier one, which is the measured version of
"Hermes frustrates me, Claude Code doesn't".

It reads nothing from security-eval's benchmarks. Its output is what
security-eval's RQ4 needs from this side.

*Changes the harness condition:* no. It is a tool, and observe-only.

### Batch G — A reviewer that can only veto *(conditional)*

The adaptation of turnstone's judge: a review of each proposed task, by a
different model from the one that proposed it, against the original request.
Its output is a verdict from a fixed menu (`off_request`, `scope_unjustified`,
`criteria_cannot_fail`, `risk_understated`). A veto parks the task as an
escalation. It can never pass a task the gates refused.

Built only if batch E's runs show tasks reaching execution that a person would
have rejected. A judge that is never needed is cost and attack surface. Pairs
with nine-batch 9b, which is the other half of plan quality.

> **Built, 2026-10-07** (`core/review.py`), on the owner's go-ahead: the
> condition was met three times, by one task -- add the Playwright suite to
> `npm run check` -- that the gate passed and every check verified in runs 4,
> 6 and 15, though plantsandclimate's CI runs `npm run check` before it
> installs Playwright's browser. Under envelope approval, each task the gate
> passes is read by a reviewer with read-only tools, routed to the `review`
> stage, which rules `proceed` or vetoes from the menu above plus a fifth,
> `breaks_the_project` -- the measured case. A veto is a `review_veto`
> escalation; no ruling, a failed call or a review model without native tools
> is noted and the gate's decision stands. `policy.veto_review`, protected.
> "A different model" is a routing choice: with one local model, it is the
> same model in a different role. Probed on the local model against run 15's
> real tasks: the CI-breaking task vetoed in 35 seconds, citing `ci.yml` line
> 31 against line 40; the i18n task let through.

### Batch H — A stuck signal *(conditional)*

Turnstone's `RepeatDetector`: three identical tool calls in a row is the cheapest
reliable "stuck" signal there is. Added as a drift signal only if batch F, or a
real run, shows looping that the existing signals miss.

*Changes the harness condition:* **yes**. It is a drift signal, and report mode
is supervised. Flag it to security-eval before merging.

### Batch I — An implementer loop a local model can work in

*Added 2026-10-07, after go-live runs 4-9.* The owner's reading, which the
runs bear out: people run Turnstone on the same model to good results, so the
gap is the harness's. Read side by side, the two loops differ where the runs
failed -- in the long, tool-heavy work of an implementer, not in the
single-shot judgements of planning and analysis, which have held since #88.

What the harness did to an implementer that Turnstone does not:

1. **Forgot what it read at every turn.** After each supervised turn the
   conversation was reset to the brief, the agent's last answer and the
   directive; every tool result was dropped. Each turn began again with six
   tool rounds. Behind the read/search loops stopped for "100% repetition"
   (runs 7, 9), "tool rounds consumed by repeated identical calls" (run 5), and
   a translation file written back from the first page seen (run 8).
2. **Bypassed the model's tool calling.** Every step was one JSON object under a
   grammar, tool calls inside it; writing a file meant its whole contents as an
   escaped string. `qwen3.8-code` has native tool calling, and Turnstone uses it.
3. **Turned the model's thinking off** on every structured call.
4. **Sampled at 0.2** against the model's tuned 0.6; low-temperature decoding is
   a known cause of repetition in this model family. A likely contributor, not
   a proven one.
5. **Buried the task.** A measured implementer brief was 28,000 characters, of
   which "what to do" was 53: the run's shared context, every lesson, the other
   agents and a JSON output contract came first. NOOA's warning that context is
   eager, in one number.

The batch: on a provider that takes tools natively, with
`policy.implementer_loop = "conversation"`, an implementer is one conversation
(`core/conversation.py`). What it reads stays, compacted only near its context
limit. It acts through native tool calls with Turnstone-style workflow guidance
in the descriptions, reads files whole, and ends a stretch of work by calling
`report`. Supervision looks in at each report and every fifteen tool calls; the
directive is appended to the conversation, not swapped in for it. Thinking is
left on and the model's own sampling used. The opening message is the task and
little else (`build_implementer_brief`).

Unchanged: what an implementer may do -- the toolbox, its fences, the floor --
and how its work is checked. Lenses and verifiers keep the turn contract.

*Done when:* P3-18 and 9a, on the same local model, end with branches the
owner would merge. Measured against runs 4-9.

*Changes the harness condition:* no. Report mode spawns no implementer.

> **Go-live run 10, 2026-10-07** (P3-18, the conversation loop, 41 minutes).
> The first run whose work passes the project's own gate: on the run's branch
> `npm run check`'s typecheck, lint, 897 unit tests (the baseline's 889 and
> eight new) and build all pass. One task verified 7/7 -- the results list
> made per-plant, a shard that cannot be reached stranding only its own plants
> as "needs connection", offline told apart from other failures -- in the
> repository's own style, and every edit surgical: `en.json` gained one key and
> lost one it no longer used, where run 8 had cut it from 483 lines to 142.
> Of the rest, a task with correct keys and passing checks failed because its
> **verifier**, still on the turn contract, returned an empty answer on all
> three attempts -- the same defect one role along, so verifiers are now
> conversations that end in a `verdict` and see only the criteria still open.
> Three tasks went to the owner: one for a test filter that cannot be pinned,
> two because the plan's envelope left out `e2e/` and the CI workflow.

> **Runs 11 and 12** (9a, 49 minutes; P3-18, 17). The conversational verifier
> works: its review verdicts quote the code they judge, by file and line. Implementers
> now escalate with exact, correct accounts -- "Scope guard refuses to let me
> edit src/supervisor_harness/core/reporting.py, which is the one the task
> requires" -- and baseline attribution read a failure right. What stopped them
> was the harness: the plan's envelope named `reporting/`, a directory that does
> not exist; a task titled "... in reporting.py" was not read as naming a file;
> a finished task's full suite failed on a test the task after it had written,
> because tasks were checked only at the end; and in run 12 five of six tasks
> went to the owner for `command` criteria whose command was in the sentence --
> "npm run typecheck passes with the new data-layer types".
>
> After them, on the owner's decision: a task's scope widens within the owner's
> grant at proposal, and at the moment of a write the task needs it widens
> within the run's envelope (after run 16, below), where no running peer's
> scope could meet the path -- and only what is not covered goes to the owner. And: bare file names are read when one file in the
> tree has the name; a finished task is checked before the next writer starts;
> and a command named in a criterion's statement is its command.

> **Run 15** (P3-18, 64 minutes). Three of four tasks verified, and the branch
> passes every check the project has: typecheck, lint, 902 unit tests (889 on
> the baseline), the build, all 25 Playwright journeys -- among them the new
> one, "a saved garden works offline: cached plants keep their verdict, the
> uncached say needs connection" -- and `scripts/verify.ps1`. Two things stood
> between it and a merge: a scratch spec its implementer could not delete
> (there was no tool; `delete_file` now), and the `npm run check` change again
> -- which batch G now vetoes.

> **Run 16** (P3-18, 1h53m, with `delete_file` and G). The branch passes every
> check the project has -- 892 unit tests, all 25 Playwright journeys among
> them a new offline one, `verify.ps1` -- and its scratch files were deleted.
> G vetoed the CI-breaking task, citing `ci.yml` line 31. Yet the harness
> verified none of six tasks and failed the run, for four reasons of its own:
>
> - every inspection criterion came with no `expect`; that was a medium
>   warning, so nothing sent it back, and verification blocked it three times
>   per task. Now it is unenforceable (sent back), and one kept through the
>   send-back becomes a mandatory review that must cite the lines;
> - the phase machine's guard counted every step and stopped the run during
>   its last verification. Now it counts steps that recorded nothing;
> - 147 `edit_file` calls missed: `read_file` put two spaces after the line
>   number, so a copied line's indentation was a guess, and the model wrote
>   twenty `scripts/tmp-*.mjs` patch scripts instead. Now a tab, as `cat -n`
>   has it, and an edit off only in indentation lands, re-indented, when it
>   matches one place;
> - every task's scope was the whole plan, so a peer made the vetoed change.
>   Now every implementer's brief lists the changes held for the owner, and no
>   scope widens into a task held for the owner. And at a write a scope widens
>   only within the run's envelope: the grant from `--grant-envelope` is the
>   whole workspace, which is how the twenty scripts got in.

> **Run 17** (P3-18, 1h32m, after #90). No agent stopped (five in run 16), no
> edit missed, and the veto held: G vetoed the e2e-into-`check` task again,
> citing the project's own definition of the green gate, and `package.json`
> was left alone. The branch passes `npm run check` (901 unit tests),
> `verify.ps1` and all 25 Playwright journeys, the new offline one among them.
> One of six tasks verified, for a bug of batch I's own: a verifier's two
> stretches shared one tool-call count, so the second ended before the model
> was called, and "call verdict now" was never sent -- twelve of thirteen
> verifiers were cut off mid-reading, and the reviewer's frequent "no ruling"
> had the same cause. Now each stretch counts its own calls, and after the
> reading stretches comes one in which `verdict` (or `ruling`) is the only
> tool. And one inspection named `src/styles/tokens.css` for the project's
> `src/app/styles/tokens.css`: where the named directory does not exist and
> one file in the task's scope has the name, the criterion checks that file.

---

## The gate between the parts

Part 2 starts only when batches B to E are merged and envelope-mode runs have
been trusted on the owner's own repositories. A loop multiplies whatever a
single run does. If a single run is not yet trusted overnight, a loop of them is
less so.

## Part 2: the outer loop

**A new, thin repository, not this one.** It depends on the harness through the
published CLI, the same way security-eval does. The primer's argument for
keeping it separate: the loop is "the same harness, one level up", with its own
memory (the backlog), its own gate (which work it may pick), and its own
cadence. Keeping it outside keeps the harness's guarantees about one run, such
as replay from the log, about one run.

### L1 — One task, on demand, end to end

Take one item (a local backlog file, or a GitHub issue with a label). Run the
harness in envelope mode, on its own branch. Open a **draft** PR, only in
repositories where the owner has granted that in the loop's own config. Write a
run record. Started by hand, never on a schedule yet.

### L2 — The owner's inbox

A generated, offline page: open escalations, what each run did and cost, the
`fails_before` evidence, links to PRs. Answering an escalation calls the
harness's resolve command; the page holds no decision state. Optionally, the
same inbox as PR comments for repositories already on GitHub.

### L3 — A schedule, and resets

Runs on a schedule, with a spend ceiling per night. The envelope's maximum age
(already a policy) is how stale consent is refused. The loop stops rather than
guesses when an escalation it depends on is unanswered. **It never merges.** The
primer's point about daemons applies: safety that is fine per cycle decays over
many cycles, and scheduled re-confirmation is the counter to that.

---

## Decided against

**Smart Approvals: an LLM approving on confidence.** Turnstone's opt-in mode
auto-approves a batch when every call has a confident LLM "approve". It is
batch-atomic and keeps a deterministic deny floor, so it is carefully built. But
it lets a model's verdict widen what happens, and the primer's own rule is that
a judge "that can approve is a tricked judge that can open the vault". Here the
deterministic gates and the envelope decide, and a model may only veto
(batch G).

**A judge on every tool call as the default.** It is the right design for an
interactive chat agent with a shell. Here the fence on tools is deterministic
code (`core/tools.py`), supervision happens per turn, and a model call per tool
call is cost with no matching gap.

**The in-run prompt optimizer** (`turnstone-optimizer`, a UCB search that edits
prompts and tool descriptions until tests pass). Inside a run, it is an agent
setting the terms of its own judgement, which the nine-batch plan already
declined for NOOA's self-extending agents. Offline, against batch F's eval and
its held-out cases, it is legitimate tooling and may be revisited there.

**The platform**: cluster routing, RBAC, SSO, chat channels, a server. The owner
is one person on one machine, and some runs' data may not leave it.

## Questions for the owner

Each has a recommendation; none blocks batches A or B.

1. **Approval mode is a protected setting** (batch E). Recommended: yes. A
   workspace that could set `approval: envelope` would be choosing to skip review
   of work done on itself.
2. **Envelope mode requires harness-run commands** (batch E). Recommended: yes.
   The alternative is autonomy checked only by models.
3. **The outer loop is a new repository**, started only after the gate.
   Recommended: yes.
4. **The inbox is offline first, GitHub optional** (L2). Recommended: yes.
5. **The group's actual comments on the results.** Please pass them on. This
   plan treats "underperforming against the attacker proxies" as a question for
   batch F and RQ4, not as a known defect, and their specifics could change that.
