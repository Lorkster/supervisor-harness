# Prompt for the agent building step 5

*Paste everything below the rule as the opening message. Carry `DESIGN.md` alongside it. Steps 1–4 must already
be merged and working, and `verdicts.jsonl` must hold real triage history — see the gate below.*

---

You are adding the **LLM ranker (R2)** and the **A/B comparison** to a morning error report over VictoriaLogs.
`DESIGN.md` sits beside this message; read §6 in full. R1 — the deterministic ranker — the report, verdict
capture and GitLab anchoring are built.

This milestone is an experiment, not a feature. Build it as one.

## The gate

You can build R2 today. **You cannot conclude anything until the verdicts exist.** Run `report verdicts` first
and look at how many runs have been triaged. If the answer is "three", build the machinery, run both rankers
daily, and say plainly in your report that the comparison is not yet readable. Do not produce a recommendation
from a handful of mornings because the task asked for one.

## Three things that decide whether this experiment is worth running

### 1. Blinding — R2 must not see R1's output

R2 receives the bundle **with `score`, `rank` and any R1-derived field stripped**, and with the candidate order
**randomised per run**. If R2 can see R1's ranking, or infer it from the order, it will anchor on it and you will
measure agreement rather than judgement. This is not a nicety; without it there is no experiment.

### 2. The candidate set must be ranker-neutral

`DESIGN.md` §8 originally capped candidates by R1 pre-score. That confounds the comparison — R1 would be choosing
what R2 is allowed to rank. The design has been amended: the prefilter is now a neutral union (everything novel,
plus top-N by distinct traces, plus top-N by hits, capped). Confirm the implementation matches before you start
collecting comparison data, and if it does not, fix that first.

Be honest in your write-up about what this still cannot measure: both rankers see the same prefiltered set, so a
finding neither ever sees is invisible to the experiment.

### 3. R2's self-consistency is a measurement, not an assumption

The design asserts that an LLM ranking "changes run to run". Test it rather than repeating it: on a sample of
runs, rank the same bundle twice and record the rank correlation between R2 and itself. If R2 is unstable against
its own input, that is a finding that matters as much as its accuracy — a ranker you cannot reproduce is one you
cannot debug on the morning it is wrong.

## What to build

- **R2**: one model call per run, taking the blinded bundle and returning
  `[{fingerprint, rank, rationale, informational: bool}]`. Its prompt states the derivative principle from §1 —
  that a flat high-volume error is not news — so both rankers aim at the same target. Keep the prompt in a file,
  version it, and record which version produced each ranking.
- **Side-by-side rendering**: two rank columns in the HTML with disagreements highlighted. Verdict capture keeps
  working and now records both ranks per finding.
- **`report compare`**: reads `verdicts.jsonl` and the stored rankings and reports, per ranker — precision@5
  against `actionable`, how often an `actionable` finding ranked below an `informational` one, mean rank of
  `actionable` findings, Spearman between the rankers, R2 self-consistency, and cost per run.

## Pre-register the decision rule

Before you look at any results, write the decision rule into the repo — which metric decides, what margin
counts, and how many triaged runs are enough. Commit it. Then run the comparison.

The design's stated expectation is that R1 wins on stability and debuggability while R2 wins on the
`informational` judgement, ending at R1-ranks / R2-demotes. **That is a prediction to be tested, not a conclusion
to be reached.** If the data contradicts it, report that clearly — it is the more valuable outcome, and the whole
reason for spending a model call a day on this.

## Out of scope

Jira. The scheduler. Widening beyond the pilot team. Acting on the comparison's outcome — that is a decision for
a human after reading the numbers.

## Verification bar

- [ ] The payload sent to R2 has been dumped and inspected: no `score`, no `rank`, no R1-derived field, order
      randomised.
- [ ] Both rankings are stored per run and survive a re-render.
- [ ] `report compare` runs on the existing verdict history and produces numbers you can explain line by line.
- [ ] Self-consistency measured on at least five bundles.
- [ ] The pre-registered decision rule is committed **before** any comparison output appears in your report.
- [ ] Cost per run measured, not estimated, and still within the budget in §8.
- [ ] R1's golden-bundle ranking is byte-identical to before.

## Report back with

1. The comparison numbers, or a plain statement that there is not yet enough triage history — with how many more
   runs are needed.
2. R2's self-consistency, and whether it changes how much you would trust it.
3. The findings the two rankers disagreed on most sharply. Those are the interesting cases and are worth showing
   individually.
4. Measured cost per run against the §8 budget.
5. Whether the pre-registered rule actually resolved the question, or whether the metric turned out to be the
   wrong one — in which case say so rather than quietly substituting a different one.
