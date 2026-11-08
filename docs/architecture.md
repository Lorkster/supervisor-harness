# How a run works, drawn

Four pictures the prose elsewhere describes but does not show: the phase
machine including the paths that are not the happy one, what is written where
and what survives a crash, where the two backends diverge, and how the fence
narrows as authority is handed down.

Drawn in ASCII rather than a diagram format, deliberately: these are read in an
editor and a terminal as often as on a page, and a picture that renders in one
place and not the others is worse than a plain one that renders everywhere.
Each also has a rendered version for the web, in [`images/`](images/), drawn by
`tools/draw_diagrams.py`. The ASCII stays the reference: change it first, then the
function that draws its picture, and run the tool again.

- [The phase machine](#the-phase-machine)
- [What is written where](#what-is-written-where)
- [Where the two backends diverge](#where-the-two-backends-diverge)
- [The fence](#the-fence)

*Last verified on 2026-11-08 against the working tree of branch
`review/code-and-docs-2026-11` (the commit is still `8f34f4e`): the phases and
every move between them against `Phase` and the `_transition` calls in
[`core/supervisor.py`](../src/supervisor_harness/core/supervisor.py), the bounds
against `Policy`, the toolbox against `available_tools`.*

---

## The phase machine

Every arrow is a `PHASE_CHANGED` event on the run's log, except the two into
`complete` and `failed`, which are written by `RUN_ENDED`. Either way a run
resumes at whatever phase it had reached, in another process or a later session.

Read the rail on the left as one path with several doors: the run reaches
`improving` from `synthesizing` (report mode, or no tasks), from
`awaiting_approval` (nothing approved), from `checkpoint` (nothing open) or from
`awaiting_owner` (everything answered, nothing granted). The rail on the right is
the way back to `executing`, from `checkpoint` (remediation) and from
`awaiting_owner` (a grant). `awaiting_owner` is entered only when an escalation
is actually open; with none, the run goes from `checkpoint` straight to
`improving` and the phase never appears in its log.

Rendered: [`images/run-lifecycle.svg`](images/run-lifecycle.svg).

```
        supervisor start
                │
                ▼
           ┌─────────┐
           │ created │
           └────┬────┘
                ▼
          ┌───────────┐  analysis lenses, in parallel; drift scored
          │ analyzing │  after every turn. A lens that goes silent is
          └─────┬─────┘  abandoned, and the phase settles without it
                ▼
         ┌──────────────┐
         │ synthesizing │─ report mode, or no tasks proposed ───────────┐
         └──────┬───────┘                                               │
                │ execute mode, tasks proposed                          │
                ▼                                                       │
      ┌───────────────────┐                                             │
      │ awaiting_approval │─ nothing approved ──────────────────────────┤
      └─────────┬─────────┘                                             │
                │ you approve (per task) -- or, under an envelope       │
                │ grant, the harness's gate does. What it refuses       │
                │ is parked for the owner; if it parks every task       │
                │ the run goes to awaiting_owner, not executing         │
                ▼                                                       │
          ┌───────────┐                                                 │
          │ executing │◀────────────────────────────┐                   │
          └─────┬─────┘  stale envelope: waits here │                   │
                ▼                                   │                   │
          ┌───────────┐                             │                   │
          │ verifying │                             │                   │
          └─────┬─────┘  may return to executing    │                   │
                ▼                                   │                   │
          ┌────────────┐                            │                   │
          │ checkpoint │─ not passed: remediation ──┤                   │
          └─────┬──────┘                            │                   │
                ├─ passed, none open ───────────────┼───────────────────┤
                │ passed, or the budget is          │                   │
                │ spent, or nothing actionable,     │                   │
                │ with an escalation open           │                   │
                ▼                                   │                   │
        ┌────────────────┐                          │                   │
        │ awaiting_owner │─ a grant ────────────────┘                   │
        └───────┬────────┘  (to verifying, if a held check is next)     │
                │ every escalation answered, nothing granted            │
                ▼                                                       │
          ┌───────────┐                                                 │
          │ improving │◀────────────────────────────────────────────────┘
          └─────┬─────┘
                ▼
           ┌──────────┐           ┌────────┐
           │ complete │           │ failed │  an error the run cannot continue past,
           └──────────┘           └────────┘  from any phase
```

### Every move between phases

The drawing shows the paths; this is the whole list, as the code emits them.
`failed` is reachable from any phase, through `Reporting._error`, and is left
out of the table.

| From | To | When | Where |
| --- | --- | --- | --- |
| `created` | `analyzing` | the plan landed, or planning was abandoned and the derived lenses stand in | `_apply_plan`, `_begin_planning` |
| `analyzing` | `synthesizing` | every lens has settled | `_continue_analysis` |
| `synthesizing` | `awaiting_approval` | execute mode (or `auto`, recommended to execute) and tasks were proposed | `_apply_synthesis` |
| `synthesizing` | `improving` | report mode, or no tasks proposed | `_apply_synthesis` |
| `awaiting_approval` | `executing` | something was approved: by `supervisor_approve`, or by the envelope gate | `approve`, `_approve_within_envelope` |
| `awaiting_approval` | `awaiting_owner` | under an envelope grant, the gate (or the veto reviewer) parked every task | `_approve_within_envelope` |
| `awaiting_approval` | `improving` | nothing approved and nothing parked: every task rejected or deferred | `approve`, `_approve_within_envelope` |
| `executing` | `verifying` | no task can start and none is running | `_continue_execution` |
| `verifying` | `executing` | a task that waited on one just verified can start now | `_continue_verification` |
| `verifying` | `checkpoint` | nothing more can start | `_continue_verification` |
| `checkpoint` | `executing` | remediation: failed tasks under `max_task_attempts` are reopened; or a task whose dependency failed for good is released | `_apply_checkpoint`, `_leave_or_release` |
| `checkpoint` | `awaiting_owner` | passed, or the remediation budget is spent, or nothing actionable came back -- and an escalation is open | `_leave_execution` |
| `checkpoint` | `improving` | the same three, and none is open | `_leave_execution` |
| `awaiting_owner` | `executing` | every escalation is answered and a granted task is runnable again | `_await_owner` |
| `awaiting_owner` | `verifying` | answered, nothing to run, but a check held for an answered task is waiting | `_await_owner` |
| `awaiting_owner` | `improving` | answered, nothing to run: declined tasks are deferred and reported | `_await_owner` |
| `improving` | `complete` | lessons, the final report and the reconciliation are written | `Reporting._complete` |

One thing happens in a phase without leaving it. A run resumed past
`policy.envelope_max_age_days` stops in `executing`, before it starts a new
execution agent, and answers `await_approval` with `needs: "envelope_renewal"`
until `supervisor approve --renew-envelope` re-grants the date (never the
paths). The `phase` it reports stays `executing`.

### The bounds on the loops

Nothing here loops without a counter, and each one is a policy setting:

| Loop | Bounded by | Default | When it runs out |
| --- | --- | --- | --- |
| An agent's turns | `default_max_turns`, `execution_max_turns` | 6, 10 | The agent is stopped and settles; its work so far is kept |
| An agent's silence | `max_unreported_dispatches`, `agent_timeout_seconds` | 3 packets, off | The agent is abandoned, on the log, and the phase settles without it |
| A task's attempts | `max_task_attempts` | 3 | The task is not reopened again; it ends `failed`, with its unmet criteria named |
| Coordinator consultations | `max_coordinator_decisions` | 24 | A failed task is followed up as itself while its attempts last; a stopped one goes to the owner |
| Remediation rounds | `max_checkpoint_iterations` | 3 | The run proceeds to `improving` and reports what did not pass |
| Escalation rounds | the owner | — | Not a counter, and the one exception: each round ends at `awaiting_owner`, so the loop goes round only as often as a person answers `grant` |

**So: what happens when a task fails verification twice?** Its criteria are
verified mechanically where they can be, and the verdict of a check the harness
ran itself outranks the verifying agent's account of it. A task whose mandatory
criteria are not all proven is marked `failed` when the phase settles. Then
the coordinator (`core/coordinator.py`) is shown what came back -- each failed
check's output, the files it names and which tasks' agents wrote them, any agent
that stopped, any finding about another task's work -- and picks from a fixed
menu: send the task back to its own implementer with a message, move a check to
the task whose work it fails on, make a task wait for another, judge a stopped
task as it stands, ask the owner, or leave it failed. The harness refuses a pick
it cannot carry out, with the reason, once; the coordinator can pass no check,
drop none, and widen nothing; a task it leaves failed is not reopened. The
checkpoint then judges the round; if it does not pass, `_remediate` reopens
every other failed task whose `attempts` are still under `max_task_attempts`,
adds the checkpoint's own corrections to what the task is told (`told`, which the
implementer reads and the verifier does not: the verifier judges the planned
`action`, so what the run said to a task cannot become part of what is checked),
and the run goes back to `executing`. That is the second attempt. The same
happens after it fails again — until either the checkpoint passes, the task
reaches three attempts, or the run has spent three remediation rounds (the
defaults of `max_task_attempts` and `max_checkpoint_iterations`). Then the run
moves on and *says so*: the
task is `failed` in the report, its unmet criteria are listed, and the
reconciliation names the finding it was meant to close as still open. Nothing
is quietly dropped, and nothing is called done.

---

## What is written where

The event log is the only thing that is not derived. Everything else can be
rebuilt from it, and is.

Rendered: [`images/event-log.svg`](images/event-log.svg).

```
   something happens
   (a turn, a directive, a
    decision, a verdict)
          │
          │  emit / aemit  — one advisory lock, one fsync, one batch per turn
          ▼
  ┌─────────────────────────┐
  │ runs/<id>/events.jsonl  │   append-only, ordered by seq, never rewritten
  │        AUTHORITATIVE    │   credential-shaped strings redacted on the way in
  └───────────┬─────────────┘
              │
              │  fold(events) — replay in seq order, one event at a time
              ▼
         ┌──────────┐
         │ RunState │  the run as it stands: agents, tasks, findings, turns,
         └────┬─────┘  facts, directives, and its own damage signals
              │
      ┌───────┴────────┐
      ▼                ▼
┌──────────────┐  ┌──────────────────┐
│ state.json   │  │ index.sqlite3    │  one row per run, agent, task,
│ a cache      │  │ a projection     │  criterion, finding, message, event
│ + last_seq   │  │                  │
└──────────────┘  └──────────────────┘
 written every 100  rebuilt by `supervisor reindex`; a failure to write it
 events and at     is noted on the log and the run carries on
 phase edges; a
 read folds the
 tail on top
```

| File | Lost or damaged means | Recovered by |
| --- | --- | --- |
| `events.jsonl` | the run is gone; this is the record | nothing — which is why it is append-only, locked and fsynced |
| a line inside it | that event is gone; the rest still replays | counted in `damaged_lines` and reported by `status`, never skipped in silence. A multibyte character torn by a crashed write is counted too, and does not take the rest of the log with it |
| `state.json` | the cost of a fold from the start | rebuilt from the log on the next read. It is written compact, at `RUN_CREATED`, every `PHASE_CHANGED`, `RUN_ENDED`, when a session closes, and otherwise every 100 events (`SNAPSHOT_EVERY`), so it can lag the log by up to about a hundred events; `load_state` and `RunStore.open` fold the events after its `last_seq` on top of it. A session that finds another writer's events in the log refolds them before it snapshots, so a snapshot never claims a sequence it did not fold |
| `index.sqlite3` | cross-run queries until it is rebuilt | `supervisor reindex`, which also prunes rows for runs that no longer exist. It is kept current incrementally: only runs whose log moved are touched, and only their new events written |
| `runs/<id>/worktree/` | an autonomous execute-mode run's working copy, on the branch `supervisor/<id>`; removed once its changes are committed | the branch, which holds the commit; if the commit failed, the worktree is kept and the run's notes say why |
| `progress.ndjson` | the line `tail -f` follows while a run is going; a failure to write it is isolated | not recovered; `events.jsonl` has every event |
| `packets/`, `contracts/`, `results/` | the briefs, answer schemas and answers a host was handed and wrote, by path. Only events are redacted: these files are not | the briefs are on the log (`BRIEF_RENDERED`); an answer reported is on the log as a turn |
| `artifacts/` | `report.md`, `reconciliation.md` and what an agent produced | the two reports are rewritten from state when the run ends |
| `lessons.jsonl` | what earlier runs taught | nothing; it is the one store not derived from a single run's log. Read by splitting on newlines only (a lesson holding U+2028 is one line), and rewritten through a unique temporary file, fsynced and renamed. Confidence decays by one flat step (`lesson_decay_per_run`) for each run that passes unconfirmed once the grace period is over |

**So: what is written where, and what survives a crash?** Everything a run
decided is in `events.jsonl` before it is anywhere else, written under an
advisory lock and fsynced, one batch per turn. A crash costs whatever had not
been emitted yet — never something that had. The snapshot beside it is a cache
that records the sequence number it was folded up to, so a snapshot written by
a process that died mid-run is *behind* rather than wrong, and the next read
notices and folds the events after it. The SQLite index is a convenience for asking questions
across runs; losing it costs the answers until `reindex`, not the run. Every id
the harness mints is time-ordered and, within one process,
strictly increasing, so a directory listing and a sort by id are both
chronological.

---

## Where the two backends diverge

Both backends run the same supervision path. The difference is who calls the
model — and in host-delegated mode the harness is not in the model path at all.

Rendered: [`images/backends.svg`](images/backends.svg).

```
        HOST-DELEGATED (default)              AUTONOMOUS
        ────────────────────────              ──────────

        supervisor_start                      supervisor run
              │                                     │
              ▼                                     ▼
        ┌───────────┐                         ┌───────────┐
        │  harness  │ writes the brief        │  harness  │ writes the brief
        └─────┬─────┘                         └─────┬─────┘
              │ work packet                         │ CompletionRequest
              ▼                                     ▼
        ┌───────────────┐                     ┌──────────────┐
        │ Claude Code / │ its own tools,      │  provider    │ openrouter,
        │    Cursor     │ its own permission  │              │ ollama,
        └─────┬─────────┘ model               └──────┬───────┘ anthropic,
              │                                      │          bedrock
              │ supervisor_report                    │ tool rounds against
              ▼                                      ▼ the sandboxed toolset
        ┌───────────────────────────────────────────────────┐
        │  record the turn · assess drift · issue a          │
        │  directive · route the messages · verify           │
        └───────────────────────────────────────────────────┘
                        the same code, both sides
```

Three differences follow from the backend rather than being oversights:

| | Host-delegated | Autonomous |
| --- | --- | --- |
| Tools | the host's, under your permission model | `list_files`, `read_file`, `search`; `edit_file`, `write_file` and `delete_file` for execution agents; `run_command` only if `policy.allow_command_execution`. In the conversation loops an agent also gets `report` (an implementer) or `verdict` (a verifier), `message`, and `find_tool` / `use_tool` for the owner's tool servers |
| A second opinion on drift | skipped when the `drift` stage is itself routed to `host` — there is no model call to make | made whenever the heuristics fire |
| An agent that goes silent | abandoned after `max_unreported_dispatches` packets: silence is all there is to go on | cannot happen — it answers, raises, or runs out of turns |

---

## The fence

Four ceilings, each narrower than the one above it. Authority only ever
narrows, and every narrowing is recorded.

Rendered: [`images/fence.svg`](images/fence.svg).

```
  ┌────────────────────────────────────────────────────────────────┐
  │ THE WORKSPACE                                                  │
  │ one run, one workspace root: every glob, the tool fence and    │
  │ the baseline commit are relative to it                         │
  │                                                                │
  │   ┌─────────────────────────────────────────────────────────┐  │
  │   │ THE RUN ENVELOPE            policy.scope_envelope, then  │  │
  │   │ granted once, before any task exists. The plan may       │  │
  │   │ narrow it. Nothing widens it — not approval, not a task. │  │
  │   │ It carries a date, and a stale grant is re-asked.        │  │
  │   │                                                          │  │
  │   │   ┌───────────────────────────────────────────────────┐  │  │
  │   │   │ THE TASK'S SCOPE                                  │  │  │
  │   │   │ proposed by the synthesis model, attenuated to    │  │  │
  │   │   │ the envelope. Editing it at approval clamps it    │  │  │
  │   │   │ the same way.                                     │  │  │
  │   │   │                                                   │  │  │
  │   │   │   ┌────────────────────────────────────────────┐  │  │  │
  │   │   │   │ THE AGENT'S SCOPE                          │  │  │  │
  │   │   │   │ attenuated at spawn to the envelope, to    │  │  │  │
  │   │   │   │ its task's scope, and to its spawner's —   │  │  │  │
  │   │   │   │ so a verifier is never handed a wider      │  │  │  │
  │   │   │   │ fence than the work it is judging.         │  │  │  │
  │   │   │   └────────────────────────────────────────────┘  │  │  │
  │   │   └───────────────────────────────────────────────────┘  │  │
  │   └─────────────────────────────────────────────────────────┘  │
  └────────────────────────────────────────────────────────────────┘

  ═══════════════════════════════════════════════════════════════════
   THE FLOOR — under all four, and not narrowed by anything above it
   .git .hg .svn (a hook is code the tool runs) and the harness's own
   store (the log every claim in the run is judged against). No agent
   writes there, whatever its scope says, in either backend.
  ═══════════════════════════════════════════════════════════════════
```

A scope that exceeds its ceiling is **narrowed to the intersection, not
refused**: a model proposing too much is ordinary, and losing the task over it
is not. Which ceiling bit is recorded on the log, shown in the notes you read at
approval, and reported by `supervisor status`.

The command fence is universal — every agent gets it, whether or not it declared
a scope. An empty scope relaxes the per-path check alone, and relaxes it to the
workspace rather than to the machine.

Two lists inside a scope narrow what it reaches without being a ceiling of their
own. `forbidden_paths` (from the envelope's `scope_envelope_forbidden` and from a
plan's "do not modify" documents) bars writes, and constrains only an agent that
can write: a lens reading such a document is not breaking anything.
`unread_paths` is what an agent's read tools will not show it, set for a task
that tests another task's work so that its tests come from the request's words
and from running the result, not from the code. It is not a security boundary:
the tests that tester writes run that code.

---

## Where to read next

| | |
| --- | --- |
| [`reasoning-control-plane.md`](reasoning-control-plane.md) | **What the harness is**, and why each of these bounds exists |
| [`protocol.md`](protocol.md) | The wire protocol: packets, responses, and the loop a host drives |
| [`../README.md`](../README.md) | Installing it, running it, and every command and setting |
