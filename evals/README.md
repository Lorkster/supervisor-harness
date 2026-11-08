# Eval cases

Cases for `supervisor eval run` (batch F; the coordinator cases are batch O): one
role, one workspace, one input each. See `src/supervisor_harness/evals/cases.py`
for the format and `src/supervisor_harness/evals/checks.py` for the checks. A
case's role is one of `planner`, `review`, `verifier` or `coordinator`.

| Directory | Source | Roles |
| --- | --- | --- |
| `cases/greenfield/` | written by hand: an empty directory, or a few files, and a request | planner, reviewer, verifier |
| `cases/p3-18/` | go-live runs 18, 19 and 21 on plantsandclimate P3-18 (base cfdb95c) | planner, reviewer, verifier |
| `cases/9a/` | go-live run 13 on supervisor-harness item 9a (base ab70d7b) | planner, reviewer |
| `cases/minesweeper/` | Minesweeper run 24 (base 90beb78): the motion-test criterion, judged three ways | verifier |
| `cases/minesweeper-coordinator/` | moments in Minesweeper runs 7 to 24 where reports came back and something had to be decided, each as the coordinator is asked it | coordinator |

The recorded cases need their repository, named by an environment variable so
no case carries one machine's paths:

```bash
export PLANTSANDCLIMATE=/path/to/plantsandclimate     # a clone holding cfdb95c
export SUPERVISOR_HARNESS=/path/to/supervisor-harness  # a clone holding ab70d7b
export MINESWEEPER=/path/to/minesweeper               # a clone holding 90beb78, 38f3e96 and ffd47ca
```

A verifier case carries the change it judges as a patch beside it
(`p3-18-run19.patch`), applied on top of the base commit. A coordinator case may
take the run's final commit and a patch that takes some of its files back out
(`ms-run24-motion-tests-before-the-spy.patch`): the tree the run had at that
moment is gone, and the final commit alone would show a fix the moment predates.

**Labels.** A case's `checks` beyond its role's generic set are a person's
expectations -- "this task should be vetoed", "no task should wire the browser
suite into `npm run check`" -- and `labelled_by` says who wrote them and on what
evidence. The P3-18 labels come from the run analyses of 2026-10-08, checked
against the project's docs and CI workflow; the owner declined every veto a
label calls a veto. The 9a review cases carry no labels, only the generic checks.

`p3-18-run18-planner-old-request.json` keeps the request as it was before run
23: it said "green when `npm run check` passes" and asked for a Playwright test,
contradicting the phase's acceptance (three separate gates). A planner should not
resolve that by changing how the project is checked.

To add cases from a run: `python tools/extract_eval_cases.py --help` for
planner, review and verifier cases. Coordinator cases come from
`python tools/extract_coordinator_cases.py --help`, which reads a spec naming a
run and a window of its log; `coordinator-cases.spec.json` beside this file is
the spec the Minesweeper cases were made from.
