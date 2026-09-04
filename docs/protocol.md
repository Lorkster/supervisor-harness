# The host protocol

This document specifies the contract between the harness and a host. Claude Code
and Cursor are supported out of the box; anything that can call MCP tools (or the
CLI's `--json` mode) can drive the same protocol.

The division of labour is fixed:

- **The harness** decides what should be examined, writes the briefs, watches for
  drift, decides what is done, and persists everything.
- **The host** executes briefs with its own tools, its own repository access and
  its own permission model, and reports honestly what came back.

The harness never executes anything on the user's behalf in this mode. The host
never decides whether work is finished.

---

## State machine

```
created ──▶ analyzing ──▶ synthesizing ──▶ awaiting_approval ──▶ executing
                                │                                    │
                                │ (report mode)                      ▼
                                │                                verifying
                                │                                    │
                                ▼                                    ▼
                            improving ◀── checkpoint ◀───────────────┘
                                │              │
                                ▼              └── (failed) ──▶ executing
                            complete
```

Every transition is an event on the run's log. A run can be resumed at any phase,
in a different process or a later session, by replaying that log.

The happy path is above; the paths that are not — abandonment, remediation,
envelope renewal, and where each loop is bounded — are drawn in
[`architecture.md`](architecture.md#the-phase-machine).

---

## The loop

### 1. Start

```jsonc
supervisor_start({
  "prompt": "<the user's request, verbatim and unsummarised>",
  "mode": "auto",            // auto | report | execute
  "host_agents": [           // what you can actually spawn
    {"name": "Explore",  "description": "Read-only search agent"},
    {"name": "Plan",     "description": "Software architect agent"},
    {"name": "general-purpose", "description": "General agent"}
  ]
})
```

`host_agents` is recorded on the run, so later phases still bind roles to real
subagent types — including after a resume. Roles match by name hint first
(`architecture` prefers `Plan`), then by description overlap, then by falling
back to a general-purpose agent.

### 2. Dispatch

The response carries `action: "dispatch"` and one or more packets:

```jsonc
{
  "run_id": "run_...",
  "agent_id": "agt_...",
  "kind": "analysis",             // analysis | execution | verification
                                  // | planning | synthesis | checkpoint | improvement
  "title": "Security",
  "brief": "# Analysis brief: Security\n...",
  "schema": { "type": "object", ... },
  "turn_index": 0,
  "turns_remaining": 6,
  "host_agent_type": "general-purpose",
  "model": "host",
  "task_id": null
}
```

Rules:

- **Issue independent packets in parallel**, in a single message. Analysis fans
  out precisely so several lenses run at once.
- **Pass `brief` verbatim.** Do not summarise, merge, extend or "improve" it. The
  supervisor scores drift against that exact text, and the brief carries the
  scope fence, the peer list, past lessons and the output contract.
- Use `host_agent_type` as your subagent type when it is set.
- `schema` is the exact JSON shape the answer must take.
- **The agents share one working tree.** Packets run concurrently in the same
  workspace, separated only by their path scopes, and a path scope does not
  constrain git: every brief therefore forbids `git stash`, `git checkout`,
  `git clean`, `git reset`, `git rebase` and their relatives. The harness can
  refuse those only for an agent it drives itself; in delegated mode your own
  permission model is the enforcement, so denying them to subagents is worth
  doing at the host.
- **Each run names a baseline commit**, carried in the briefs. A criterion that
  measures the whole repository -- "the test suite still passes" -- is judged
  against that commit plus the task's own diff, because the tree itself is
  moving while several agents write into it.

### 3. Report

```jsonc
supervisor_report({
  "run_id": "run_...",
  "agent_id": "agt_...",
  "result": { /* the agent's JSON, unmodified */ }
})
```

Report what the agent actually produced. Do not fill in empty fields, fix
formatting, or pad a thin answer — the supervisor needs to see weak work in
order to correct it. Prose instead of JSON is acceptable; the harness extracts
what it can.

The response contains a directive:

| Directive | What it means | What you do |
| --- | --- | --- |
| `continue` | On brief | Run the returned continuation packet |
| `refocus` | Drifted off the objectives | Run the continuation packet with its corrections |
| `narrow` | Went outside scope | Same, scope corrections attached |
| `deepen` | Claimed done, too shallow | Same |
| `escalate` | Agent blocked | Agent stops; supervisor handles it |
| `accept` | Objectives met | Agent is finished |
| `stop` | Budget spent, or repeat drift | Agent is finished |

`continue`, `refocus`, `narrow` and `deepen` all return a continuation packet.
Its brief is the agent's **original brief followed by the directive**, including
any messages other agents sent to it. The brief is repeated deliberately: you may
have kept the agent alive and be feeding it a follow-up turn, or you may be
resuming days later in a new process with the original agent long gone. A packet
cannot know which, so it always stands on its own.

An outstanding directive survives a resume. If a run is interrupted after an
agent was corrected but before it answered, `supervisor_advance` re-issues that
correction rather than briefing the agent again from scratch — otherwise the
agent would have no idea it had been told to change course, and the supervisor
no idea it had said so.

### 4. Advance

When every packet has been reported, call `supervisor_advance(run_id)` for the
next phase.

### 5. Abandon

A sub-agent can die: killed by an infrastructure failure, cancelled, or handed a
packet you simply cannot run. The supervisor cannot see that -- a host agent
reports through you, so "still working" and "gone" look identical from here.
Say so:

```jsonc
supervisor_abandon({
  "run_id": "run_...",
  "agent_id": "agt_...",
  "reason": "the sub-agent was killed by an infrastructure failure"
})
```

The agent is marked failed, the reason goes on the run's log by name, and the
phase settles: analysis moves to synthesis, an abandoned task falls to the
checkpoint and is remediated on the next attempt. Do this rather than reporting
invented output on the agent's behalf -- the supervisor is judging real work, and
a made-up turn is worse than a missing one.

If nobody says anything, the supervisor eventually concludes it on its own: an
agent handed `policy.max_unreported_dispatches` packets (3 by default) with no
report in between is abandoned with the same note and the same settling.
`policy.agent_timeout_seconds` applies the same bound in wall-clock terms, and is
off by default because a host agent may legitimately take a long time.

### 6. Approval

At `action: "await_approval"` the response carries `tasks` and `task_notes`.
Present each task to the user with its `action`, `motivation`,
`closes_findings` and `definition_of_done`, plus any `task_notes` — those record
criteria the harness inserted (test, negative-test, security, liveness and
code-quality bars), criteria it judged too weak to verify, and references to
findings it could not resolve.

```jsonc
supervisor_approve({
  "run_id": "run_...",
  "decisions": [
    {"task_id": "tsk_a", "decision": "approve"},
    {"task_id": "tsk_b", "decision": "modify", "note": "narrower",
     "modifications": {"action": "...", "scope_paths": ["src/auth/**"]}},
    {"task_id": "tsk_c", "decision": "reject"}
  ]
})
```

Modifiable fields: `title`, `action`, `motivation`, `effort`, `scope_paths`, and
`dod` (replaced wholesale). Criteria cannot be silently dropped — weakening a
definition of done after approval is how verification stops meaning anything.

**Never approve on the user's behalf.** Rejecting everything is valid: the run
ends with the analysis report.

### 7. Verification

Verification packets ask for real evidence:

```jsonc
{
  "results": [
    {"criterion_id": "dod_...", "status": "pass",
     "evidence": "$ pytest -q\nexit=0\n7 passed"}
  ],
  "summary": "...",
  "regressions": []
}
```

Two rules the harness enforces regardless of what an agent claims:

1. **A pass with empty evidence is recorded as a failure.**
2. **A verdict the harness proved mechanically outranks an agent's account of
   it.** If the harness read the file or ran the command and got a result, an
   agent cannot flip it; the disagreement is recorded as a note.

Criteria the harness can settle itself — inspections, and commands when
`policy.allow_command_execution` is on — are closed before verification packets
are issued, so agents are only asked about what genuinely needs judgement.

### 8. Completion

At `action: "complete"`, `report_markdown` contains the deliverable, including a
per-criterion checklist showing what was proven and what was not, and a
reconciliation of every finding the run produced: fixed here, attempted, still
pending, or still open. `detail.reconciliation` names the artifact holding the
full finding-by-finding mapping, and `detail.findings_open` lists the finding
ids this run did not close. Present it as written. Do not describe a task as
done when its criteria are unmet — say plainly what is outstanding.

---

## Failure handling

| Situation | What to do |
| --- | --- |
| Agent returns unusable output | Report it anyway; a correction and another turn follow |
| Agent looks plausible but off-brief | `supervisor_check_drift(run_id, agent_id)` for a second opinion |
| A verification command will not run | Report `status: "blocked"` with the real error |
| Run interrupted | `supervisor_resume(run_id)` — findings, tasks and verification survive |
| Unknown `agent_id` | The run is out of step; call `supervisor_status` |
| Sub-agent crashed or was cancelled | `supervisor_abandon(run_id, agent_id, reason)` |

Calls are idempotent at the phase level: `supervisor_advance` on a phase that has
not settled returns the outstanding packets rather than skipping ahead.

---

## Every tool

Eleven tools. The first six are the loop above; the rest are inspection you can
call at any time.

| Tool | Arguments | Returns |
| --- | --- | --- |
| `supervisor_start` | `prompt`, `mode`, `host_agents`, `backend` | the run id and its first work packets |
| `supervisor_report` | `run_id`, `agent_id`, `result` | the supervisor's directive for that agent |
| `supervisor_advance` | `run_id`, `host_agents` | the next packets, an approval request, or completion |
| `supervisor_abandon` | `run_id`, `agent_id`, `reason` | the phase, with that agent ended |
| `supervisor_approve` | `run_id`, `decisions` | the first execution packets |
| `supervisor_resume` | `run_id` (optional) | whatever the run owes next |
| `supervisor_status` | `run_id` (optional) | phase, agents, drift, criteria |
| `supervisor_runs` | `limit` | recent runs, newest first |
| `supervisor_check_drift` | `run_id`, `agent_id` | a drift score and directive for one agent |
| `supervisor_lessons` | `target`, `limit` | what previous runs taught the harness |
| `supervisor_providers` | — | stage routing and provider health |

`run_id` is optional wherever the table says so: omitted, it means the most
recent run in the store.

## Driving it without MCP

Every tool has a CLI equivalent that emits the same JSON, so the harness is
usable from a shell script, CI, or a host that does not speak MCP at all:

```bash
supervisor start "..." --host-agents '[{"name":"general-purpose"}]' --json
supervisor report <run_id> <agent_id> --input turn.json --json
supervisor advance <run_id> --json
supervisor abandon <agent_id> <run_id> --reason "sub-agent cancelled" --json
supervisor approve <run_id> --task tsk_a:approve --task tsk_b:reject --json
supervisor resume <run_id> --json
supervisor status <run_id> --json
supervisor runs --json
supervisor lessons --json
supervisor providers --json
supervisor drift <agent_id> <run_id> --json
```

`supervisor status` reports each agent's last recorded drift score; `supervisor
drift` is what asks the drift model for a fresh one, and is the CLI equivalent
of `supervisor_check_drift`.

The CLI also has commands with no tool behind them, because they are not part of
the protocol: `init`, `run` (which drives the whole loop itself), `events`,
`reindex`, and `mcp` (which serves the tools above). `supervisor --help` lists
them all.
