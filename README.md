# supervisor-harness

A supervising layer for agentic coding. It wraps **Claude Code** and **Cursor**
to add what a single agent pass does not give you: the task examined from
several angles at once, work you approve before it happens, agents that get
corrected when they wander, and a finish line that has to be *proven* rather
than announced.

The harness never claims something is done. It reports which criteria were
verified, with what evidence, and which were not.

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

**6. It learns.** Failures that better briefing would have prevented become
lessons, stored across runs and injected into future briefs for that role.

---

## Install

```bash
pip install -e .
```

Then, in the project you want supervised:

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
supervisor lessons
```

`run` drives everything itself against your configured models, which needs a
non-host route (see below). `--yes` approves every proposed task without asking;
use it only where you have accepted that trade.

### Driving the protocol yourself

The CLI exposes exactly what the MCP server does, so any host can drive it:

```bash
supervisor start "..." --json          # returns work packets
supervisor report <run> <agent> -i turn.json --json
supervisor advance <run> --json
supervisor approve <run> --all
supervisor resume                      # picks up where it stopped
```

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
rather than a sandbox — worth reading before you enable it. A scoped agent may
run only the project's own check runners (`pytest`, `npm`, `make`, …), may not
use shell metacharacters or globs, may not name a path outside its scope, and
may not hand a runner its program inline (`python -c`, `node -e`). But a check
runner still runs whatever the project tells it to: `npm test` runs a line of
`package.json` and `make` runs the Makefile, either of which can write anywhere.
It is built to keep a drifting agent inside its scope, not to contain a hostile
one -- if the workspace's own build scripts are untrusted, run the harness in a
container. An agent with no declared scope has no fence at all.

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

Everything lives under `.supervisor/` in your workspace (or `SUPERVISOR_HOME`):

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

## When not to use it

For a quick question or a one-line fix, just let your agent do the work. This
costs several parallel agents and a round of your attention on approval. That is
worth it for substantial or risky changes, and wasteful for a typo.

## Licence

MIT
