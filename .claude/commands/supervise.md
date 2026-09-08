---
description: Run a task under the supervisor harness with parallel analysis, approved execution tasks and verified completion
argument-hint: <task description>
---

Run this task under the supervisor harness: $ARGUMENTS

Follow the `supervise` skill. In short:

0. `supervisor_runs` first — if a run is already in flight, `supervisor_resume` it
   instead. The store is gitignored and invisible to file listing, so looking for it
   on disk finds nothing whether or not it is there.
1. `supervisor_start` with the task above verbatim and the sub-agent types you can spawn.
2. Dispatch every returned packet in parallel. Have each sub-agent read `brief_path`
   in full and write its answer to `result_path`; never write that file yourself.
3. `supervisor_report` with `result_path`, so the answer never crosses your context.
4. `supervisor_advance` when all packets are reported.
5. At `await_approval`, show the user each task with its action, motivation and
   definition of done, and let them decide before calling `supervisor_approve`.
6. At `complete`, present `report_markdown`, including which criteria were proven
   and which were not.
