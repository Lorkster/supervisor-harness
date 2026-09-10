# Prompt for the agent building step 3

*Paste everything below the rule as the opening message. Carry `DESIGN.md` alongside it. Steps 1 and 2 must
already be merged and working — this builds directly on their output.*

---

You are adding verdict capture to a morning error report over VictoriaLogs. `DESIGN.md` sits beside this message;
read §6 and §7 before writing code. Steps 1 and 2 — discovery, the evidence bundle, the deterministic R1 ranker
and the HTML report — are built. Each finding in the report already carries a `data-fingerprint` attribute; that
is your attachment point.

## Why this milestone exists

Step 5 compares two rankers. That comparison is worthless without labelled ground truth, and ground truth can
only be gathered by a human looking at real findings on real mornings. **Every day this is not shipped is a day
of evidence permanently lost.** That is the whole justification for building it before anything more interesting.

Keep it small. This is a day of work, not a week.

## Scope

Add to the existing report the ability for whoever triages to mark each surfaced finding, and persist those marks
as an append-only log the later milestones can read.

**Verdict vocabulary** — four values, not the three in the design:

| Verdict | Meaning |
|---|---|
| `actionable` | A developer should do something about this. |
| `informational` | Real, understood, no action needed. |
| `already-known` | Being tracked elsewhere; the report is late, not wrong. |
| `bad-fingerprint` | The grouping is wrong — unrelated failures collapsed together, or one failure split across several findings. |

The fourth is the addition. It is not a verdict about the error; it is a verdict about **the tool**, and it is the
only signal that will tell you `collapse_nums prettify` is mis-grouping this deployment's logs. Count them
separately and surface the rate — a rising `bad-fingerprint` rate is a defect report, not data.

## The one decision you have to make

The report is a static HTML file. It cannot write to disk from `file://`, and `localStorage` on a `file://`
origin is inconsistent across browsers and silently per-machine. Pick a persistence mechanism and say why.

**Recommended:** add `report serve` — a small localhost HTTP server that serves the report and accepts a POST per
verdict, appending to `verdicts.jsonl`. It needs no infrastructure, no auth (localhost only), keeps everything on
the machine, and fits the dev-machine-first constraint the whole project is built around.

**Required regardless of what you pick:** if the report is opened as a bare file with no server behind it, the
buttons must still work and offer an export — a copy-to-clipboard of the verdict lines, or a downloaded
`.jsonl` — rather than appearing to save and not saving. **A verdict that looks recorded but is not is worse than
no button at all**, because it destroys trust in the numbers step 5 depends on.

## Shape of the data

Append-only, one JSON object per line, never rewritten in place:

```json
{"fingerprint":"9f2c1a…","run_date":"2026-09-10","verdict":"actionable",
 "rank_r1":3,"by":"someone","at":"2026-09-10T08:41:12Z","note":"optional free text"}
```

- Keyed by **fingerprint + run_date**, not fingerprint alone. The same error can be actionable one week and
  understood the next; both facts are true and both matter.
- Record the rank the finding held when it was judged. Without it, precision@5 cannot be computed later.
- A corrected verdict appends a new line. Readers take the last write per key. Never edit history.
- Re-rendering an old bundle must show verdicts already recorded for that run, so a second triager sees what the
  first decided rather than re-judging blind.

Add a `report verdicts` command that summarises what has accumulated: counts per verdict, the `bad-fingerprint`
rate, and how many runs have been triaged at all. You will want it to know whether step 5 is ready to run.

## Scope guard — read this twice

**Verdicts must not feed back into R1 scoring, suppression, or anything else that changes what the report
surfaces.** They are observational labels only.

If verdicts influence the ranking, the labels stop being independent of the ranker and the step 5 comparison
measures nothing. This is the single easiest way to destroy the experiment, and it will look like a helpful
feature while you are doing it.

Note that `suppressions.yaml` from §7 is a *different* mechanism with different semantics — an explicit human
action that does change the output, reviewed in an MR. Keep the two files and the two concepts separate. Do not
auto-generate suppressions from `informational` verdicts, however tempting.

Also out of scope: R2, GitLab anchoring, Jira, the scheduler.

## Verification bar

Do not report this as working until you have shown the output:

- [ ] Triaging a report writes the expected lines to `verdicts.jsonl`; you have shown the file after a real pass.
- [ ] Re-rendering the same bundle shows those verdicts already applied.
- [ ] A corrected verdict appends rather than edits, and readers resolve to the last write.
- [ ] The no-server path was tested by opening the file directly: buttons work, export works, and nothing claims
      to have saved when it has not.
- [ ] `report verdicts` returns sensible counts over at least two runs' worth of data.
- [ ] Steps 1 and 2 still pass their existing tests, and the golden-bundle R1 ranking is byte-identical to before
      — you have changed no scoring behaviour.

## Report back with

1. The persistence mechanism you chose and why, including what happens with no server.
2. A real `verdicts.jsonl` from an actual triage pass.
3. Your `bad-fingerprint` count from that pass, and — if any — which findings earned it and what went wrong with
   the grouping. That is the highest-value thing you can tell us.
4. How many days of triage you estimate are needed before the step 5 comparison has enough signal, given the
   number of findings a typical run surfaces.
