# The next three, in order

A working document for a session that was not present when it was written, in
the same shape as `docs/remediation-plan.md`. It says what the next three pieces
of work are, what has been *verified* about each, what is still only a reading,
and what must be decided before the work starts.

**State as of `9bb1bf7`.** 306 tests pass, 2 skipped. Ruff reports 58 findings
against defaults the project has never configured, and CI gates on *no new*
(file, rule) pairs rather than on zero — `python tools/ruff_diff.py`.
`core/supervisor.py` is 2,584 lines of a 14,860-line package.

Every finding from the original 88-finding self-review is closed, both
outstanding policy calls are decided, and all four dimensions of the
control-plane assessment are done. What remains is these three.

## Before starting anything

Read `MEMORY.md` and the two memories it points at — they carry the working
conventions and the verification bar, and repeating them here would let the two
drift apart. The two that matter most on this work:

1. **Verify the claims in this document before designing against them.** Every
   batch this year has found at least one thing a planning document asserted
   that was no longer true — including a feature built and never wired, a README
   caveat that outlived its defect, and an assessment that described a
   consistency problem in an empty store. Sections below are marked
   **[verified]** or **[a reading]** accordingly.
2. **Do not stack PRs.** Delete-branch-on-merge is on, so merging the lower PR
   closes the upper one instead of retargeting it, and a closed PR cannot be
   reopened once its base is gone.

---

# 1 · Bedrock as a provider (issue #31)

> "Make sure the multi model support covers use cases where, for example,
> Amazon Bedrock is used as a provider. One common use case is through
> environment variables used by Claude Code CLI."

The issue conflates two cases that cost very different amounts. **Split them.**

## 1a · Host-delegated mode — verify and document, probably do not build

**[verified]** `HostProvider.complete` never calls a model. It raises
`DelegationRequired` carrying the packet, the MCP layer turns that into a tool
result, and the host reports back through `supervisor_report`. In the default
backend the harness is not in the model path at all.

**[a reading, and the thing to check first]** It follows that
`CLAUDE_CODE_USE_BEDROCK=1`, `AWS_REGION` and friends are consumed entirely by
Claude Code, and a user running Claude Code against Bedrock already gets a
working harness. Nothing has been run to confirm this.

**One concrete risk worth a test rather than a reading.** Bedrock model ids
contain a colon — `us.anthropic.claude-sonnet-4-5-20250929-v1:0` — and
`HarnessConfig._parse_ref` resolves a routing string by
`provider, _, model = primary.partition(":")`. `partition` splits on the *first*
colon, so this reads correctly; that is a reading of two lines, and a colon in a
model id is exactly the kind of thing that has bitten this project before.

**Definition of done**
- A test pins that a routing string whose model id contains a colon resolves to
  the whole id, not a truncated one.
- A run driven in host-delegated mode is confirmed to touch no provider code,
  by test rather than by assertion.
- `README.md` says plainly which modes support Bedrock today and which do not.

**Cost:** small. **Independent of 9c** — this is `providers/` and `config.py`,
which 9c does not touch.

## 1b · Autonomous mode — a dependency decision before any code

**[verified]** `AnthropicProvider` is a hand-rolled `httpx` client that sends
`x-api-key` and `anthropic-version` to a configurable `base_url`. Bedrock does
not accept that: it authenticates with AWS SigV4. This is not a `base_url` swap.

**[verified]** The package has exactly **one** runtime dependency: `httpx`.
Supporting Bedrock directly means botocore/boto3 or `anthropic[bedrock]`, and
either roughly triples the install footprint for one provider.

**[verified]** `providers/router.py:build_provider` is an `if kind == …` chain
ending in `raise ValueError(f"unknown provider type: {kind!r}")`, so
`"type": "bedrock"` fails today with a clear message rather than silently.

### The decision, which is the user's

| option | for | against |
|---|---|---|
| **Optional extra** (`pip install supervisor-harness[bedrock]`) | Keeps the default install at one dependency; the code path only loads when configured. | Two install shapes to document and test; CI needs the extra to cover it. |
| Always-on dependency | Simplest code and testing. | Triples the footprint of a package whose lightness is currently a feature. |
| Do not support it; document the workaround | Zero cost. A Bedrock-backed *gateway* exposing an Anthropic-compatible endpoint already works through `base_url`. | Leaves the issue open, and the workaround needs infrastructure the user may not want. |

**Do not start 1b until this is answered.** The options differ in what gets
built, not just in how much.

---

# 2 · Split `core/supervisor.py` (9c)

## Say the honest thing first

**There is still no defect behind this.** It has been recorded as *not
scheduled* since batch 9 for that reason, and the trigger written down for
revisiting it — "a defect that is hard to fix *because* of the file's size, or a
second person working in it" — has not happened.

What has changed is smaller than a trigger and worth stating rather than
inflating: item 3 wants architecture diagrams, and diagrams drawn against a
2,584-line module are diagrams that have to be redrawn if it is ever split. That
is a reason to decide the order, not a reason the split has become necessary.

**It is entirely reasonable to skip this and go straight to item 3**, drawing
the diagrams at the level of phases and data flow rather than modules — which is
arguably the more useful picture anyway. If that is the choice, record it here
and close 9c as declined rather than leaving it open forever.

## If it is done

**[verified]** The module already carries phase-shaped seams, marked by its own
section comments: planning (488), analysis (587), synthesis (621), execution
(763), verification (820), checkpoint (916), improvement (1006). 81 defs.

**[verified]** `core/phases.py` (1,086 lines) already holds the phase *content*
as free functions taking `RunState`. The seam is therefore between the phase
*machine* — methods on `Supervisor` that emit, transition and dispatch — and the
phase content that is already elsewhere.

### The design decision

Methods on one class cannot be moved to another file without choosing how:

| option | for | against |
|---|---|---|
| Free functions taking `(supervisor, session)` | Matches what `phases.py` already does; testable in isolation. | `supervisor` as a parameter is a god object by another name. |
| Mixin classes per phase | Minimal diff; methods stay methods. | Mixins hide the call graph, and this codebase has been consistently hostile to indirection that hides one. |
| Split by *layer*, not phase (emission/dispatch vs phase machine vs reporting) | Cuts along the real responsibilities. | The largest rewrite of the three, and the least mechanical. |

### Constraints anything must respect

- **The `emit` / `aemit` rule.** Synchronous emission is correct only where
  nothing is in flight; anything reachable while agents run must use the async
  form. It is mechanically checkable — *no `session.emit(` inside an
  `async def`* — and a split must keep that checkable.
- **306 tests must pass unchanged.** A refactor that needs its tests rewritten
  is not a refactor. If a test has to change, that is a signal the behaviour
  moved and is worth stopping over.
- **No behaviour change at all**, and the PR should say how that was
  established, not merely assert it.

**Cost:** high churn, no new tests to write, and the risk is entirely in what a
mechanical change does silently.

---

# 3 · Documentation (issue #30)

> "Documentation needs to be updated following the current development cycle.
> Better flow diagrams are needed to visualize the design and data flow. The
> reasoning control plane paradigm needs to be clearly explained and anchored in
> documentation and diagrams."

Two halves with different dependencies. **Do them separately.**

## 3a · The paradigm, anchored properly — independent of everything

**[verified]** The reasoning-control-plane framing currently lives *inside*
`docs/remediation-plan.md`, under "After the findings", in the middle of a
document about closing 37 findings. That is the wrong home for the paradigm the
project is built on: a reader looking for what the harness *is* has to read a
remediation log to find it.

The four dimensions are all closed now, and each has a landing point worth
naming:

| dimension | where it lives |
|---|---|
| deterministic guardrails | `core/tools.py` fence + floor, `config.py` `PROTECTED_SETTINGS`, `core/dod.py` bars |
| agent-to-agent attenuation | `core/envelope.py`, `ScopeEnvelope`, `Supervisor._spawn` |
| observability | `core/journal.py`, `supervisor explain` |
| shared semantic context | `Fact`, `FACT_ESTABLISHED`, `core/blackboard.py` |

**Definition of done:** the paradigm is explained where a new reader finds it,
with each dimension pointing at the code that implements it, and
`docs/remediation-plan.md` keeps the history rather than the definition.

**This can be done at any time, including before items 1 and 2.**

## 3b · Flow and architecture diagrams — after 9c is decided

**Why last:** diagrams that name modules go stale the moment the modules move.
If 9c is declined (a legitimate outcome), this unblocks immediately — draw them
at the level of phases and data flow instead, which does not depend on the file
layout.

Worth covering, and currently not drawn anywhere:

- the phase machine, including the paths that are *not* the happy one:
  abandonment, remediation, the checkpoint loop, envelope renewal;
- what an event is and how the log, the fold, the snapshot and the index relate
  — the durability story is the most reviewed part of this codebase and the
  least visible;
- delegated vs autonomous, and where the two paths diverge, which is a recurring
  source of confusion in the code's own docstrings;
- the fence: floor, envelope, task scope, agent scope, and what attenuates to
  what.

**[verified]** The README's only diagram today is a small ASCII box of the happy
path.

**Definition of done:** a reader can answer "what happens when a task fails
verification twice?" and "what is written where, and what survives a crash?"
from the documentation without reading the source.

---

## Recommended order, and why

1. **1a** — cheap, independent of everything, and its answer is an *input* to
   3a, which has to state what is supported.
2. **3a** — independent of the code layout, and overdue.
3. **Decide 9c** — do it, or close it as declined and record that.
4. **3b** — once the layout is settled either way.
5. **1b** — whenever the dependency question is answered; it does not block
   anything else.

`1b` and `2` are the two that need a decision before any code. Neither should be
started by asking the code what to do.
