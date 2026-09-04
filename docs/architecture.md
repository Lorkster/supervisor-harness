# How a run works, drawn

Four pictures the prose elsewhere describes but does not show: the phase
machine including the paths that are not the happy one, what is written where
and what survives a crash, where the two backends diverge, and how the fence
narrows as authority is handed down.

Drawn in ASCII rather than a diagram format, deliberately: these are read in an
editor and a terminal as often as on a page, and a picture that renders in one
place and not the others is worse than a plain one that renders everywhere.

- [The phase machine](#the-phase-machine)
- [What is written where](#what-is-written-where)
- [Where the two backends diverge](#where-the-two-backends-diverge)
- [The fence](#the-fence)

---

## The phase machine

Every arrow is a `PHASE_CHANGED` event on the run's log, so a run resumes at
whatever phase it had reached, in another process or a later session.

```
  supervisor start
        │
        ▼
   ┌─────────┐
   │ created │
   └────┬────┘
        ▼
   ┌───────────┐   analysis lenses, in parallel. After every turn: score the
   │ analyzing │   drift, issue a directive, route the messages. A lens that
   └────┬──────┘   goes silent is abandoned; the phase settles without it.
        ▼
  ┌──────────────┐
  │ synthesizing │────────────────────────────┐  report mode, or no tasks
  └──────┬───────┘                            │  proposed at all
         │ execute mode, tasks proposed       │
         ▼                                    │
  ┌───────────────────┐                       │
  │ awaiting_approval │───▶ envelope stale?   │
  └─────────┬─────────┘     wait here for     │
            │               --renew-envelope  │
            │ you approve (per task)          │
            ▼                                 │
      ┌───────────┐                           │
      │ executing │◀────────────────┐         │
      └─────┬─────┘                 │         │
            ▼                       │         │
      ┌───────────┐                 │ remediation: the tasks that fell
      │ verifying │                 │ short are reopened, carrying the
      └─────┬─────┘                 │ checkpoint's corrections into
            ▼                       │ their action
      ┌────────────┐   not passed   │
      │ checkpoint │────────────────┘
      └─────┬──────┘
            │ passed — or the remediation budget is spent, or nothing
            │ actionable came back
            ▼                                 │
      ┌───────────┐◀──────────────────────────┘
      │ improving │   lessons, the final report, the reconciliation
      └─────┬─────┘
            ▼
      ┌──────────┐          ┌────────┐  an error the run cannot continue
      │ complete │          │ failed │  past: a stage routed somewhere this
      └──────────┘          └────────┘  backend cannot reach, a phase that
                                        will not settle
```

### The bounds on the loops

Nothing here loops without a counter, and each one is a policy setting:

| Loop | Bounded by | Default | When it runs out |
| --- | --- | --- | --- |
| An agent's turns | `default_max_turns`, `execution_max_turns` | 6, 10 | The agent is stopped and settles; its work so far is kept |
| An agent's silence | `max_unreported_dispatches`, `agent_timeout_seconds` | 3 packets, off | The agent is abandoned, on the log, and the phase settles without it |
| A task's attempts | `max_task_attempts` | 3 | The task is not reopened again; it ends `failed`, with its unmet criteria named |
| Remediation rounds | `max_checkpoint_iterations` | 3 | The run proceeds to `improving` and reports what did not pass |

**So: what happens when a task fails verification twice?** Its criteria are
verified mechanically where they can be, and the verdict of a check the harness
ran itself outranks the verifying agent's account of it. A task whose mandatory
criteria are not all proven is marked `failed` when the phase settles. The
checkpoint then judges the round; if it does not pass, `_remediate` reopens
every failed task whose `attempts` are still under `max_task_attempts`, appends
the checkpoint's own corrections to the task's action, and the run goes back to
`executing`. That is the second attempt. The same happens after it fails again —
until either the checkpoint passes, the task reaches three attempts, or the run
has spent three remediation rounds. Then the run moves on and *says so*: the
task is `failed` in the report, its unmet criteria are listed, and the
reconciliation names the finding it was meant to close as still open. Nothing
is quietly dropped, and nothing is called done.

---

## What is written where

The event log is the only thing that is not derived. Everything else can be
rebuilt from it, and is.

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
 read only when    rebuilt by `supervisor reindex`; a failure to write it
 last_seq >= the   is noted on the log and the run carries on
 log's own tail
```

| File | Lost or damaged means | Recovered by |
| --- | --- | --- |
| `events.jsonl` | the run is gone; this is the record | nothing — which is why it is append-only, locked and fsynced |
| a line inside it | that event is gone; the rest still replays | counted in `damaged_lines` and reported by `status`, never skipped in silence |
| `state.json` | one fold's worth of time | rebuilt from the log on the next read |
| `index.sqlite3` | cross-run queries until it is rebuilt | `supervisor reindex`, which also prunes rows for runs that no longer exist |
| `lessons.jsonl` | what earlier runs taught | nothing; it is the one store not derived from a single run's log |

**So: what is written where, and what survives a crash?** Everything a run
decided is in `events.jsonl` before it is anywhere else, written under an
advisory lock and fsynced, one batch per turn. A crash costs whatever had not
been emitted yet — never something that had. The snapshot beside it is a cache
that records the sequence number it was folded up to, so a snapshot written by
a process that died mid-run is *behind* rather than wrong, and the next read
notices and refolds. The SQLite index is a convenience for asking questions
across runs; losing it costs the answers until `reindex`, not the run.

---

## Where the two backends diverge

Both backends run the same supervision path. The difference is who calls the
model — and in host-delegated mode the harness is not in the model path at all.

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
| Tools | the host's, under your permission model | `list_files`, `read_file`, `search`; `write_file` for execution agents; `run_command` only if `policy.allow_command_execution` |
| A second opinion on drift | skipped when the `drift` stage is itself routed to `host` — there is no model call to make | made whenever the heuristics fire |
| An agent that goes silent | abandoned after `max_unreported_dispatches` packets: silence is all there is to go on | cannot happen — it answers, raises, or runs out of turns |

---

## The fence

Four ceilings, each narrower than the one above it. Authority only ever
narrows, and every narrowing is recorded.

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

---

## Where to read next

| | |
| --- | --- |
| [`reasoning-control-plane.md`](reasoning-control-plane.md) | **What the harness is**, and why each of these bounds exists |
| [`protocol.md`](protocol.md) | The wire protocol: packets, responses, and the loop a host drives |
| [`../README.md`](../README.md) | Installing it, running it, and every command and setting |
