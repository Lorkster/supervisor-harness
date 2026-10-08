"""Batch F: does each role do what the harness asks of it?

One role at a time -- the planner, the reviewer, the verifier -- called the way
the harness calls it, on a fixed input, several times, scored by properties of
its output that hold for any project, and by a person's labels where a case has
them. And, for a whole run, a scorecard: the project's own acceptance commands
on the run's branch, beside what the harness recorded.

Observe-only. Nothing here changes how a run behaves; it measures, so that a
change to a role can be judged on recorded inputs in minutes rather than on one
live run that cannot tell a fix from luck. See docs/autonomy-plan.md, batch F.
"""
