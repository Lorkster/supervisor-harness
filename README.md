# supervisor-harness

A supervising layer for agentic coding. It wraps **Claude Code** and **Cursor**
to add what a single agent pass does not give you: the task examined from
several angles at once, work you approve before it happens, agents that get
corrected when they wander, and a finish line that has to be *proven* rather
than announced.

The harness never claims something is done. It reports which criteria were
verified, with what evidence, and which were not.

The one idea underneath all of it: **the subject of a judgement may not set the
terms of it.** An agent proposes; policy disposes. What that means in practice —
and where each part of it lives in the code — is
[`docs/reasoning-control-plane.md`](docs/reasoning-control-plane.md).

```
                 ┌──────────── supervisor ────────────┐
   your task ──▶ │ plan → analyse → synthesise → you   │
                 │        ↑ drift    │ approve         │
                 │        │ correct  ▼                 │
                 │      execute → verify → checkpoint  │──▶ report +
                 │                          │          │    verified DoD
                 │                          ▼          │
                 │                      lessons ───────┼──▶ next run
                 └────────────────────────────────────┘
```

---

## What it actually does

**1. Analysis, from the angles that fit.** A deterministic scorer picks lenses
from the task — architecture, security, technical feasibility, quality, data,
performance, operations, UX, risk, prior art. A typo fix gets two lenses; an
OAuth integration leads with security. A planning model may sharpen the
objectives, but it cannot drop a lens your policy requires.

**2. Agents run in parallel and talk through the supervisor.** Every
agent-to-agent message is routed by the supervisor, which can annotate it,
broaden it, or turn it into a directive before the recipient wastes a turn.
Mechanically-detected contradictions between lenses are surfaced rather than
averaged away.

**3. Drift is caught and corrected, cheaply.** After every turn, deterministic
heuristics check for files touched outside scope, work on explicitly excluded
topics, the brief being restated instead of answered, repetition, empty turns,
and objectives left uncovered as the budget burns. Only when those fire does the
harness spend a model call on a second opinion — which is why it can watch
continuously. Correction comes before termination: an agent is refocused or
narrowed first, and stopped only if it drifts again or writes somewhere it was
forbidden.

**4. You decide what gets built.** Analysis produces execution tasks, each with
a concrete action, a motivation tied to a finding, and a definition of done. You
approve, modify, or reject each one. Nothing touches your code before that.

**5. Done means proven.** Criteria phrased so they cannot fail ("the code is
clean") are rejected at proposal time, and so are the ones that pass by running
nothing: a `pytest -k` or `go test -run` filter that selects no test still exits
0, so a criterion that filters a suite has to name node ids or state a minimum
selection. Policy inserts the test, security and code-quality bars where the
task admits them; a task whose point is a fence, a lock or a quota also has to
carry the negative test for the shape it exists to refuse, and one that touches
locking, retries or I/O has to show it still terminates in bounded time. A
criterion marked passed with no evidence is recorded as **failed**. The final
report shows the checklist.

**5b. Every finding is accounted for.** Each task names the findings it closes,
and the run ends with a reconciliation — finding by finding, fixed here,
attempted, still pending, or still open — written as its own artifact.

**5c. Agents build a shared record.** An analysis lens that establishes
something the others need — which store the counters live in, which entrypoint
is actually reachable — records it as a keyed fact with its evidence, and later
agents inherit it. Where two lenses key the same claim and say different things,
the disagreement is kept and surfaced rather than resolved by whoever wrote
last: it shows in the brief as open, in the report's conflicts, and in
`supervisor status`. A claim with no evidence is dropped.

**6. It learns.** Failures that better briefing would have prevented become
lessons, stored across runs and injected into future briefs for that role.

---

## Install

```bash
pip install -e .
```

Then, in each project you want supervised:

```bash
supervisor init
```

That detects your host, installs the skill or rules, adds the slash command, and
registers the MCP server in `.mcp.json`. Restart the host afterwards.

| Host | Installed |
| --- | --- |
| Claude Code | `.claude/skills/supervise/SKILL.md`, `.claude/commands/supervise.md` |
| Cursor | `.cursor/rules/supervisor.mdc`, `.cursor/commands/supervise.md` |
| Both | `.mcp.json` entry for the `supervisor` MCP server |

Use `--host both` to install for both, `--force` to overwrite.

### Several projects

`init` is per project because everything it writes is per project — your host
finds a skill, a slash command and an MCP server by looking in the repository it
has open. Run it in each one; nothing about the harness is bound to a single
project.

Every command takes `-w/--workspace`, so one installation drives any of them
from anywhere:

```bash
supervisor run "Review the payment path" -w ~/code/billing --mode report
supervisor runs -w ~/code/billing
```

What differs between projects is where the runs are kept:

| | Runs, lessons and the index live in | Use it when |
| --- | --- | --- |
| default | `<project>/.supervisor/` — one store per project | projects are unrelated, or you want a project's history to travel with it |
| `SUPERVISOR_HOME=~/.supervisor` | one store for every project | you want `supervisor runs` to answer across all of them, and lessons learned in one to reach the others |

Configuration layers either way: a shared home's `config.json` is trusted and
sets your defaults, and each project's own `supervisor.config.json` still tunes
policy and routing on top of it — within the trust boundary described under
[Which config files are trusted](#which-config-files-are-trusted).

One limit worth knowing before you rely on it. **A run is anchored to one
workspace root**: scope globs, the tool fence and the baseline commit every
criterion is measured against are all relative to it, so work spanning several
repositories is several runs, not one.

Under a shared `SUPERVISOR_HOME` the lessons library is shared too, which is the
point of it — a lesson learned in one project is worth having in the next. Each
lesson records where it was learned, and where it has since been relearned;
briefs say so (`learned here` / `learned in <project>`) and tell the agent that
a borrowed lesson is evidence rather than a rule. Lessons learned here outrank
borrowed ones of equal strength, and `policy.lesson_max_age_days` drops the
stale. Writers take an advisory lock, so concurrent runs in different projects
no longer lose a lesson between them.

---

## Using it

### From Claude Code or Cursor

```
/supervise Add rate limiting to the login endpoint so credential stuffing is blocked
```

or just ask it to supervise the work. The host runs the agents with its own
tools and permissions; the harness plans, watches and verifies.

### From the command line

```bash
supervisor run "Review src/auth for security problems" --mode report
supervisor status
supervisor explain
supervisor lessons
```

`run` drives everything itself against your configured models, which needs a
non-host route (see below). `--yes` approves every proposed task without asking;
use it only where you have accepted that trade.

`--mode` says what the run should produce, and `start` takes it too:

| `--mode` | The run ends with |
| --- | --- |
| `auto` (default) | whichever of the two synthesis judges the request to be asking for |
| `report` | the analysis: findings, disagreements between lenses, open questions |
| `execute` | execution tasks you approve or reject, then verified work |

### Driving the protocol yourself

The CLI exposes exactly what the MCP server does, so any host can drive it:

```bash
supervisor start "..." --json          # returns work packets
supervisor report <run> <agent> -i turn.json --json
supervisor advance <run> --json
supervisor approve <run> --all
supervisor resume                      # picks up where it stopped
```

### Contributing checks

```bash
python -m pytest -q
python -m ruff check .
```

The second is the lint gate CI runs, and it is a gate at **zero**. It was not
always: ruff went unconfigured until the rule set was chosen by measurement, and
the 67 findings that set produced were tolerated by a by-(file, rule) diff until
they were driven to zero. Widening the set later means taking the new rule to
zero in the same change rather than reintroducing a baseline — a permanent
backlog is not a standard.

Both `ruff` and `mypy` are pinned to a minor range for that reason: when a check
gates at zero, a new release that adds a rule turns an unrelated pull request
red, and upgrading should be a deliberate act whose findings someone reads.

### Every command

`--help` on any of them says the same as this table; nothing below is a
shorthand for something the CLI will not tell you itself.

| Command | What it does | Its own arguments |
| --- | --- | --- |
| `init` | install host integrations and an example config into a project | `--host claude\|cursor\|both` (default: whichever host is detected), `--force` to overwrite existing files |
| `run PROMPT` | drive a whole run to completion without a host | `--mode`, `--backend host\|autonomous`, `-y/--yes` |
| `start PROMPT` | begin a host-delegated run and print its first work packets | `--mode`, `--host-agents` — the subagent types you can spawn, as a JSON array: `'["general-purpose"]'`, or `'[{"name": "general-purpose", "description": "..."}]'` when you want the description to inform role matching |
| `report RUN AGENT` | hand back one agent's result | `-i/--input` a JSON file, or `-` for stdin (the default) |
| `advance [RUN]` | move a run to its next phase once its packets are reported | — |
| `abandon AGENT [RUN]` | give up on an agent that will never report | `--reason`, recorded on the run's log |
| `approve [RUN]` | decide on proposed tasks | `--all`, or `--task ID[:approve\|reject\|defer]`, repeatable |
| `resume [RUN]` | continue an interrupted run from its event log | — |
| `status [RUN]` | show one run in detail: phase, agents, drift, criteria | — |
| `drift AGENT [RUN]` | ask the drift model for a second opinion on one agent's last turn | — |
| `events [RUN]` | print a run's event log, including its diagnostic notes | `-t/--type` one type (`note`, `unknown`, …), `--since SEQ` |
| `runs` | list recent runs in this store | `-n/--limit` (default 20) |
| `lessons` | show what previous runs taught the harness | `-t/--target` a role id, `supervisor`, `dod` or `*`; `-n/--limit` |
| `providers` | show stage routing and whether each provider answers | — |
| `reindex` | rebuild `index.sqlite3` from the event logs | — |
| `mcp` | run the MCP server on stdio; `.mcp.json` starts the same server through the `supervisor-mcp` entry point | — |

Every command also takes `-w/--workspace`, `--json` and `--debug`, before or
after the subcommand; `supervisor --version` prints the version.

| Flag | Effect |
| --- | --- |
| `-w`, `--workspace` | which project to act on (default: the working directory) |
| `--json` | machine-readable output, which is what a host driving the protocol wants |
| `--debug` | let an unexpected failure raise with its traceback, instead of printing one line |

**`RUN` is optional wherever it appears in brackets.** Omitted, it means the
most recent run in the store — the workspace's own `.supervisor/`, or the
shared one if you set `SUPERVISOR_HOME`. That is what makes
`supervisor status`, `supervisor approve --all` and `supervisor resume` work
with no arguments at all.

Two defaults worth stating, because both decide something on your behalf.
`run` flips to the autonomous backend when your config routes to the host,
since a bare CLI run has no host to delegate to — pass `--backend host` if you
meant it. And `approve --task` states the whole decision, not part of it: every
proposed task you do not name is **rejected**.

---

## Choosing models per stage

Routing is per **stage**, with fallbacks. A cheap local model can watch for
drift while a strong hosted model does the architecture pass.

```jsonc
// supervisor.config.json
{
  "routing": {
    "default": "host",                                   // Claude Code / Cursor runs it
    "analysis.security": "openrouter:anthropic/claude-opus-4.1|host",
    "drift": "ollama:qwen3.8-code:latest|host",          // local, called constantly
    "improvement": "ollama:qwen3.8-code:latest"
  }
}
```

A stage falls back to its parent and then to `default`, so
`analysis.architecture` resolves through `analysis` to `default`. `|` separates
fallbacks, tried in order when a provider fails.

| Provider | Set up with |
| --- | --- |
| `host` | Nothing — the packet is handed to Claude Code or Cursor |
| `ollama` | A running Ollama; optionally `OLLAMA_HOST` |
| `openrouter` | `OPENROUTER_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `bedrock` | `pip install 'supervisor-harness[bedrock]'` and an AWS region — see below |

A model id may contain a colon — `us.anthropic.claude-sonnet-4-5-20250929-v1:0`
is one identifier, not a provider and a model. A route splits on its **first**
colon only, so the id arrives whole.

### Amazon Bedrock

| Mode | Bedrock | |
| --- | --- | --- |
| **Host-delegated** (default) | **Works, with nothing to configure here** | Claude Code reads `CLAUDE_CODE_USE_BEDROCK`, `AWS_REGION` and your AWS credentials, and runs every packet itself. The harness is not in the model path at all — no provider is constructed, no HTTP client is opened. |
| **Autonomous** | **Works, with the optional extra installed** | See below. |
| **Autonomous**, via an Anthropic-compatible gateway in front of Bedrock | Should work; not covered by the suite | Point the `anthropic` provider's `base_url` at the gateway. `base_url` is one of the settings a workspace config file may not set — put it in your trusted home config. |

If you drive the harness from Claude Code and Claude Code is on Bedrock, **you
are already running on Bedrock** and need none of what follows.

### Autonomous Bedrock

```bash
pip install 'supervisor-harness[bedrock]'
```

An optional extra rather than a runtime dependency: it pulls the Anthropic SDK,
boto3 and botocore, and the package otherwise has exactly one dependency —
which is worth keeping for everyone not using Bedrock. Nothing imports the SDK
unless a `bedrock` provider is actually configured.

```jsonc
// ~/.supervisor/config.json  — see "Which config files are trusted"
{
  "providers": {
    "bedrock": { "type": "bedrock", "region": "eu-west-1" }
  },
  "routing": {
    "default": "bedrock:us.anthropic.claude-sonnet-4-5-20250929-v1:0"
  }
}
```

Credentials are resolved by the normal AWS chain — environment, shared config,
SSO, IMDS, assumed roles — so a machine already set up for Bedrock needs
nothing but the region, and `AWS_REGION` supplies even that. Add
`"profile": "..."` to pin a named profile.

Two things worth knowing:

- **`region` and `profile` are settings a workspace config file may not set**,
  alongside `base_url` and the API keys. Both decide where a credentialed
  request goes and which identity signs it, and the AWS chain resolves an
  identity from the environment — so a repository able to set them could
  redirect your traffic or assume a different profile without ever naming a
  secret. Put them in your trusted home config.
- **Model ids are inference profiles**, like
  `us.anthropic.claude-sonnet-4-5-20250929-v1:0` — usually what an account is
  entitled to invoke, and the trailing `:0` is part of the id.

`supervisor providers` reports whether the extra is installed and whether a
region resolved, so a misconfiguration shows up before a run rather than
during one.

Check what resolves where:

```bash
supervisor providers
```

Environment overrides work for one-off runs:
`SUPERVISOR_ROUTE_ANALYSIS=ollama:qwen3.8-code:latest`.

### Which config files are trusted

Config layers merge with later files winning, but not every layer is trusted with
everything. Files under your home directory (or an explicit `SUPERVISOR_HOME`)
are trusted — you put them there. Files **inside the workspace** are not, because
the workspace is often a repository someone else wrote and you have merely
pointed the harness at.

A workspace file may tune how the harness thinks — policy thresholds, routing,
budgets. It may not set:

| Setting | Why |
| --- | --- |
| `policy.allow_command_execution` | It would grant shell execution by being checked out |
| `providers.*.base_url` | It would redirect where your API key is sent |
| `providers.*.api_key`, `.api_key_env`, `.type` | Same |
| `providers.*.region`, `.profile` | They decide where an AWS-credentialed request goes and which identity signs it |
| `home` | It would redirect where run history is written |

Anything rejected is reported by `supervisor providers` rather than silently
dropped. This closes a real hole: without the split, cloning a repository that
happens to contain a `supervisor.config.json` was enough to turn on command
execution and post your `ANTHROPIC_API_KEY` to someone else's host.

---

## The two backends

Both run the same supervision path: every reported turn is recorded, assessed for
drift, answered with a directive, and has its messages routed. Two differences
remain, and they follow from the backend rather than being oversights:

- Escalating a drift suspicion to a model needs the harness to make a model call,
  so it is skipped when the `drift` stage is itself routed to `host`.
- Tool use, wall-clock budgets and failure capture apply only to agents the
  harness drives. A host-run agent uses the host's tools and fails in the host's
  own way.

**Host-delegated** (default). The harness emits work packets; Claude Code or
Cursor runs them with its own tools, under your own permission model, and
reports each turn back. Nothing runs that your host would not have run.

**Autonomous.** The harness drives models directly through a
workspace-sandboxed toolset (`list_files`, `read_file`, `search`, and
`write_file` for execution agents). Reads cannot escape the workspace; writes
are additionally confined to the agent's declared scope. Shell execution is
**off** unless you set `policy.allow_command_execution`, because in delegated
mode that decision belongs to your host.

Turning it on adds `run_command` for execution agents, and that one is a fence
rather than a sandbox — worth reading before you enable it. **Every** agent may
run only the project's own check runners (`pytest`, `npm`, `make`, …), may not
use shell metacharacters or globs, and may not hand a runner its program inline
(`python -c`, `node -e`). Paths named on the command line must fall inside the
agent's scope; an agent that declared none is held to the workspace, which is
what an empty scope already meant to `write_file`.

That last part used to work the other way round. The fence applied only to an
agent that *had* a scope, on the reasoning that there was nothing to check a
path against — but three of those four rules are not about paths, and a scope is
supplied by a model, so "no scope" is a state a model can cause by saying
nothing. The least specified agent in a run held the widest shell in it.

Making it universal costs `git status`: git is not a check runner, and it cannot
be narrowed to its read-only subcommands by name, because
`git -c alias.s='!sh -c …' s` runs anything at all. No agent can see its own diff
through the harness's shell. Through your host's own tools, in delegated mode,
it can.

A check runner still runs whatever the project tells it to: `npm test` runs a
line of `package.json` and `make` runs the Makefile, either of which can write
anywhere. It is built to keep a drifting agent inside its scope, not to contain
a hostile one — if the workspace's own build scripts are untrusted, run the
harness in a container.

**The run envelope.** A scope is enforced well; what used to be missing was any
bound on where one came from. Every scope in a run was proposed independently by
a model -- the planner's for an analysis lens, the synthesis model's for an
execution task -- and no two were ever compared, so a model could scope a task to
anywhere in the workspace and approval was per-task with nothing above it.

Each run now has an envelope: the union of what that run may modify, fixed
before any task exists. `policy.scope_envelope` sets it (empty means the whole
workspace) and the plan may narrow it further; nothing widens it. Every agent's
scope is attenuated at spawn to the envelope, to its task's scope, and to its
spawner's where there is one -- so a verifier cannot be handed a wider fence than
the work it is judging. A scope that exceeds its ceiling is narrowed to the
intersection rather than refused, and the narrowing is recorded: on the log, in
the notes the user reads at approval, and in `supervisor status`.

**Reading the decisions back.** `supervisor status` says where a run is now.
`supervisor explain` says how it got there: for each agent, in order, every turn
it took, the drift assessment and signals that turn produced, any second opinion
a model was asked for, the inbox it was handed, the directive it was issued and
the rationale behind it -- and, before its first turn, whatever its scope was
narrowed to and why. `-a <agent-id>` narrows it to one agent, `--json` gives the
same thing structured, and the MCP tool `supervisor_explain` serves it to a host.

It is assembled from the event log rather than the state snapshot, because the
snapshot does not keep enough: `RunState.drift` is keyed by agent, so it holds
only each agent's most recent assessment, and an assessment that has been
overwritten cannot explain the directive it produced.

**A grant has a shelf life.** The envelope records when it was granted, and a
run resumed more than `policy.envelope_max_age_days` later (7 by default, 0 to
disable) pauses before it spawns an execution agent and asks you to re-grant it:
`supervisor approve --renew-envelope`. Analysis and reporting continue freely
and nothing already established is lost — only writing waits. It is the same
rule as "nothing touches your code before you approve it", applied to consent
that has gone stale rather than to consent that was never given. Renewal renews
the date, never the paths.

Duration is bounded per agent by its budget — turns, tokens, seconds and tool
calls — rather than by the scope. Putting a clock inside the write fence would
make the fence behave differently on a slow machine, and `core/tools.py` refuses
only on facts that do not change under load.

Approving a task cannot widen the envelope. A `scope_paths` edit at approval is
clamped like any other scope, because a bound that a per-task decision can move
is only ever as strong as the most permissive task anyone approved. Widen it by
starting the run with a wider envelope, which is visible from the beginning.

One refusal applies to every agent, scoped or not, because it is not about a
path: no command may change the working tree's git state. `git stash`,
`git checkout`, `git clean`, `git reset`, `git rebase` and their relatives act
on the whole tree at once, and the agents in a run share that tree -- a stash
taken for one agent's clean baseline can destroy another's half-written file.
Every brief says so as well, since in host-delegated mode your own permission
model, not the harness, is what can actually refuse the command.

Tool rounds do not consume an agent's turn budget — reading three files to
answer one question is one piece of work, not three.

---

## Persistence and resumption

Everything lives under `.supervisor/` in your workspace, or in one shared store
if you set `SUPERVISOR_HOME` (see [Several projects](#several-projects)):

```
runs/<run_id>/events.jsonl    append-only, authoritative
runs/<run_id>/state.json      derived snapshot, for fast status reads
runs/<run_id>/artifacts/      report.md, reconciliation.md, agent output
lessons.jsonl                 cross-run lessons library
index.sqlite3                 derived, rebuildable with `supervisor reindex`
```

The event log is the source of truth: every turn, directive, drift assessment,
message, decision and verification is an event. Run state is a fold over that
log, so an interrupted run resumes with its findings, tasks and verification
intact — including in a different session.

Because the reasoning is on disk, you can ask cross-run questions:

```sql
-- which roles drift most?
SELECT role, AVG(drift_score) FROM agents GROUP BY role ORDER BY 2 DESC;
-- which verification methods actually prove things?
SELECT method, status, COUNT(*) FROM criteria GROUP BY method, status;
```

---

## Policy

Tuning lives in `supervisor.config.json` under `policy`:

| Setting | Default | What it controls |
| --- | --- | --- |
| `max_parallel_agents` | 4 | Concurrency in autonomous mode |
| `default_max_turns` | 6 | Analysis agent budget |
| `drift_threshold` | 0.45 | Score at which a correction is issued |
| `drift_hard_threshold` | 0.8 | Score at which a repeat offender is stopped |
| `model_drift_check` | true | Escalate suspected drift to the drift model |
| `checkpoint_threshold` | 0.75 | Score needed to pass the quality gate |
| `max_checkpoint_iterations` | 3 | Remediation rounds before giving up |
| `require_tests` | true | Insert a mandatory test criterion |
| `require_security_review` | true | Force a security lens and criterion |
| `require_code_quality` | true | Insert a mandatory convention criterion |
| `require_negative_test` | true | Demand the rejected case on a fence or guard task |
| `require_liveness_review` | true | Demand a bounded-time proof on locking, retry or I/O |
| `min_dod_criteria` | 2 | Reject thinner definitions of done |
| `max_unreported_dispatches` | 3 | Packets to a silent host agent before abandoning it |
| `agent_timeout_seconds` | 0 | Wall-clock bound on the same silence; 0 disables |
| `allow_command_execution` | false | Let the harness run commands itself |
| `apply_lessons` | true | Inject past lessons into briefs |

---

## MCP tools

| Tool | Purpose |
| --- | --- |
| `supervisor_start` | Begin a run; returns the first work packets |
| `supervisor_report` | Hand back one agent's result; returns a directive |
| `supervisor_advance` | Move to the next phase once packets are reported |
| `supervisor_abandon` | Give up on an agent whose sub-agent crashed or was cancelled |
| `supervisor_approve` | Record the user's decisions on proposed tasks |
| `supervisor_status` / `supervisor_runs` | Inspect runs |
| `supervisor_resume` | Continue an interrupted run |
| `supervisor_check_drift` | Second opinion on an agent that looks off-brief |
| `supervisor_lessons` | What previous runs taught the harness |
| `supervisor_providers` | Stage routing and provider health |

---

## Layout

```
src/supervisor_harness/
  models.py        domain types; everything persisted is here
  contracts.py     JSON schemas every stage answers in, and their parsers
  config.py        layered config, per-stage routing, policy
  store/           event log, fold, snapshots, SQLite projection
  providers/       openrouter, ollama, anthropic, host delegation, routing
  agents/          roles and lens selection, host-agent discovery, briefs
  core/
    supervisor.py  the state machine driving a run
    phases.py      prompts and pure transformations per phase
    drift.py       heuristics, escalation, the directive ladder
    dod.py         criteria validation, quality bars, verification
    blackboard.py  shared context, message routing, contradiction detection
    tools.py       sandboxed workspace tools for autonomous agents
    baseline.py    the commit a run measures its whole-repository checks against
  mcp_server.py    MCP surface
  cli.py           command line
```

---

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests -q
```

The suite covers both backends end to end against a fake provider, so
orchestration is tested rather than a model's mood: the full lifecycle, host
delegation, resumption from the event log, drift correction inside a live run,
definition-of-done enforcement, and the lessons library.

`tests/test_hardening.py` holds regressions for defects the harness found while
reviewing its own source. They are worth reading as a list of the mistakes this
design invites — a verification check that ignored exit codes, an event log that
handed out duplicate sequence numbers while it was small, scope matching that
read absolute paths as out-of-scope, and state that lived only in one process's
memory.

---

## Documentation

| | |
| --- | --- |
| [`docs/reasoning-control-plane.md`](docs/reasoning-control-plane.md) | **What the harness is.** The four dimensions of the design, each pointing at the code that implements it, and what each one deliberately does not do. |
| [`docs/protocol.md`](docs/protocol.md) | The wire protocol between the harness and the host. |
| [`docs/shared-context-spec.md`](docs/shared-context-spec.md) | Shared semantic context in full: the design, its decided choices, and its open ones. |
| [`docs/quality-assessment.md`](docs/quality-assessment.md) | The standard this codebase is held to, what it measures against it, and the findings still open. |
| [`docs/remediation-plan.md`](docs/remediation-plan.md) | The history — what a review of this codebase found, what was fixed, and why each call was made. |
| [`docs/next-three.md`](docs/next-three.md) | What is scheduled next, and what has already been decided. |

Documents that cite code by line number are checked in CI
(`tools/check_doc_refs.py`), so a reference that stops pointing at what it
claims fails the build rather than quietly misleading a reader.

---

## When not to use it

For a quick question or a one-line fix, just let your agent do the work. This
costs several parallel agents and a round of your attention on approval. That is
worth it for substantial or risky changes, and wasteful for a typo.

## Licence

MIT
