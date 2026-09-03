# What comes next, in order

A working document for a session that was not present when it was written, in
the same shape as `docs/remediation-plan.md`. It says what the next pieces of
work are, what has been *verified* about each, what is still only a reading, and
what has been decided.

It was written as *the next three*. Item 4 arrived with the 9c decision, and the
title moved rather than the item being squeezed into one of the others.

**State as of `9bb1bf7`.** 306 tests pass, 2 skipped. Ruff reports 58 findings
against defaults the project has never configured, and CI gates on *no new*
(file, rule) pairs rather than on zero — `python tools/ruff_diff.py`.
`core/supervisor.py` is 2,584 lines of a 14,860-line package.

Every finding from the original 88-finding self-review is closed, both
outstanding policy calls are decided, and all four dimensions of the
control-plane assessment are done. What remains is the items below.

## Decisions taken, 2026-09-02

Both questions this document said had to be answered before any code have been,
by the user. They are recorded here rather than in the sections below so that a
reader meets the answer before the argument:

| question | decision |
| --- | --- |
| **1b** — how much dependency Bedrock is worth | **Optional extra.** `pip install supervisor-harness[bedrock]`; the default install keeps its single runtime dependency and the Bedrock code path loads only when configured. |
| **9c** — split `core/supervisor.py`, or decline it | **Accepted, and widened.** The reason given is maintainability rather than a defect: any class that size is impractical to maintain regardless of whether a bug has yet been traced to it. The scope now extends past the split — see item 4. |

The 9c decision overrides this document's own recommendation to close it as
declined, and the reasoning is worth keeping rather than quietly overwriting:
this document argued that with no defect behind it, the churn was not yet
bought. The counter-argument accepted is that a 2,584-line class is a
maintainability cost paid continuously and not visible as any single defect -
which is a judgement about the codebase rather than a claim about the code, and
is the user's to make.

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

### Decided: the optional extra

`pip install supervisor-harness[bedrock]`. What that commits to:

- `[project.optional-dependencies]` gains a `bedrock` group; the default install
  still has exactly one runtime dependency.
- `providers/bedrock.py` imports its dependency lazily, so importing
  `providers.router` on a default install cannot fail.
- `build_provider` gains a `bedrock` branch whose missing-dependency error names
  the extra to install, rather than surfacing a bare `ImportError`.
- CI installs the extra in at least one job, or the branch is untested code.
- The routing docs gain the Bedrock model-id form, which is the colon case that
  item 1a pins.

### Closed

Landed on `feat/bedrock-provider`. Every point above holds, and three things
were decided or found in the building that the plan did not anticipate.

**The dependency, which the plan left open.** It said "botocore/boto3 or
`anthropic[bedrock]`" without choosing. Costed both and put it to the user:
signing here with botocore would have been the smaller install and would have
kept one transport story, but **`anthropic[bedrock]` was chosen** so that the
request/response mapping and the AWS credential chain are the vendor's to
maintain rather than ours. `providers/bedrock.py` therefore wraps
`AsyncAnthropicBedrock`.

**`region` and `profile` are in `PROTECTED_PROVIDER_KEYS`.** Bedrock needs a
region, and a region has to live somewhere. It belongs with `base_url` rather
than with the tunable settings: both decide where a credentialed request goes,
and `profile` decides which identity signs it. The AWS chain resolves an
identity from the environment, so a workspace file able to set either could
redirect a run's traffic or make it assume another profile **without ever naming
a secret** — which is precisely the shape batch 7 protected `base_url` against.

**The SDK's retry loop is turned off** (`max_retries=0`). `ModelRouter.complete`
already retries with backoff and then falls back down the `|` chain; leaving the
SDK's own loop on multiplies the two, so a stage configured for one retry would
make up to eight attempts against a throttled endpoint. That is the worst
available response to throttling, and it would have been invisible.

**One trap found by a test failing.** This package has its own
`providers/anthropic.py`, so a fixture blocking `import anthropic` by *name*
also blocks `from .anthropic import AnthropicProvider` in `router.py` — the
relative import arrives at `__import__` under the same name. The fixture now
blocks only `level == 0`, and the shadowing has a test of its own, because the
failure it would produce is bizarre rather than obvious.

**One vacuous-coverage bug, found by running the default shape for real.**
`tests/test_bedrock_provider.py` used a module-level `pytest.importorskip`,
which skips the **whole file** from that point — taking the "without the extra"
tests with it. So on a default install, the very tests guaranteeing that a
default install works did not run: measured at **0 passed, 1 skipped**, and
reported as success. It is now a per-test `skipif` marker, and the same install
shape reports 8 passed, 13 skipped. CI gained the mirror of the `bedrock` job's
check — the matrix now fails if no bedrock test *passes* — so the hole is
guarded from both sides.

Found only by building a throwaway venv with `.[dev]` and running the suite in
it. Neither shape is the developer's, so both have to be run deliberately.

**Verification.** 13 sabotages, 13 caught — the laziness of the SDK import, the
install hint, the trust boundary on `region`/`profile`, the schema instruction,
retryability by status, retryability of connection errors, `max_retries=0`, the
usage mapping, the empty-conversation repair, the missing-region check, the
region reaching the client, `aclose`, and the `build_provider` branch. CI gains
a `bedrock` job that installs the extra, and that job **fails if the
with-the-extra tests skip** — otherwise `importorskip` would report success
while covering nothing. Both shapes were run before the pull request: 335 passed / 2 skipped with the extra, 322 passed / 15 skipped without it.

One test from item 1a was changed on purpose:
`test_bedrock_as_a_provider_type_is_refused_by_name` existed so that this batch
would have to make `"type": "bedrock"` work deliberately rather than by
accident. It is now
`test_an_unknown_provider_type_is_refused_by_name`, keeping the guarantee with a
type the harness genuinely does not implement, plus a test asserting the
transition itself.

**Not done, deliberately.** `default_config()` does not declare a `bedrock`
provider. The other providers are declared unconfigured so that
`supervisor providers` lists them, but a Bedrock row would report
`dependency_installed: false` for everyone who never wanted it. It is added by
hand, and the README says how.

---

# 2 · Split `core/supervisor.py` (9c)

## Decided: do it — and say the honest thing anyway

**Accepted on maintainability grounds, not on a defect.** That distinction
should survive into the PR description, because it changes what "done" means:
there is no failing behaviour to point at afterwards, so the only available
evidence that the split was safe is that nothing changed — which has to be
*shown*, not asserted.

**There is still no defect behind this.** It had been recorded as *not
scheduled* since batch 9 for that reason, and the trigger written down for
revisiting it — "a defect that is hard to fix *because* of the file's size, or a
second person working in it" — has not fired. It is being done anyway, because a
class of that size is a cost paid on every future change rather than one that
shows up as a bug.

What has changed is smaller than a trigger and worth stating rather than
inflating: item 3 wants architecture diagrams, and diagrams drawn against a
2,584-line module are diagrams that have to be redrawn if it is ever split. That
is a reason to decide the order, not a reason the split has become necessary.

## The work

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

**The design decision above is still open.** It is a design call rather than a
policy one, and it should be made against the code with the three options
costed — not settled by whoever starts typing first.

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

### Closed

Landed on `docs/control-plane-paradigm`.

`docs/reasoning-control-plane.md` is the definition: the one rule the design
reduces to, where the framing was borrowed from and what is *not* being claimed
by borrowing it, then each of the four dimensions — what it means, how the
harness does it, the code that implements it, and what it deliberately does not
do. It ends with one worked path through a run touching all four, and an
explicit "what this is not" (it is not a sandbox, not a permission system, and
assumes an agent that is wrong rather than hostile).

`docs/remediation-plan.md`'s framing section is **kept, not rewritten**. It is
the assessment as it stood when two dimensions were open and two of its claims
were wrong, and that is worth having; it now opens with a pointer saying the
definition lives elsewhere and that this is the historical record. Rewriting it
to match the code would have destroyed the record of how the framing was
arrived at.

`README.md` states the one rule in its opening and links to the document, and
gains a documentation index — the `docs/` directory was previously reachable
only by knowing it was there.

**One thing was built that item 3a did not ask for.** The document explains the
design by pointing at code *by file and line*, which is the useful form and also
the one that rots silently. Every other place in this repository that names code
is a test that would fail; prose had no guard at all. `tools/check_doc_refs.py`
is that guard — for every `path:line` citation it checks the file exists, the
line exists, and the symbol the prose names in backticks is still within two
lines of it. An unanchored line number is reported rather than passed, because
that is the case the tool exists to prevent. It runs in the `lint` job and gates
at **zero**: unlike the ruff baseline there is nothing pre-existing to
grandfather in.

Three sabotages confirmed it discriminates: a cited line drifting by five, a
cited symbol renamed in the code, and a link to a document that does not exist.
All three were caught; the first two would both have shipped silently without
it.

## 3b · Flow and architecture diagrams — after the split lands

**Note after 3a:** `reasoning-control-plane.md` now carries a prose walk of one
run touching all four dimensions. That is the *narrative*; it is not a
substitute for the diagrams below, and the two should agree when 3b is done.

**Why last:** diagrams that name modules go stale the moment the modules move,
and item 2 is now going to move them. Draw these after the split — or draw them
at the level of phases and data flow, which does not depend on the file layout
and is arguably the more useful picture regardless.

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

# 4 · A best-practices pass over the whole package

**Added with the 9c decision:** *"after all changes we need to make sure the
entire application adheres to best practices for architecture, code quality and
testing."*

This is a bigger and less defined piece of work than the three above, and it
should not be started as one undifferentiated sweep. What it needs first is an
**assessment** — the same shape as the control-plane assessment that produced
items 1-3 — against explicit written criteria, producing findings that are then
closed in batches. Guessing at "best practices" without criteria written down
first is how a refactor becomes taste.

**Nothing in this section is verified.** These are the axes an assessment would
cover, not findings:

- **Architecture** — module boundaries and their direction; whether `core/`
  depends on `store/` and `providers/` only through interfaces it owns; whether
  `RunState` is a data structure or a god object; where the phase machine ends
  and phase content begins (item 2 answers part of this).
- **Code quality** — the 58 ruff findings CI currently only gates on *not
  getting worse*: decide which rules the project actually wants, configure them
  in `pyproject.toml` rather than running against unconfigured defaults, and
  drive the chosen set to zero. Type coverage is unmeasured; there is no
  `mypy` or `pyright` in CI at all.
- **Testing** — 306 tests with no coverage measurement, so what is untested is
  unknown. Two questions worth asking of the suite itself: how many of its tests
  would survive the sabotage check that has caught five vacuous tests across two
  batches, and whether the fixtures in `conftest.py` have grown into a second
  implementation of the thing under test.

**Definition of done for the assessment** — not for the work it finds: written
criteria, findings with ids, and a batch plan, in the shape
`docs/remediation-plan.md` already uses so that the two read the same way.

### 4a closed

Landed on `chore/quality-assessment` as
[`docs/quality-assessment.md`](quality-assessment.md): ten written criteria, 15
findings with ids, a five-batch plan for 4b, and the instruments in CI — a
coverage floor at 82%, mypy gating at **zero**, and ruff against a configured
rule set for the first time.

Four findings are closed by that batch itself: the three type errors, and a real
flaw in `tools/ruff_diff.py` — it ran ruff separately in each tree, so each used
its own configuration, meaning any change to the rule set compared two different
rule sets. Both trees now use the working tree's.

**Two claims in this document were wrong, and the measurement says so.** Type
coverage was not a risk: 3 errors across 40 files. And the argument for putting
4a before the split — *"with no coverage measurement nobody knows what fraction
of a 2,584-line module they exercise"* — was answered at **94%**. The ordering
still holds, for the other reason given: *how* to split is an architecture
question and the criteria should precede the design call. But the fear was
unfounded, and the complexity numbers reframe item 2 further: the worst function
in the codebase is `store/events.py`'s fold at 48 branches, and
`core/supervisor.py` holds only 3 complexity findings. **The split is a
file-size fix, not a complexity fix**, and should not be sold as the latter.

**Order:** after item 2, because a split that moves half the package would
invalidate architecture findings written against the current layout.

---

## Recommended order, and why

Revised 2026-09-02, after the decisions above. Two things moved, and both
moves have a reason beyond preference.

| | item | why here |
| --- | --- | --- |
| 1 | **3a** — the paradigm document | Cheap, independent, overdue, and it is what a new reader currently gets wrong. **Done** — see below. |
| 2 | **1b** — the Bedrock optional extra | Small, independent, closes issue #31 — and it lands *before* the assessment so the assessment covers the module set being kept. **Done** — see §1b. |
| 3 | **4a** — the assessment: criteria, instrumentation, and layout-independent findings | Produces the coverage measurement and the architecture criteria that item 2 needs in order to be provable. **Done** — [`quality-assessment.md`](quality-assessment.md). |
| 4 | **2 (9c)** — the split | Made against 4a's criteria and protected by 4a's coverage. |
| 5 | **4b** — close the remaining findings in batches | Against the settled layout, so cleanup diffs are not written into `core/supervisor.py` and immediately moved again. |
| 6 | **3b** — the diagrams | Last, unchanged: diagrams that name modules go stale the moment the modules move. |

### What moved, and why

**1b moved early.** It was "anywhere in the list". It should be before the
assessment: adding a provider module *after* assessing the codebase means that
module is the one part never assessed.

**Item 4 split in two, and 4a moved ahead of the split.** This contradicts what
this document said when item 4 was written — that all of item 4 comes after the
split, because a split invalidates architecture findings. That reasoning is
right about the *architecture* findings and wrong about the rest, and it misses
that **the split needs part of item 4 as its instrument**:

- 9c's definition of done is "no behaviour change at all, and the PR should say
  how that was established, not merely assert it." Today the only available
  evidence is *313 tests pass*. **There is no coverage measurement**, so nobody
  knows what fraction of a 2,584-line module those tests exercise — and the
  uncovered parts are exactly where a silent break from a mechanical move would
  hide. Measure coverage before anything moves, or the split's central claim
  cannot be evidenced.
- *How* to split — free functions, mixins, or by layer — is itself an
  architecture question. This document says elsewhere that guessing at best
  practices without written criteria is how a refactor becomes taste. The
  criteria should exist before the call is made, not after it.

So **4a** delivers: coverage measured and reported in CI, the ruff rule set
actually decided and configured, a type-checking decision, the testing audit,
and the written architecture criteria the split has to satisfy. **4b** is the
work those findings turn into.

**One caveat on staging ruff.** CI gates on *no new* `(file, rule)` pairs. Turn
on a stricter rule set in 4a and every pre-existing violation becomes a gate
failure on arrival. Decide and configure the rule set in 4a; enable gating
per rule as each is driven to zero in 4b.

---

## Item 1a, closed

Landed on `feat/bedrock-host-delegated`. The reading this document recorded held
up, and is now a test rather than a reading:

- **Verified, by test.** `us.anthropic.claude-sonnet-4-5-20250929-v1:0` resolves
  whole through `_parse_ref`, through the `|` fallback chain in
  `ModelRouter.complete` — which parses refs *itself*, in a second place a fix
  to the first would not have covered — through `SUPERVISOR_ROUTE_*`, and back
  through `ModelBinding.ref()`.
- **Verified, by test.** A complete host-delegated run, planning through report,
  passes with `build_provider`, `ModelRouter.complete` and
  `httpx.AsyncClient.__init__` all rigged to raise. The harness is genuinely not
  in the model path, so `CLAUDE_CODE_USE_BEDROCK` and the AWS credentials are
  the host's business entirely.
- **Verified, by test.** `"type": "bedrock"` still fails by name, so item 1b
  changes that deliberately rather than by accident.
- `README.md` now states which modes support Bedrock and which do not.

Each mechanism was disabled in turn to confirm the tests were not vacuous. All
seven go red under at least one sabotage, and the two colon-parsing sites redden
disjoint sets, which is what shows they are covered independently rather than
one standing in for the other.
