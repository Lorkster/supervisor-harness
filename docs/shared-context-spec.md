# Shared semantic context — specification

The last open dimension of the control-plane assessment in
`docs/remediation-plan.md`. Recorded there as wanting "a design pass that ends
in a written spec before any code, the way the envelope got one". This is that
spec.

**Status: implemented on `feat/shared-context` (2026-09-02).** The three open
choices in §5 were decided by the user, each as recommended; §8 records them and
the one place the implementation departed from this spec.

Written to be picked up by a session that was not present for it, and to be
argued with: the section that mattered most was [§5 Open
choices](#5-open-choices), because those were the parts genuinely undetermined
and expensive to get wrong.

---

## 1. What the assessment said, and where it was wrong

> **Shared semantic context — partial.** `RunState.shared_context` and
> `RunState.facts` are event-sourced (`CONTEXT_SET`), reach briefs through
> `render_context`, and since 9b the supervisor answers an agent's questions
> from them. But they are free text, unversioned, and nothing checks that two
> agents mean the same thing by a term. `detect_contradictions` compares
> findings, not vocabulary.

Every clause of that is true. The conclusion drawn from it is not, because it
describes a *consistency* problem in a store that is, in practice, empty.

Verified against `main` at the time of writing:

| claim | status |
|---|---|
| `shared_context` / `facts` are event-sourced and reach briefs | **true** |
| the supervisor answers questions from them | **true** (`answer_from_record`) |
| nothing checks that two agents agree on a term | **true, and vacuous** |

`state.facts` is written in exactly two places in the whole codebase:

```
supervisor.py   facts[BASELINE_FACT]   the git commit the run started from
supervisor.py   facts["restated goal"] the planning model's restatement
```

Both keys are the harness's own. **No schema anywhere lets an agent contribute
a fact** — `ANALYSIS_TURN_SCHEMA` offers `output`, `findings`, `files_examined`,
`messages`, `open_questions`, `status`, `self_assessment` and `blocked_on`, and
none of them lands in `facts`.

So two agents cannot disagree about a term, because neither of them can state
one. Checking vocabulary consistency is downstream of having a vocabulary.

## 2. What is actually missing

**What one agent establishes does not become part of what the run knows.**

Analysis lenses run in parallel against the same task. Today a lens that
establishes something the others need — which store the counters must live in,
which of two endpoints is actually reachable, what "the limiter" refers to —
can only pass it on by *choosing* to send a message. Nothing accumulates. The
synthesis stage merges the findings at the end, which is after every analysis
agent has finished and can no longer use them.

Three concrete symptoms, all verified:

1. **The record has nothing to answer from.** `answer_from_record` (9b) reads
   `agent.objectives`, `agent.scope`, `state.facts` and the findings. Two of
   those four are the two harness facts above, so an agent asking "what did the
   security lens conclude about the account key?" is answered only if the words
   happen to overlap a *finding* that has already been recorded.

2. **Peers are named but not heard.** `_peers_block` tells each agent who else
   is working and invites messages. It cannot tell them what those peers have
   found, because the brief is rendered once, up front, as a stable anchor for
   drift scoring.

3. **`open_questions` is collected and discarded.** Every analysis agent is
   asked for it (`contracts.py`). `AgentTurn` has no field for it,
   `supervisor.py` never mentions it, and `build_report` reads `open_questions`
   only from the *synthesis* payload (`phases.py`). An analysis agent's open
   questions go nowhere — the same shape as the dead paths batch 9b removed,
   and a wasted prompt slot in every analysis brief.

## 3. The proposal

A run accumulates the facts its agents establish, and disagreement between them
is made visible rather than resolved silently.

### 3.1 Agents may establish facts

`ANALYSIS_TURN_SCHEMA` gains `established`: a short list of
`{key, statement, evidence}`. *(As written this said the execution schema would
gain it too; §5.1 then settled on analysis agents only, so only the analysis
schema asks. An execution agent that offers one anyway is ignored rather than
refused — there is no reason to fail a turn over a field nobody asked for.)* The wording in the brief follows the rule
the harness already states — *an assertion with no evidence is a finding you
have not made yet* — so a fact with no evidence is dropped, exactly as a
criterion marked passed with no evidence is recorded as failed.

This is deliberately **not** a free-text bag. A fact is keyed, because a key is
what makes two agents' claims comparable, and comparability is the whole point
of the dimension.

### 3.2 The harness records them, the agents do not

Facts arrive as proposals on a turn and are written by the supervisor as
`CONTEXT_SET` events — the mechanism that already exists and already folds.
This is the same division every other agent output is held to: findings,
messages and tasks are all parsed and normalised by the harness rather than
trusted as written. *Reasoning proposes, policy disposes.*

### 3.3 Disagreement is kept, not overwritten

The fold today is:

```python
for key, value in (p.get("facts") or {}).items():
    state.facts[str(key)] = str(value)
```

Last writer wins, silently. With two harness-written keys that has never
mattered; the moment agents write facts it is the bug this repository keeps
finding — a silent overwrite of one agent's work by another's.

A second, different value for an existing key is therefore recorded **as a
disagreement**, surfaced the way `detect_contradictions` already surfaces
clashing findings: to the supervisor, for judgement, rather than averaged away.

### 3.4 Established facts reach later agents

Through `render_context`, which already puts `facts` in every brief. No new
plumbing. An agent spawned after a fact is established inherits it; one already
running does not, because its brief is a fixed anchor for drift scoring and
rewriting it would break that.

### 3.5 `open_questions` is either wired or removed

It is currently asked for and dropped. Either it becomes the natural companion
to established facts — what the run does *not* know, carried alongside what it
does — or it comes out of the schema. Leaving a field that agents spend tokens
on and nothing reads is the thing 9b was about.

## 4. What this is not

- **Not an ontology.** No types, no schema for values, no inference. A fact is a
  key, a sentence and its evidence.
- **Not versioned.** The assessment called the current facts "unversioned" as a
  criticism. The event log *is* the version history: every `CONTEXT_SET` is on
  it with its actor and sequence, and `supervisor explain` can already show it.
  Adding a version field would duplicate the log.
- **Not a model call.** Detecting that two agents used one key for two claims is
  string comparison. Judging which is right is the supervisor's existing job.
- **Not retroactive.** A fact established at turn 4 does not reach an agent
  briefed at turn 1. Making it do so means re-rendering briefs, which drift
  scoring depends on not happening.

## 5. Open choices

These are the parts the spec does not settle, and the reason it exists.

### 5.1 Who may establish a fact?

| option | for | against |
|---|---|---|
| **Analysis agents only** | They are the stage whose job is establishing things; execution agents report what they changed, which findings and turns already carry. | An execution agent that discovers a load-bearing fact mid-change has nowhere to put it. |
| Any agent | Uniform; no rule to remember. | Verifiers establishing facts about the thing they are judging is a conflict of interest of exactly the kind batch 7 was about. |
| Synthesis only | It already merges and is already trusted to. | Nothing accumulates *during* analysis, which is the gap. |

### 5.2 What happens on a conflicting key?

| option | for | against |
|---|---|---|
| **Keep both, flag it** | Matches how contradictions between findings are handled; the disagreement is the signal. | The brief has to render two competing values without confusing the reader. |
| First writer wins | Simple; stable. | Rewards being early over being right. |
| Last writer wins (today) | No change. | Silent overwrite — the failure mode this repo keeps finding. |

### 5.3 How much should the harness police a key?

Two agents writing `redis` and `Redis`, or `counter store` and `counter-store`,
are agreeing. Two writing `store` for different things are not.

| option | for | against |
|---|---|---|
| **Normalise case and separators only** | Cheap, obvious, no false merges of distinct concepts. | `counter store` and `counters` stay separate. |
| Token-similarity merge (`jaccard`, already in `drift.py`) | Catches near-misses. | Merging two facts that were never the same is worse than leaving them apart, and it is unfalsifiable from inside the run. |
| Nothing | Honest. | Every spelling is a new key and the store fragments. |

## 6. Definition of done

- An analysis agent can establish a keyed fact with evidence, and a later agent's
  brief carries it.
- A fact with no evidence is dropped, and the run says so.
- Two agents claiming the same key with different statements produces a recorded
  disagreement, not a silent overwrite — with a test that fails against the
  current fold.
- `open_questions` is either read or gone.
- Every new test fails against the previous commit; where a test is a guard
  rather than a proof, its docstring says which.
- Ruff findings diffed against `main` by rule and file.

## 7. Cost and risk

Small-to-medium. The storage, the event, the fold and the brief plumbing all
exist; what is new is a schema field, a parser, a conflict rule and the
rendering. The risk is not in the code — it is that a badly chosen key
discipline (§5.3) either fragments the store or silently merges two things that
were never the same, and the second is much worse than the first, because a run
cannot tell from the inside that it has done it.


---

## 8. What was decided, and what changed in the building

All three of §5's choices were taken as recommended.

| choice | decision |
|---|---|
| §5.1 who may establish | **Analysis agents only.** An execution agent's changes are already carried by its turn and its findings; a verifier writing into the record it judges against is batch 7's conflict of interest in different clothes. |
| §5.2 conflicting key | **Keep both, flag it.** Rendered in the brief as "agents disagree, treat as open", appended to the report's conflicts, and shown in `supervisor status`. |
| §5.3 key discipline | **Case and separators only.** `jaccard` is deliberately not used: merging two facts that were never the same destroys a distinction silently, and a run cannot tell from the inside that it has done it. |

**One departure from §3.2.** The spec said facts would be recorded as
`CONTEXT_SET` events, "the mechanism that already exists and already folds".
They are not. `CONTEXT_SET` carries `shared_context` and a flat `dict[str, str]`
whose fold is `facts[key] = value`; a `Fact` has an author, evidence and the
capacity to be contested, and overloading one branch to do both would have made
it do two unrelated things. A distinct `FACT_ESTABLISHED` event was added
instead, which also gives `supervisor explain` and `supervisor events --type`
something to name.

**Two things §3 did not anticipate.**

`RunState.facts` was left exactly as it was rather than being widened. It holds
what the *harness* knows — the baseline commit, the planner's restatement — and
those have no author, need no evidence and cannot be contested. Giving them an
`evidence` field and a conflict story they can never have would have been worse
than keeping two stores, so `render_context` renders them apart and says which
is which. That matches what the harness already does elsewhere: a lesson says
`learned here` or `learned in <project>`, an envelope says `configuration` or
`run plan`.

`open_questions` (§3.5) was **wired**, not removed. `AgentTurn` gains the field,
`supervisor status` collects them across turns, and `supervisor explain` shows
them beside the turn that raised them.

## 9. Verification

306 pass (292 + 14 new). Ruff diffed against `main` by rule and file: 58 before,
58 after — the gate added in B1 caught an import-ordering regression in
`core/supervisor.py` during this work, which is the first time it has earned its
keep on a change it was not written for.

Every mechanism was disabled in turn and every one is caught. Two rows needed a
fix first, and only one was the script's fault:

- *"every agent kind may establish facts"* passed with the guard removed,
  because the fake execution agent never proposed a fact — so the test could not
  tell the rule from the absence of anything to apply it to. The fixture now has
  an execution agent offer one, which is expected to be ignored.
- *"briefs do not carry what the run established"* matched two call sites and
  the sabotage only patched one. A script problem, not a test problem.

## 10. What this still does not do

Everything in §4 stands. In particular a fact established at turn 4 does not
reach an agent briefed at turn 1: analysis lenses are briefed together and a
brief is a fixed anchor for drift scoring, so the agent that inherits is the one
spawned afterwards — in practice the execution agent. Two lenses running in
parallel still reach each other only through messages. Closing *that* means
either re-rendering briefs mid-run, which drift scoring depends on not
happening, or delivering facts through the directive that starts each turn,
which is a larger change and a separate decision.
