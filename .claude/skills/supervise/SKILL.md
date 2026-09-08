---
name: supervise
description: Run a task under the supervisor harness — parallel multi-angle analysis, verifiable execution tasks the user approves, drift correction, and a definition of done that must be proven before anything is called finished. Use when a task is substantial enough to benefit from being examined from several angles at once, when the user asks to "supervise", "analyse properly", or "do this thoroughly", or when work needs verified completion rather than an assurance that it is done.
---

# Supervised execution

You are the **executor**, not the planner. The harness decides what should be
examined, watches for drift, and decides when something is actually done. You
run the agents it briefs and report honestly what they produced.

## Before you start: is there already a run?

**Never look for a run on the filesystem.** The store writes a `.gitignore` of
`*` into itself, deliberately -- it holds the user's prompt, their absolute
paths and every agent's full output, and it must not be committed by accident.
The consequence is that it is **invisible to file listing and search**, so
"no supervisor state found" is what you will conclude whether or not a run
exists.

Ask the harness instead:

```
supervisor_runs(limit=5)          # is one already going?
supervisor_resume(run_id="...")   # pick it up; omit run_id for the latest
```

A resume returns the run's next packets with its findings, tasks and
verification state intact -- they were never in your context, they are on disk.
**Starting a fresh run when one is in flight duplicates every agent and every
approval**, so check before you start.

## The loop

1. **Start.** Call `supervisor_start` with the user's task verbatim, plus the
   sub-agent types you can spawn:

   ```
   supervisor_start(
     prompt="<the user's full request, unsummarised>",
     mode="auto",
     host_agents=[{"name": "Explore", "description": "..."},
                  {"name": "Plan", "description": "..."},
                  {"name": "general-purpose", "description": "..."}]
   )
   ```

   List the agent types you genuinely have. Roles bind to them by name, so a
   security lens can land on a security-review agent rather than a generic one.

2. **Dispatch the packets.** A packet normally carries **paths, not text**:

   | field | what it is |
   | --- | --- |
   | `brief_path` | the brief. Tell the sub-agent to read this file. |
   | `contract_path` | the JSON schema its answer must match |
   | `result_path` | **where the sub-agent writes its answer** |
   | `brief_digest` | a few lines for you. It names the job; it is not the job. |
   | `host_agent_type` | the `subagent_type` to spawn, when set |
   | `host_agent_reason` | why that one, or why none |

   Spawn a subagent with the Task tool and tell it to **read `brief_path` in
   full and write its answer to `result_path`**. Do not read the brief into your
   own context to relay it, and do not work from `brief_digest` -- the
   supervisor measures drift against the brief's exact text, and a sub-agent
   briefed from the digest is an unmeasured agent.

   **You must not write the result file yourself.** It is the sub-agent's
   answer, written by the sub-agent. If you write it, the answer passed through
   your context on the way, which is the whole thing this avoids -- and you are
   the only party who can tell that it happened.

   **Issue every independent packet in a single message so they run in
   parallel.** That parallelism is the point of the analysis phase.

   A packet with `brief` and `schema` populated and no paths is the *inline*
   form, used when the harness is configured for a host that cannot read files.
   Handle both: if `brief_path` is set, use the files.

3. **Report each result.** By path, whenever the packet gave one:

   ```
   supervisor_report(run_id="...", agent_id="...", result_path="...")
   ```

   The harness reads the file. The answer never enters your context, so a lens
   that produced forty findings costs you one string. Pass `result=` only for a
   packet that was inline, or for an agent that answered in the conversation
   instead of writing its file.

   Report what the agent actually said. Do not fill in fields it left empty,
   fix its formatting, or improve a thin answer — the supervisor needs to see
   thin work in order to correct it. If a subagent returned prose instead of
   JSON, pass the prose; the harness will extract what it can.

   You get back a directive. If it contains a packet, run that packet and
   report again. If the agent was accepted or stopped, move on.

4. **Advance.** Once every packet is reported, call `supervisor_advance` to get
   the next phase.

5. **Approval.** When the run returns `await_approval`, present each proposed
   task to the user showing:
   - what it will do (`action`)
   - why (`motivation`)
   - how completion will be proven (`definition_of_done`)
   - anything in `task_notes` — these are criteria the harness added or flagged
     as too weak to verify

   Ask which to approve, modify or reject. **Never approve on the user's
   behalf.** Then call `supervisor_approve` with their decisions.

6. **Execution and verification** repeat steps 2–4. Verification agents must
   run the stated commands for real and report the actual output. A criterion
   marked passed with no evidence is recorded as failed.

7. **Finish.** At `complete`, show the user `report_markdown`. It states which
   definition-of-done criteria were proven and which were not. Do not describe
   a task as done if its criteria are unmet — say plainly what is outstanding.

## Rules

- **Do not answer packets yourself** when you can spawn a subagent. Independent
  agents disagreeing with each other is what makes the analysis worth running;
  you answering all of them yourself produces one opinion wearing several hats.
- **Do not skip the approval step.** Execution tasks change the user's code.
- **Do not re-litigate a directive.** If the supervisor says an agent drifted,
  pass the correction to that agent rather than arguing on its behalf.
- **Resume rather than restart**, and resume by *asking the harness*, not by
  looking for state on disk -- see "Before you start". A fresh run alongside a
  live one duplicates every agent and every approval.
- **Print the `ledger` line** on each response before you dispatch. It is one
  line -- phase, agents done, turns, findings, elapsed, and how many agents are
  still out -- and it is all the user has to tell a slow run from a stuck one.

## When something goes wrong

- An agent returns unusable output → report it anyway; the supervisor will
  issue a correction and give it another turn.
- An agent looks plausible but off-brief → `supervisor_check_drift(run_id,
  agent_id)` asks the drift model for a second opinion.
- A subagent crashed, was cancelled, or its packet cannot be run →
  `supervisor_abandon(run_id, agent_id, reason)`. The harness cannot see that
  one of your subagents died: from its side a dead agent and a working one are
  both silence, and it will keep handing you the same packet. Say so instead of
  inventing a result on the agent's behalf. The phase then settles: analysis
  moves on, an abandoned task falls to the checkpoint and is retried.
- A verification command cannot run → report `status: "blocked"` with the real
  error. A blocked criterion is honest; a fabricated pass is not.
- The user wants to stop → the run is persisted; `supervisor_status` shows
  where it got to and it can be resumed later.

## Choosing not to use this

For a quick question, a one-line fix, or anything where a single pass is
obviously enough, just do the work. The harness costs several parallel agents
and a round of user approval; that is worth it for substantial or risky work
and wasteful for a typo.
