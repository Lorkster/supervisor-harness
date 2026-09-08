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

A claim is also checked against the tree it was made about. An agent that names
the file its claim concerns has that file's state recorded alongside it, and a
later agent inheriting the claim is told when the file has been edited or
removed since — so the statement reads as the writer's snapshot rather than as
the current state. Resolution is strict path lookup and never evaluation: a fact
is written by a model onto a board other agents read.

**6. It learns, and forgets.** Failures that better briefing would have
prevented become lessons, stored across runs and injected into future briefs for
that role. At the end of each run the library is consolidated: rows saying the
same thing are folded together, a lesson this run re-learned has its clock
reset, and one that has gone unconfirmed for long enough loses confidence and is
eventually retired — kept, with the reason, so that "never learned" and "learned
and later judged stale" stay different facts. The clock is **runs since last
confirmed** in a workspace that knows the lesson, not wall-clock age: a lesson
about a subsystem nobody has touched is not less true for having been left
alone, and a run in a project that never learned it is not evidence about it.

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
| Claude Code | `.claude/skills/supervise/SKILL.md`, `.claude/commands/supervise.md`, and the `supervisor` server in `.mcp.json` |
| Cursor | `.cursor/rules/supervisor.mdc`, `.cursor/commands/supervise.md`, and the same server in `.cursor/mcp.json` |

Two MCP files because the two hosts look in different places: Claude Code reads
`.mcp.json` at the repository root, Cursor reads `.cursor/mcp.json`. Both are
merged into rather than overwritten, so servers you already had are kept.

Use `--host both` to install for both. For a worked two-host setup, see
[`docs/setup-examples.md`](docs/setup-examples.md).

### Updating

`supervisor init` is also the update procedure. Run it again after upgrading:

```bash
pip install -U -e .
supervisor init
```

It refreshes the skill, the rules, the slash commands and the `supervisor` MCP
entry whenever the package ships a newer one, reports each as `(updated)`, and
says `Nothing to do` when your project is already current. Restart the host
afterwards.

Those files are not your configuration — they are the harness's instructions to
your host, and a release that changes the protocol changes them with it. A copy
left behind goes on driving the old protocol: nothing fails, the run just costs
several times what it should. `pip install -U` cannot fix that on its own,
because the stale copy is in your project.

Two things are never refreshed:

- **`supervisor.config.json`** is yours. `--force` replaces it with the example;
  nothing else touches it.
- **Anything you have deliberately edited**, if you pass `--keep-integrations`.
  That copy then stops tracking the package, so no later release will correct
  it for you.

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

The five CI runs, in the order they are cheapest to fix:

```bash
python -m ruff check .                                    # lint, and complexity
python -m mypy                                            # strict, zero errors
python tools/check_doc_refs.py                            # the docs still describe this code
python -m pytest -q                                       # the suite, on 3.11-3.14 × two OSes
python -m pytest -q --cov=supervisor_harness --cov-fail-under=92   # the floor
```

Each is a gate at its target rather than at a tolerated baseline: zero lint
findings, zero type errors, zero stale documentation references, and a coverage
floor that only ever rises. Lint was not always one — ruff went unconfigured
until the rule set was chosen by measurement, and the 67 findings that set
produced were tolerated by a by-(file, rule) diff until they were driven to
zero. Widening the set later means taking the new rule to zero in the same
change rather than reintroducing a baseline — a permanent backlog is not a
standard.

Both `ruff` and `mypy` are pinned to a minor range for that reason: when a check
gates at zero, a new release that adds a rule turns an unrelated pull request
red, and upgrading should be a deliberate act whose findings someone reads.

### Every command

`--help` on any of them says the same as this table; nothing below is a
shorthand for something the CLI will not tell you itself.

| Command | What it does | Its own arguments |
| --- | --- | --- |
| `init` | install host integrations and an example config, or refresh them after an upgrade | `--host claude\|cursor\|both` (default: whichever host is detected), `--force` to also replace `supervisor.config.json`, `--keep-integrations` to leave edited files alone |
| `run PROMPT` | drive a whole run to completion without a host | `--mode`, `--backend host\|autonomous`, `-y/--yes` |
| `start PROMPT` | begin a host-delegated run and print its first work packets | `--mode`, `--host-agents` — the subagent types you can spawn, as a JSON array: `'["general-purpose"]'`, or `'[{"name": "general-purpose", "description": "..."}]'` when you want the description to inform role matching |
| `report RUN AGENT` | hand back one agent's result | `-i/--input` a JSON file, or `-` for stdin (the default) |
| `advance [RUN]` | move a run to its next phase once its packets are reported | — |
| `abandon AGENT [RUN]` | give up on an agent that will never report | `--reason`, recorded on the run's log |
| `approve [RUN]` | decide on proposed tasks | `--all`, or `--task ID[:approve\|reject\|defer]`, repeatable; `--renew-envelope` to re-grant a scope envelope that has gone stale |
| `resume [RUN]` | continue an interrupted run from its event log | — |
| `status [RUN]` | show one run in detail: phase, agents, drift, criteria | — |
| `explain [RUN]` | how the run got here: every turn, its drift signals, and the directive each one drew | `-a/--agent` one agent, `--width COLS` |
| `drift AGENT [RUN]` | ask the drift model for a second opinion on one agent's last turn | — |
| `events [RUN]` | print a run's event log, including its diagnostic notes | `-t/--type` one type (`note`, `unknown`, …), `--since SEQ` |
| `trajectory [RUN]` | export the run as a portable trajectory document | `-o FILE` |
| `audit [RUN]` | what a finished run's agents actually did, as against what they were allowed to do | `-o NAME` for the evidence filename |
| `runs` | list recent runs in this store | `-n/--limit` (default 20) |
| `lessons` | show what previous runs taught the harness, including the ones it has retired | `-t/--target` a role id, `supervisor`, `dod` or `*`; `-n/--limit`; `--live-only` to hide retired ones |
| `providers` | show stage routing and whether each provider answers | — |
| `reindex` | rebuild `index.sqlite3` from the event logs | — |
| `delete [RUN]` | **remove runs from disk** and their rows from the index | a run id, or `--older-than DAYS`; `--keep-last N` never deletes the N most recent (default 5) |
| `prune-lessons` | drop lessons the library has not seen for a while | `--older-than DAYS` (default 180) |
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

A packet carries **paths, not text**: the brief, the answer's JSON schema and
the file to write the answer to, plus a few lines naming the job. The sub-agent
reads the brief and writes its answer; the orchestrator holds neither. That is
not tidiness — a supervised run makes the orchestrator the message bus, so
without it every brief and every result crosses one context, and an analysis
fan-out costs about nine times what it needs to. Set `inline_briefs` to put the
full text back in the packet for a host that cannot read files.

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
runs/<run_id>/progress.ndjson derived, tailable: one short line per event
runs/<run_id>/packets/        the briefs handed out, and the run's RULES.md
runs/<run_id>/contracts/      the answer schemas, written once per run
runs/<run_id>/results/        the answers agents wrote back
runs/<run_id>/artifacts/      report.md, reconciliation.md, agent output
lessons.jsonl                 cross-run lessons library
index.sqlite3                 derived, rebuildable with `supervisor reindex`
```

The event log is the source of truth: every turn, directive, drift assessment,
message, decision and verification is an event. Run state is a fold over that
log, so an interrupted run resumes with its findings, tasks and verification
intact — including in a different session.

### Watching a run

A supervised run is many sequential sub-agents, and the harness only wakes when
it is called, so it cannot push progress to you. Two things it can do instead.

Every response carries a one-line **ledger**, which the host is told to print
before it dispatches:

```
analyzing | 2/4 agents done | turn 9/24 | 11 finding(s) | 4m20s | 2 out, longest 1m10s
```

That last clause is the one that matters: a dispatched agent that has not
answered is the difference between a run that is slow and one that is stuck.

And every event leaves a short line in `progress.ndjson`, so you can follow a
run from another terminal without touching the harness:

```bash
tail -f .supervisor/runs/<run_id>/progress.ndjson
```

### Auditing a run

```bash
supervisor audit <run_id>
```

The envelope and the fence answer one question, at issue time: what may this
agent touch. In host-delegated mode — the default — they are not even the
enforcement, because your host runs the work under your own permission model and
the harness sees only what comes back. So the run with the least enforcement had
no way to be asked, afterwards, what its agents actually *did*.

Six scanners, all deterministic, none of them asking a model anything: writes an
agent reported outside its own scope, writes outside the run's envelope, criteria
whose recorded command is one no agent may run, passes whose evidence never
mentions the command it is supposed to be the output of, findings closed by tasks
that touched none of the files they cite, and — where there is a baseline commit
and a git repository — files the tree shows changed that no agent claimed.

The method matters more than any single check, and is borrowed from NVIDIA's
red-team scanners: **classify the action and its result, never the surface that
advertised the capability.** The trap here is sharper than it was there, because
every brief this harness writes contains the prohibited git commands in full — a
scanner that searched briefs would report every agent in every run, at total
confidence, for having read its instructions. So a brief, a scope and a task's
action are never evidence. Only what an agent reported doing, and what a
criterion recorded as run, are read.

A scanner that could not run says so in the report rather than being omitted: a
silent scanner and a clean one look identical, and only one of them is
reassuring. The evidence is written under the run, and the command exits non-zero
when there is something to look at, so it can gate a pipeline.

### Exporting a run

```bash
supervisor trajectory <run_id> -o run.json
```

One JSON document: the supervisor's steps, and each agent's nested beneath it
with its own id. It is a projection over the event log — nothing is recorded for
it, and running it changes nothing.

Two properties make it worth having rather than just being the log again. **A
deterministic step says so and carries no model metrics**, so `policy_steps`
against `model_steps` says how much of a run a model was asked for at all — the
claim that continuous drift-watching is cheap, as a number rather than an
argument. And **a step that repeats earlier work names the step it repeats**, so
a packet re-issued to an agent that never answered is not counted twice by
whatever consumes the document.

Both are checked by a validator rather than left as documentation, and the
command reports any breach and still writes the file: a document that violates
an invariant is evidence about the build, and withholding it leaves you with
nothing to send anyone.

The shape is adapted from NVIDIA's ATIF trajectory format, whose nesting is the
one a supervised run already has. It is deliberately not ATIF itself — the names
are this project's, and pinning to another project's evolving version would buy
compatibility with consumers that do not exist yet.

`supervisor status` adds two things worth reading afterwards. **`timing`** says
where the wall clock went — total, busy, idle, agent-seconds, concurrency, per
phase, per kind, and the slowest dispatch — folded from the log's own timestamps
rather than recorded separately. Busy and agent-seconds are separate numbers on
purpose: dispatches overlap, so their sum measures effort and only their union
measures wall clock, and dividing one by the other says how many agents were
really running at once. **`assists`** counts what the harness had to repair
before each answer could be used: JSON dug out of prose, a list boxed into an
object, a dependency named by title and resolved to an id. Those repairs are all
the right behaviour, and they were all invisible; a run where the harness fixed
forty answers is not the run the report otherwise describes, and it is the
sharpest signal there is about a provider.

Where a run's data cannot leave the machine it happened on, there is a tool that
reads the log and prints **only** durations, counts, phase names and agent
kinds:

```bash
python tools/where_the_time_went.py <workspace>/.supervisor/runs/<run_id>/events.jsonl
```

```
by phase                 elapsed      busy      idle   agent-s   conc
  analyzing              1391.6s   1364.8s     26.8s   1364.8s   1.00
  synthesizing            447.6s    446.8s      0.8s    446.8s   1.00
```

`conc` is the column to read first: the average number of agents actually
running at once. Four independent lenses reporting `conc 1.00` ran one after
another, whatever the dispatch asked for — `busy` equal to `agent-s` is the same
fact stated twice.

No prompt, no findings, no task titles, no file paths — the output is safe to
read out loud, which is the point of it. Standard library only and no import of
this package, so it also runs on a machine with an older build, or none. The
`idle` column is what it is for: a phase close to the dispatches inside it was
busy, and one far above them was waiting.

Its companion answers the other question — not why a run was slow but why it
stopped moving:

```bash
python tools/where_the_turns_went.py <workspace>/.supervisor/runs/<run_id>/events.jsonl
```

```
by agent            sent   turns  repeat  said-done  status      directives
  analysis#1           6     6/6        5          0  stopped     continue x6
  analysis#2           6     6/6        0          0  stopped     refocus x2, continue x4
  execution#1          3     0/10       0          0  unknown     -
```

Three different stalls, one table. `analysis#1` spent a six-turn budget on one
answer reported six times — an orchestrator that lost track of what it had
already handed back, which is what a context compaction does to one. `analysis#2`
did six turns of real work and was never accepted. `execution#1` was handed the
same packet three times and never answered at all, which costs no budget and so
appears nowhere else.

Same promise as the timing tool, and one more: an agent's `role` is written by
the planning model out of your prompt, so the table shows kinds and ordinals
(`analysis#1`) and never roles or ids. Turn contents are hashed to count
repeats and never printed.

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
| `lesson_decay_after_runs` | 10 | Runs a lesson may go unconfirmed before its confidence falls |
| `lesson_decay_per_run` | 0.05 | How much it falls per run past that |
| `lesson_confidence_floor` | 0.15 | Below this a lesson is retired — kept, with the reason, but not briefed |

One setting sits beside `policy` rather than inside it, because it decides how
a packet is carried rather than how hard the supervisor pushes back:

| Setting | Default | What it controls |
| --- | --- | --- |
| `inline_briefs` | false | Put the whole brief and schema in the packet instead of pointing at them. For a host that cannot read files. |

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
| `supervisor_explain` | The decision journal: every turn, its signals, and the directive it drew |
| `supervisor_lessons` | What previous runs taught the harness |
| `supervisor_providers` | Stage routing and provider health |

---

## Layout

```
src/supervisor_harness/
  models.py        domain types; everything persisted is here
  contracts.py     JSON schemas every stage answers in, and their parsers
  config.py        layered config, per-stage routing, policy
  serde.py         dataclasses to JSON and back
  ids.py           identifiers, timestamps, ages
  store/           event log, fold, snapshots, SQLite projection, redaction
  providers/       openrouter, ollama, anthropic, bedrock, host delegation, routing
  agents/          roles and lens selection, host-agent discovery, briefs
  host/            which host is driving, and what it can spawn
  integrations/    the files `supervisor init` writes into a project
  core/
    supervisor.py  the state machine driving a run
    lifecycle.py   an agent's life: spawned, attenuated, statused, abandoned
    packets.py     briefs, work packets, and the directive carried into the next turn
    supervision.py recording a turn, assessing it, answering it
    reporting.py   status, artifacts, the final report and the reconciliation
    responses.py   the packet and response types the protocol hands back
    phases.py      prompts and pure transformations per phase
    drift.py       heuristics, escalation, the directive ladder
    dod.py         criteria validation, quality bars, verification
    envelope.py    the run's scope grant, and attenuation down the delegation chain
    facts.py       whether a fact one agent established is still true when another reads it
    blackboard.py  shared context, message routing, contradiction detection
    consolidate.py keeping the lessons library worth reading: merge, decay, retire
    journal.py     the decision journal `supervisor explain` renders
    tools.py       sandboxed workspace tools for autonomous agents
    paths.py       path normalisation and scope matching
    baseline.py    the commit a run measures its whole-repository checks against
    audit.py       what a finished run's agents actually did, from the record
    timing.py      where a run's wall clock went, folded from the log
    trajectory.py  a run exported as a portable, validated document
  assists.py       what the harness had to repair before an answer could be used
  mcp_server.py    MCP surface
  cli.py           command line
```

That listing is checked in CI (`tools/check_doc_refs.py`): a module added or
moved without touching it fails the build. It described the pre-split package
for four batches before anyone noticed, which is the argument for the check
rather than for more care.

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
| [`docs/architecture.md`](docs/architecture.md) | **How a run works, drawn.** The phase machine including its failure paths, what is written where and what survives a crash, where the two backends diverge, and how the fence narrows. |
| [`docs/setup-examples.md`](docs/setup-examples.md) | Worked setups end to end, including two hosts on one machine with different credentials, and what to add for autonomous runs. |
| [`docs/protocol.md`](docs/protocol.md) | The wire protocol between the harness and the host. |
| [`docs/quality-standard.md`](docs/quality-standard.md) | **The standard this codebase is held to.** Ten criteria, the CI gates that enforce each, and when a new gate is worth adding. |
| [`docs/development-plan.md`](docs/development-plan.md) | **The work that is scheduled.** Nine batches: three from what a real run cost in latency, context and unused local agents, and six adapted from a reading of another agent framework. Says what is verified, what is only a reading, and what was decided against. |
| [`docs/history/`](docs/history/) | **Closed records.** The self-review that found the defects and how they were closed, the plan the work followed, the quality assessment that set the standard, and the design pass for shared context. Kept for the reasoning; nothing in there describes the harness as it is now. |

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
