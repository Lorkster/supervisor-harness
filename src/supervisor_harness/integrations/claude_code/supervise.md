---
description: Run a task under the supervisor harness with parallel analysis, approved execution tasks and verified completion
argument-hint: <task description>
---

Run this task under the supervisor harness: $ARGUMENTS

Follow the `supervise` skill. In short:

1. `supervisor_start` with the task above verbatim and the sub-agent types you can spawn.
2. Dispatch every returned packet in parallel, passing each brief unmodified.
3. `supervisor_report` each result exactly as the agent produced it.
4. `supervisor_advance` when all packets are reported.
5. At `await_approval`, show the user each task with its action, motivation and
   definition of done, and let them decide before calling `supervisor_approve`.
6. At `complete`, present `report_markdown`, including which criteria were proven
   and which were not.
