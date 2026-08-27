---
name: supervise
description: Run a task under the supervisor harness — parallel multi-angle analysis, verifiable execution tasks the user approves, drift correction, and a definition of done that must be proven before anything is called finished. Use when a task is substantial enough to benefit from being examined from several angles at once, when the user asks to "supervise", "analyse properly", or "do this thoroughly", or when work needs verified completion rather than an assurance that it is done.
---

# Supervised execution

You are the **executor**, not the planner. The harness decides what should be
examined, watches for drift, and decides when something is actually done. You
run the agents it briefs and report honestly what they produced.

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

2. **Dispatch the packets.** Each packet has `agent_id`, `brief`, `schema` and
   `host_agent_type`. For each one, spawn a subagent with the Task tool, using
   `host_agent_type` as `subagent_type` when it is set.

   **Issue every independent packet in a single message so they run in
   parallel.** That parallelism is the point of the analysis phase.

   Pass `brief` through **verbatim**. Do not summarise it, tighten it, add to
   it, or merge two packets into one prompt. The supervisor measures drift
   against that exact text, and it contains the scope fence, the peer list and
   the output contract.

3. **Report each result.** When a subagent returns, call `supervisor_report`
   with its JSON exactly as produced:

   ```
   supervisor_report(run_id="...", agent_id="...", result={...})
   ```

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
- **Resume rather than restart.** If a run was interrupted,
  `supervisor_resume` picks it up from the event log with its findings, tasks
  and verification state intact.

## When something goes wrong

- An agent returns unusable output → report it anyway; the supervisor will
  issue a correction and give it another turn.
- An agent looks plausible but off-brief → `supervisor_check_drift(run_id,
  agent_id)` asks the drift model for a second opinion.
- A verification command cannot run → report `status: "blocked"` with the real
  error. A blocked criterion is honest; a fabricated pass is not.
- The user wants to stop → the run is persisted; `supervisor_status` shows
  where it got to and it can be resumed later.

## Choosing not to use this

For a quick question, a one-line fix, or anything where a single pass is
obviously enough, just do the work. The harness costs several parallel agents
and a round of user approval; that is worth it for substantial or risky work
and wasteful for a typo.
