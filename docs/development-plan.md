# The development plan: nine batches

*Written 2026-09-07. This one is live — the work in it is scheduled, not done.*

> **Progress.** Batch 1 is merged ([#53](https://github.com/Lorkster/supervisor-harness/pull/53)).
> Batch 2 is open. Nothing below has been edited to match what happened;
> where a batch measured something the plan only estimated, the measurement
> is added beneath it and the estimate is left standing.

A working document in the same shape as the closed
[`history/development-plan.md`](history/development-plan.md): what the next
pieces of work are, what has been *verified* about each, what is still only a
reading of the source, and what has been decided. It is written in the present
tense of the day it was written and will be left that way; when every batch is
done it moves to `history/` with a banner rather than being edited into
agreement with what happened.

Two things produced it.

**Three observations from a real run** — the harness driven from Claude Code
against Sonnet 4.6, on a minor refactoring task. It was slow, it was opaque
while being slow, it burned enough context to force three compactions where the
same user's ordinary skills-and-subagents workflow forces none, and not one of
the workspace's own agent definitions was used, including on a lens whose whole
subject is security. Batches 1–3 are those three observations. They come first
because everything after them is diluted by them.

**A reading of [NVIDIA-NeMo/labs-OO-Agents](https://github.com/NVIDIA-NeMo/labs-OO-Agents)**
(NOOA) — a model-agnostic Python agent framework with a different thesis from
this one: an agent is a Python object, and the framework's job is to keep
judgement inside selected methods while Python retains program structure. It is
not a supervisor and does not try to be. But several of the mechanisms it built
for its own purposes answer questions this harness has open, and batches 4–9 are
those, adapted. Each says what idea is taken and where the mechanism is
deliberately *not* copied — a port of someone else's abstraction into a codebase
with a different thesis is how a control plane stops being one.

- [The three observations](#the-three-observations)
- [Batch 1 — Bind local agents](#batch-1--bind-local-agents)
- [Batch 2 — Packets by reference](#batch-2--packets-by-reference)
- [Batch 3 — The run ledger and stage timing](#batch-3--the-run-ledger-and-stage-timing)
- [Batch 4 — Trajectory export](#batch-4--trajectory-export)
- [Batch 5 — Facts that know whether they are still true](#batch-5--facts-that-know-whether-they-are-still-true)
- [Batch 6 — Lessons that decay and consolidate](#batch-6--lessons-that-decay-and-consolidate)
- [Batch 7 — `supervisor audit`](#batch-7--supervisor-audit)
- [Batch 8 — Enforce versus observe](#batch-8--enforce-versus-observe)
- [Batch 9 — Reactive supervision](#batch-9--reactive-supervision)
- [Woven in rather than batched](#woven-in-rather-than-batched)
- [Decided against](#decided-against)

---

## The three observations

### 1. No local agent was ever used

**Verified by reading, not by reproduction.** Discovery works:
[`agents/registry.py`](../src/supervisor_harness/agents/registry.py) already
scans `<workspace>/.claude/agents` and `~/.claude/agents`, so the Tester and
Security Specialist definitions *were* found. Matching works: `match()` falls
back to `file_agents` and the substring pass finds `security` inside "Security
Specialist".

Then the result is discarded. Five sites in
[`core/phases.py`](../src/supervisor_harness/core/phases.py) read

```python
host_agent_type=match.name if match and match.spawnable else None
```

and `spawnable` is `self.source == "host"`. A file-sourced agent is never
spawnable, so the match is computed and thrown away on every packet. The
docstring calls file agents "hints" — but nothing anywhere carries a hint to the
host, so the distinction has no consumer and never had one.

**The premise is wrong for the host it matters most for.** Agents defined in
`.claude/agents/*.md` *are* spawnable by name via `subagent_type`; they are not
hints, they are exactly the spawnable set. Cursor modes are the case that
motivated the caution, and Claude Code got caught by it.

Two smaller defects behind the same wall. The MCP `INSTRUCTIONS` block — which
is what a host actually steers by — never mentions `host_agents`, though the
`supervisor_start` tool description does, so the declaration arrives `None` and
the run falls through to the files that are then discarded. And hint lookup is
an exact `by_name` match, so `host_agent_hints=["security", ...]` can never
match an agent *named* "Security Specialist"; it works only by accident, through
the looser pass beneath it.

### 2. Context use is extremely high

**Not a defect — an architectural inversion**, and the right diagnosis of why
the harness loses so badly to a skills-and-subagents setup on this axis.

A native subagent is a *context firewall*: it burns its own window and the
caller pays only for the final report. This harness makes the orchestrator the
message bus. Every brief goes out through the orchestrator's context, every
structured result comes back through it, every directive goes out again. Nothing
is firewalled, because the supervisor has to see everything in order to
supervise it.

On top of that the per-packet payload is heavy. Measured:

| Fixed text in every analysis brief | chars |
| --- | --- |
| `ANALYSIS_TURN_SCHEMA`, `json.dumps(indent=2)` | 5,012 |
| `SHARED_TREE_RULE` | 1,106 |
| the baseline block | ~1,200 |
| tools section and core rules | ~1,700 |

≈ 9,000 characters — roughly 2,300 tokens — of *fixed* text per brief, before
the task, the shared context or the lessons. A minor refactor is one planning
packet, three or four lenses at up to six turns each, a synthesis packet, one to
three execution tasks at up to ten turns each, a verification packet per task, a
checkpoint and a lessons packet: 25–40 round trips, each carrying a brief out
and a JSON result back through one context. Three compactions is the arithmetic
working correctly.

The recoverable part is that the *supervisor* needs to see everything; the
*orchestrator's context* does not. That is batch 2.

### 3. Slow, and invisible while slow

Two separate things, and only one of them is fixable cheaply.

The slowness is structural: 25–40 sequential subagent invocations where the
user's ordinary workflow is three to five. Batch 2 helps and lowering
`default_max_turns` helps more, but a supervised run is genuinely more work.
The honest framing — which belongs in the README, not only here — is that this
is for tasks where being right matters more than being quick.

The invisibility is a gap. The harness only wakes when it is called, so it
cannot push. But two things are missing that cost nothing:

- **No timing is recorded anywhere.** There is no per-stage wall clock, so "it
  is slow" cannot be attributed to a phase, a lens or a provider.
- **No progress surface.** `supervisor_status` exists and nothing instructs the
  host to call it or print it. Every dispatch response already knows the phase,
  the agents, the turns spent and the findings so far. It just does not say so
  in a form a host will echo.

---

## Batch 1 — Bind local agents

*Closes observation 1. Smallest, and first because every later batch is worth
less while the lenses run as generic agents.*

- Decide spawnability where the host is known — at construction in
  `discover_host_agent_files`, not as a property of a dataclass that cannot see
  the host. On Claude Code a `.claude/agents` file is spawnable by name; on
  Cursor a file agent stays a hint; a builtin is never spawnable.
- Make hint matching case-insensitive and word-boundary tolerant, so `security`
  binds "Security Specialist" through the hint list it was written for rather
  than through the fallback beneath it.
- Carry `host_agent_reason` on `AgentSpec` and `WorkPacket` — *why* this agent
  type, or why none — so a wrong binding is visible in the packet and on the log
  instead of being silent.
- Rewrite the MCP `INSTRUCTIONS` to tell the host to declare its agent types on
  `supervisor_start` and to honour `host_agent_type` when the packet sets one.
- Emit `HOST_AGENTS_DECLARED` even when the declaration is empty, so the log
  distinguishes "the host declared none" from "the host was never asked".

**From NOOA: nothing.** This is a straight defect fix and it should not wait for
an idea.

> **Done** in #53. Two things the plan did not anticipate. Hint matching was
> broken one layer deeper than described: the description-overlap pass matched
> bare substrings, so "security" bound an *Insecurity* Auditor — found by a test
> written for the hint pass and fixed in the same batch. And
> `build_execution_agent` already had a local named `binding` holding a
> `ModelBinding`, so the registry's binding is `agent_binding`; a blanket rename
> would have shadowed it silently.

## Batch 2 — Packets by reference

*Closes observation 2. Inspired by NOOA's progressive disclosure (`doc(obj)`)
and its warning that context blocks are eager — every selected block costs
prompt space on every turn.*

The idea taken is: bound what the prompt carries by default and let the reader
expand on demand. NOOA does it with a helper the model calls inside a REPL. We
have no REPL, so the adaptation is the filesystem — the run directory is already
authoritative and already on disk.

- Write each brief to `runs/<id>/packets/<agent_id>.t<N>.md`. `WorkPacket`
  returns `brief_path` plus a three-to-five line `brief_digest` — title,
  objectives, scope in one line, budget. The subagent reads the file; the
  orchestrator holds the digest.
- Write each turn schema **once per run** to `runs/<id>/contracts/<name>.json`
  and reference it by path. `render_directive` already avoids re-sending the
  schema on continuations; the initial brief of every new agent never did.
- Accept a result *path* on `supervisor_report`, so a large finding set never
  transits the orchestrator's context.
- Move `SHARED_TREE_RULE` and the baseline block into a run-scoped `RULES.md`
  referenced by one line from each brief. They are byte-identical in every brief
  of every run.

Keep the inline mode behind a flag: a host that cannot read files needs it, and
the autonomous backend does not want the indirection.

> **Measured.** An analysis fan-out of three lenses: **39,517 characters inline
> against 4,232 by reference**, an 89% reduction, as the JSON that actually
> crosses the MCP boundary. Two lenses measure about seven-fold rather than
> nine, because what a by-reference packet carries is mostly path length and
> does not grow with the brief — so the saving improves as a run fans out
> wider. Pinned at five-fold in the suite, deliberately below the observed
> value: a bar set just under it would need re-baselining every time a role's
> charter is edited.

**Divergence:** NOOA's disclosure is model-callable at run time. Ours is
resolved by the host before the subagent starts — same economics, no new tool
surface, and no new way for an agent to reach something it was not given.

## Batch 3 — The run ledger and stage timing

*Closes observation 3. Inspired by `runtime/harness_metrics.py` and ATIF's
`MetricsSchema`.*

Two ideas, one visible and one measured.

**Count where the harness silently helps the model.** NOOA's module tracks
exactly this — fence removal, import stripping, response fixups, error recovery
— per generation, flushed to a span. Our equivalents live in
[`core/responses.py`](../src/supervisor_harness/core/responses.py): schema
coercions, re-asks, field repairs. A run in which the harness repaired forty
responses is not the run the report currently describes, and that number is what
should drive `supervisor_providers` and the `TOOLING` / `ROUTING` lessons.

**Per-stage metrics as a first-class field.** Extend the usage object in
`models.py` with `cached_tokens`, `cost_usd` and wall-clock, recorded per packet
and per stage. This is what turns the README's claim about cheap continuous
drift-watching from an assertion into a measurement.

Then the visible part: every dispatch response carries a one-line ledger
(`analyzing · 2/4 lenses done · turn 9/24 · 11 findings · 4m20s`) and the
instructions tell the host to print it verbatim before dispatching — no extra
model calls. Alongside it, append one NDJSON line per event to
`runs/<id>/progress.ndjson` so a second terminal can `tail -f` a run. That is
NOOA's `tail()` producer inverted, and the cheapest available answer to "I
cannot see what is happening".

## Batch 4 — Trajectory export

*Inspired by ATIF (`src/nooa/atif/`).*

A pure projection from `events.jsonl`, in the same class as
[`core/journal.py`](../src/supervisor_harness/core/journal.py): no new events,
no model call, nothing written at run time. Their nesting maps onto ours
directly — a root trajectory for the supervisor, `subagent_trajectories[]` for
the lens and execution agents, each with an id resolved from inside the
document.

Two of their conventions are worth adopting as our own invariants:

- **A deterministic step is marked as such and carries no model metrics.** They
  spell it `llm_call_count=0`, with a validator forbidding `metrics` and
  `reasoning_content` on such a step. Ours are the heuristic drift assessments
  and the mechanical checkpoint scoring. A reader of the log currently cannot
  cheaply separate what the harness decided for free from what a model was
  asked, which is the central economic claim of the drift design.
- **A step retained across a compaction boundary is flagged**, so downstream
  consumers filter it. We have no compaction marker at all, and after batch 2 we
  will have runs long enough to need one.

**Divergence:** do not implement ATIF v1.7 literally. Pinning to someone else's
evolving spec version buys compatibility with consumers we do not have. Take the
shape, keep our own names, and write an adapter if a consumer appears.

## Batch 5 — Facts that know whether they are still true

*Inspired by `nooa-memory/references.py`.*

Their `MemoryRef` resolves to LIVE or DANGLING, and a DANGLING reference renders
the write-time snapshot **clearly stamped as stale** — their reasoning being
that the honest semantic for cross-agent recall is that reader B sees writer A's
snapshot, labelled as such.

That is our blackboard `Fact` with a dimension missing. A lens establishes
"the counters live in store X" during `analyzing`; a verifier inherits it during
`verifying`, three agents and a hundred edits later, and nothing tells it which
of those it is reading. Add a resolution kind to `Fact` — a file path, a symbol,
a command's output — re-checked when the fact is rendered into a brief, and
rendered as *stale, recorded at turn N* when the referent has moved. We already
keep contradictions rather than resolving by last writer; this is the same
instinct applied to time instead of to authorship.

Take their security rule verbatim as a rule, not just as an implementation
detail: **strict name lookup, never eval**, precisely because fact content
originates from other agents.

## Batch 6 — Lessons that decay and consolidate

*Inspired by `nooa-memory/{forgetting,reflection}.py`.*

The library has `occurrences`, `confidence` and `also_seen_in`, and
`recurring_lessons` groups by `(target, statement)` — but nothing decays,
nothing merges, and a lesson learned once outranks nothing for ever. With
`max_lessons_in_brief: 6`, which six an agent gets is already arbitrary and gets
more arbitrary with every run.

Their structure is the right one and is already the house style: an **ordered
deterministic pipeline** — dedup and merge, link, re-score, prune — with the
generative step optional and skipped when no model is supplied so the pass stays
offline-testable, and a report of what changed (merged, pruned, superseded) for
auditability. Pruning tombstones rather than deletes, which suits an append-only
log.

**Divergence: skip the Ebbinghaus curve.** Time since access is the wrong clock
here — a lesson about a subsystem nobody has touched in three months is not less
true. Decay against *runs in that workspace since the lesson was last
confirmed*, and let a lesson contradicted by a later run be superseded rather
than merely faded.

## Batch 7 — `supervisor audit`

*Inspired by `examples/arc_agi_3/analysis/red_team/`.*

Standalone scanners over a finished run's log, parameterised by run id so they
work on any run, writing an evidence directory. The envelope and the fence prove
what an agent **may** touch, at issue time. Nothing currently proves what it
**did**.

The method is the part to copy carefully. Their scanners classify only the code
inside the tool call and the stdout of the result, never the tool-advertising
surface, *so that advertising cannot be mistaken for use*. Our equivalent: a
brief listing a tool is not evidence the tool was used; only recorded turns are.

First scanners: writes outside the attenuated scope; commands run against the
forbidden list; git commands the shared-tree rule prohibits; criteria marked
passed whose evidence does not contain the command's output; findings closed by
tasks that touched none of the files they cite.

This pairs with the reconciliation artifact, and it is the one class of check an
agent cannot write for itself.

## Batch 8 — Enforce versus observe

*Inspired by `skills/nooa-middleware-hooks/SKILL.md`.*

They draw one line and hold it: interception may block and transform and its
exceptions propagate, because it is control flow; observation cannot change
anything and its exceptions are isolated. We have the enforcement —
[`core/tools.py`](../src/supervisor_harness/core/tools.py),
[`core/envelope.py`](../src/supervisor_harness/core/envelope.py) — but not the
stated invariant, and no test that an observer can never move a verdict.

Two hazards they document apply to us directly. Their re-entry guard:
enforcement must not recurse into work it triggered itself. And their warning
that a context-window retry re-runs interception, so it must be idempotent —
our remediation loop re-issues packets and re-runs criteria, and idempotence
there is currently assumed and nowhere asserted.

A small batch, mostly tests and a documented invariant. It is the one that makes
the control-plane claim checkable rather than argued.

## Batch 9 — Reactive supervision

*Inspired by `nooa.runtime.channels` and `nooa.runtime.producers`. Largest, and
last, because it changes the run loop.*

Today the supervisor is blind between turns: `agent_timeout_seconds` is off by
default, and the MCP instructions concede that the harness cannot tell a dead
agent from a slow one — which is why `supervisor_abandon` has to exist.

Their model is named channels in two modes (queue, consumed one at a time;
event, fire-and-forget into the next prompt), a `race()` returning the single
winner or nothing, registration order as priority, and background producers
feeding them — with a monitored command running in its own process group and the
whole group killed on cancel, so nothing is orphaned.

**Take now:** the process-group discipline, which is simply correct and applies
to any verification command the harness runs; and the queue/event distinction
for the blackboard, since most supervisor messages are notifications that should
render into the next brief rather than items an agent must consume, and we
currently model them all as the latter.

**Defer:** `race()` itself. Deterministic pull-based delivery is load-bearing
for replay, and it should not be traded until batches 3 and 7 have said where
the wall clock actually goes.

---

## Woven in rather than batched

Two pieces of NOOA's vocabulary, taken into docs alongside the batch that makes
each true.

**"Types validate values; Python validates the world."** Seven words for the
thesis of [`core/dod.py`](../src/supervisor_harness/core/dod.py), and it names a
split that module currently blurs: schema validation of a returned value versus
verification of external state. Into the batch 4 documentation.

**Separate agent instances do not isolate a shared external resource.** Their
statement of it — concurrent writers need separate worktrees, sandboxes or
namespaces — is the general form of the specific bug
[`core/baseline.py`](../src/supervisor_harness/core/baseline.py) exists to fix.
Worth citing there, together with the stronger remedy this harness has
implicitly declined (a worktree per execution agent), so that the decision is on
the record as a decision.

## Decided against

**NOOA's self-extending agents** — agent-authored libraries with lint-gated hot
reload. Well built, and pointed the opposite way from this harness's thesis: an
agent that can extend its own capabilities during a run is an agent setting the
terms of its own judgement. The narrow version worth revisiting later is letting
a *verified* check be promoted into the definition-of-done check library under a
gate of the same kind.

**The ellipsis-body DSL itself** — an agentic method declared by writing `...`
as its body. Elegant in a framework whose unit is a Python object. It moves the
contract into a place this harness's policy cannot inspect, which is the one
move the control-plane framing does not allow.
