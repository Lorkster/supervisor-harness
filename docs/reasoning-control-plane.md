# The reasoning control plane

**What the harness is, and where each part of it lives in the code.**

A single coding agent proposes and disposes in the same breath. It decides what
the task means, decides what it may touch, decides when it is finished, and
reports all three as one answer. Every one of those is a judgement, and the
agent making them is also the subject of them.

This harness is a **control plane over reasoning**: a layer that governs how
agents reason, sitting outside the reasoning it governs. The agents still do the
thinking. What they do not do is set the terms they are judged against, grant
themselves authority, or decide when they are done.

Stated as one rule, which the rest of this document is the elaboration of:

> **The subject of a judgement may not set the terms of it.**

## Where the framing comes from

The four dimensions below are borrowed from an article on the reasoning control
plane (DZone, 2026), which argues that multi-agent systems need a governing
layer over reasoning itself and describes it along four axes: shared semantic
context, agent-to-agent access controls that attenuate as authority is
delegated, observability over non-deterministic decisions, and deterministic
guardrails outside the model's reasoning context. Its four-word summary of the
last, *"reasoning proposes, policy disposes"*, is this project's own design
stated better than the project had stated it.

It is borrowed as a way of seeing the harness from outside — not as a standard
being conformed to, and not as a claim that these four are the only axes that
matter. Where the harness does something the framing does not ask for, or
declines something it does, this document says so.

All four dimensions are implemented. What follows is what each one means, how
the harness does it, where the code is, and what it deliberately does not do.

The shape of the claim, before the detail — what sits outside the model's
reasoning, and what the model is left to do:

```
                          the task, from you
                                  │
   ╔══════════════════════════════▼════════════════════════════════╗
   ║  1 · DETERMINISTIC GUARDRAILS                                  ║
   ║  the execution fence · the config trust boundary · the         ║
   ║  definition-of-done bars. Ordinary code. None of it asks       ║
   ║  the model whether it should apply.                            ║
   ║                                                                ║
   ║    ┌────────────────────────────────────────────────────────┐  ║
   ║    │ 2 · ATTENUATION                                        │  ║
   ║    │ the run's envelope, narrowing at every hand-off and     │  ║
   ║    │ widening at none                                       │  ║
   ║    │                                                        │  ║
   ║    │      ┌───────────────────────────────────────────┐     │  ║
   ║    │      │          the model reasons                │     │  ║
   ║    │      │   the only part of this that does, and    │     │  ║
   ║    │      │   the only part that sets no terms        │     │  ║
   ║    │      └───────────────────────────────────────────┘     │  ║
   ║    │                                                        │  ║
   ║    │ 4 · SHARED CONTEXT                                     │  ║
   ║    │ facts other agents established, with their evidence;    │  ║
   ║    │ where two disagree, both survive                       │  ║
   ║    └────────────────────────────────────────────────────────┘  ║
   ║                                                                ║
   ║  3 · OBSERVABILITY                                             ║
   ║  every input to every directive is on the log, and             ║
   ║  `supervisor explain` reassembles it afterwards                ║
   ╚════════════════════════════════════════════════════════════════╝
                                  │
                    a directive back to the agent — and a
                    verdict that only evidence can move
```

The four bounds drawn against the code that applies them, rather than against
the reasoning they govern, are in [`architecture.md`](architecture.md).

---

## 1 · Deterministic guardrails

*Bounds that hold whatever the model concludes, evaluated outside its reasoning
context.*

A guardrail a model can talk its way past is not a guardrail. Everything in this
dimension is ordinary code that runs before or after the model, and none of it
consults the model about whether it should apply.

**The execution fence.** Writes and commands are checked against the agent's
scope, and under that scope sits a floor nothing can lower: `VCS_DIRS` and
`STORE_DIRS` in [core/tools.py:118](../src/supervisor_harness/core/tools.py:118)
keep every agent out of `.git` and out of the harness's own run store, whatever
its scope says. The command fence is **universal** — every agent gets it, not
only those with a declared scope. An empty scope relaxes the per-path check
alone, and relaxes it to the workspace rather than to the machine.

**The configuration trust boundary.** A config file inside the workspace is not
trusted with everything, because the workspace is usually a repository someone
else wrote. `PROTECTED_SETTINGS`
([config.py:272](../src/supervisor_harness/config.py:272)) and
`PROTECTED_PROVIDER_KEYS` ([config.py:311](../src/supervisor_harness/config.py:311))
are the list of what it may not set: command execution, provider `base_url`, API
keys, the AWS `region` and `profile` a Bedrock route would use, the store
location. The principle is the same one in a different costume —
a repository may tune how much work the harness does, not how sceptical it is
about work done on it.

**The definition-of-done bars.** Criteria that cannot fail are rejected at
proposal time, not at verification time. Policy inserts test, security and
code-quality bars where a task admits them, and `verify_command`
([core/dod.py:688](../src/supervisor_harness/core/dod.py:688)) holds a runner to
an allow-list. A criterion marked passed with no evidence is recorded as
**failed** — the one place where the harness overrules a model's own report of
its work.

**What this deliberately does not do.** It does not sandbox. The autonomous
toolset confines reads to the workspace and writes to the agent's scope, but
`run_command` is a *fence*, not a jail: it constrains which program runs and
which paths are named, and a program that gets past that runs with the user's
own privileges. Shell execution is off by default for exactly this reason, and
in delegated mode the decision belongs to the host rather than to the harness.

---

## 2 · Agent-to-agent attenuation

*Authority narrows as it is delegated, and never widens.*

This is the dimension that was weakest, and the reason is worth keeping: every
`Scope` in a run is proposed by a *model* — the planner's for an analysis lens,
the synthesis model's for an execution task — and before
[core/envelope.py](../src/supervisor_harness/core/envelope.py) existed, no two
of them were ever compared. The fence was enforced immaculately. Nobody asked
where the fence came from. The synthesis model drew the boundary that its own
tasks then ran inside.

**The run envelope** is the answer. `ScopeEnvelope`
([models.py:255](../src/supervisor_harness/models.py:255)) is a run-level grant,
established once: configuration is the floor, the plan may **narrow** it, and
nothing may widen it (`establish`,
[envelope.py:83](../src/supervisor_harness/core/envelope.py:83)).

**Every scope below it is attenuated to every ceiling above it.** `attenuate`
([envelope.py:122](../src/supervisor_harness/core/envelope.py:122)) narrows a
scope to the stack of ceilings over it and says *which* ceiling bit, so a
clamp is legible rather than mysterious. It runs at all three points where
authority is handed on — when synthesis creates a task (`attenuate` at
[supervisor.py:550](../src/supervisor_harness/core/supervisor.py:550)), when an
agent is spawned (`attenuate` at
[lifecycle.py:92](../src/supervisor_harness/core/lifecycle.py:92)), and
when you edit a task's `scope_paths` at approval (`_apply_modifications` at
[supervisor.py:1612](../src/supervisor_harness/core/supervisor.py:1612)). It
narrows rather than refuses: a model proposing too much is ordinary, and losing
the task over it is not.

**The grant has a shelf life.** An envelope carries a date, and a stale one is
re-asked rather than silently honoured (`stale_reason`,
[envelope.py:162](../src/supervisor_harness/core/envelope.py:162)). Renewal
renews the *date*, never the paths.

**Two decisions worth not re-deriving:**

- **Approval may not widen the envelope.** A `scope_paths` edit at approval time
  is clamped like any other scope, and the clamp is recorded. A per-task
  approval is a decision about *that task*; if it could move a run-level bound,
  the bound would only ever be as strong as the most permissive task anyone
  approved — which is not a bound. The cost is real and is not hidden: a plan
  that draws the envelope too narrowly cannot be widened from the approval
  prompt, only restarted with a wider one.
- **`pattern_within` is sound, not complete.** `True` is a proof of containment;
  `False` means "not provably contained", and every caller narrows on `False`.
  It refuses two cases on purpose: wildcard-inside-wildcard, and containment in
  a *union* rather than in one member — because whether `src/*` sits inside
  `{src/a*, src/b*}` depends on what happens to be on disk, and a fence whose
  meaning changes when a directory is created is not a fence.

---

## 3 · Observability over non-deterministic decisions

*Every supervisory decision can be reconstructed, with the inputs that produced
it.*

After every turn the harness issues a directive back to the agent — one of the
nine kinds in `DirectiveKind`
([models.py:96](../src/supervisor_harness/models.py:96)): continue, refocus,
narrow, deepen, answer, escalate, accept, reject, stop. The choice is driven by
deterministic drift heuristics and, only when those fire, a second model
opinion. A directive whose reasoning cannot be recovered is indistinguishable
from an arbitrary one.

**The decision journal** ([core/journal.py](../src/supervisor_harness/core/journal.py),
surfaced as `supervisor explain <run> [agent]`) answers *why was this directive
issued to this agent* in one place: the brief the agent was given, the turns
before it, the inbox it carried, the drift assessment and its signals, the
directive chosen, and any question the supervisor answered from the run's
record. It is a pure projection — no new events, no model call, and it writes
nothing.

**It reads the event log, not `RunState`, and that is the whole point.** The
plan that scheduled it asserted everything was already in `RunState`. That is
true of briefs, turns, messages and notes. It is false for the one input that
matters most: `RunState.drift` is keyed by agent, so the fold keeps only each
agent's **newest** assessment and overwrites every earlier one. Measured on a
run with one three-turn agent: twelve assessments on the log, eight in
`RunState`, and an agent that peaked at 0.85 reading as 0.4 in the snapshot.
An assessment that has been overwritten cannot explain the directive it
produced.

That split is the right one generally: **`status` answers "where is this run
now" from the snapshot; `explain` answers "how did it get here" from the log.**

**Related, and part of the same dimension:** lessons carry where they were
learned, findings are reconciled one by one at the end of a run rather than
summarised, and the store records its own damage signals (`orphaned_events`,
`rejected_events`, `damaged_lines`) instead of quietly dropping what it could
not read.

---

## 4 · Shared semantic context

*Agents establish facts for the run, and disagreement is kept rather than
resolved by whoever wrote last.*

Parallel lenses that each rediscover the same thing are wasting turns; parallel
lenses that quietly assume *different* things about the same thing are worse,
because the contradiction only surfaces in the work.

**A `Fact`** ([models.py:283](../src/supervisor_harness/models.py:283)) is
something an agent established, under a normalised key, with the evidence that
backs it and the author who established it. Establishing one emits
`FACT_ESTABLISHED` ([events.py:47](../src/supervisor_harness/store/events.py:47)),
and facts reach later agents' briefs through `render_context`
([core/blackboard.py:84](../src/supervisor_harness/core/blackboard.py:84)).

Note the distinction the type carries, which is easy to get wrong:
`RunState.facts` holds what the **harness** knows — the baseline commit, the
planner's restatement. Those have no author, need no evidence, and cannot be
contested. A `Fact` has all three.

**Three decided rules:**

- **Only analysis agents establish facts.** An execution agent may offer one; it
  is ignored. Establishing is a claim about what is true of the codebase, and
  the agent that is changing the codebase is not a neutral party to it.
- **Disagreement is kept.** Where two lenses key the same claim and say
  different things, both survive — shown as open in the brief, in the report's
  conflicts, and in `supervisor status`. It is not resolved by write order.
- **Keys normalise on case and separators only.** Fuzzy merging (`jaccard`) was
  considered and rejected: a bad merge destroys a distinction *silently*, and
  fragmentation is at least visible.

A claim with no evidence is dropped.

**What this deliberately does not do.** A fact established at turn 4 does not
reach an agent briefed at turn 1. Analysis lenses are briefed together, and a
brief is a fixed anchor for drift scoring — so the agent that inherits facts is
the one spawned afterwards, in practice the execution agent. Two lenses running
in parallel still reach each other only through messages. Closing that means
either re-rendering briefs mid-run, which drift scoring depends on *not*
happening, or delivering facts through the directive that starts each turn —
a larger change, and a separate decision.

---

## How the four compose

One task, end to end, touching all four:

1. You give the harness a task. The planner proposes lenses and draws the run's
   **envelope** — narrowing the configured floor, never widening it *(2)*.
2. Analysis lenses run in parallel, each with a scope **attenuated** to that
   envelope *(2)*, each fenced by the floor whatever its scope says *(1)*.
3. A lens establishes that the counters live in `src/cache.py`. It becomes a
   **fact** with its evidence; a second lens keying the same claim differently
   does not overwrite it *(4)*.
4. After each turn, deterministic drift heuristics run. If they fire, and only
   then, a model gives a second opinion. Whatever directive results, its inputs
   are on the log and `explain` can reassemble them *(3)*.
5. Synthesis proposes tasks with definitions of done. Criteria that cannot fail
   are rejected here, before any work happens *(1)*.
6. **You approve.** An edited scope is clamped to the envelope, and the clamp is
   recorded *(2)*.
7. Execution runs, inheriting the facts *(4)*, fenced *(1)*, watched *(3)*.
8. Verification judges against the criteria. A criterion passed without evidence
   is recorded as failed *(1)*. The verifier is itself a supervised agent with a
   recorded turn and a drift score *(3)*.

The through-line: at every step the thing being judged is not the thing setting
the terms.

## What this is not

It is not a safety mechanism against a hostile model. Every bound here assumes
an agent that is capable and occasionally wrong, not one that is adversarial —
a fence that a determined process can get around by running a program is not
claimed to be more than it is. It is not a sandbox, it is not a permission
system, and in delegated mode it deliberately does not try to be: the host's
own permission model is the one that runs.

What it is: a layer that makes an agent's authority, reasoning and finish line
into things that can be *bounded and inspected from outside* — rather than
things you take the agent's word for.

---

## Where to read next

| | |
| --- | --- |
| [`../README.md`](../README.md) | What the harness does, and how to run it |
| [`architecture.md`](architecture.md) | The same four bounds **drawn**: the phase machine, the durability story, the two backends, and the fence |
| [`protocol.md`](protocol.md) | The wire protocol between harness and host |
| [`shared-context-spec.md`](shared-context-spec.md) | Dimension 4 in full, including its open choices |
| [`history/`](history/) | The **records**: the self-review that found the defects, and the plan that closed them. Closed, and kept for the reasoning rather than the conclusions |

[`history/self-review.md`](history/self-review.md) is where this framing was
first written down, in the middle of a document about closing 37 findings. That section is kept as the
record of the assessment *as it stood then* — four dimensions with two of them
open. This document is the current definition; that one is how it got here.
