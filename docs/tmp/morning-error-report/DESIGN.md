# Morning error report — design

A scheduled job that reads VictoriaLogs, decides which errors deserve a developer's attention today, anchors each
one in code and change history, publishes an HTML report, and files a Jira issue for anything above a threshold.

Pilot scope: `{environment="si1.mt1", team="cav"}`.

---

## 1. The principle everything else follows from

**The report is a derivative detector, not a magnitude detector.**

An error that has occurred 4,000 times a day for the last month is not news. An error that occurred 11 times today
and never before is. Ranking by volume produces a report that is correct, stable, and ignored within a week —
which is the normal failure mode of this kind of tooling.

So every finding is scored on **what changed**, and volume enters only as a multiplier on blast radius. The
report's job is to be *short* and *different every day*.

Corollary: high-volume flat errors do not disappear from the report. They move to a collapsed "steady state"
section that is honest about their existence without demanding attention. Hiding them teaches people the report
is lying.

---

## 2. Constraints

| Constraint | Consequence for the design |
|---|---|
| Must be verifiable on a dev computer first | Plain CLI, no host dependencies. Freeze/replay of inputs. Dry-run by default. |
| Token usage must be minimal | The model only ever sees a small JSON bundle, never log lines. §8. |
| **The VictoriaLogs HTTP API is closed** — access is via SSO-approved channels, with a scoped token in the VL MCP client config | The MCP server is the only sanctioned data path, and the collector speaks MCP as a plain program. Collection stays free; nothing about the architecture changes. Never bypass the wrapper. §8. |
| Jira via a non-official Atlassian MCP (regulatory) | The issue sink is an interface with four methods, not a dependency. The regulated boundary is one file. |
| Compare deterministic vs LLM ranking | Two rankers over one frozen bundle, plus a ground-truth capture loop. §6. |
| Host not yet chosen | Everything above the scheduler is host-agnostic; the host is a `cron` line wrapping one command. §10. |

---

## 3. Architecture

Four stages with a **frozen evidence bundle** as the seam in the middle.

```
  VictoriaLogs MCP                     GitLab            Jira MCP
  (scoped token)                                         (non-official)
       │                                    │                │
       ▼                                    ▼                ▼
  ┌─────────┐   ┌──────────┐   ┌────────┐   ┌────────┐   ┌────────┐
  │ A       │──▶│ B        │──▶│ bundle │──▶│ C      │──▶│ D      │
  │ discover│   │ evidence │   │ .json  │   │ rank   │   │ emit   │
  └─────────┘   └──────────┘   └────────┘   └────────┘   └────────┘
   ~2 queries    ~4 queries     the seam     R1 / R2      HTML + Jira
   0 tokens      0 tokens       ~20 KB       0 or 1 call  1 call/finding
```

The bundle earns its place three times over:

- **Dev verification.** `--replay bundle.json` reruns C and D offline with no VictoriaLogs access, no clock
  dependency, and no cost. Ranking becomes unit-testable against a golden bundle checked into the repo.
- **A fair A/B.** R1 and R2 rank *identical* input. Any difference is the ranker, not query drift.
- **Token control.** The bundle is the only thing the model ever sees. Raw log lines never enter a context
  window; at most one exemplar line per candidate does.

---

## 4. Stage A — candidate discovery

One query produces the day's candidate set. `collapse_nums prettify` is the fingerprinting primitive: it rewrites
numbers, UUIDs, IPs, timestamps and durations into placeholders, so a thousand distinct error lines collapse into
the handful of *patterns* that produced them.

```logsql
_time:24h {environment="si1.mt1", team="cav"} log.level:in("error","fatal")
  | copy _msg as raw_msg
  | collapse_nums at _msg prettify
  | stats by (service_name, _msg)
      count() hits,
      count_uniq(trace_id) traces,
      row_max(_time, raw_msg, trace_id) as newest,
      row_min(_time, raw_msg) as oldest
  | sort by (hits desc)
  | limit 200
```

- `copy _msg as raw_msg` **before** collapsing preserves a real example. Without it, every exemplar in the report
  is a pattern with `<N>` in it, which developers cannot search for.
- `row_max`/`row_min` return whole log entries as JSON, giving first-seen, last-seen and a quotable exemplar from
  one pass. (`min(_time)` may also work but is documented in terms of string comparison — `row_*` is the
  documented way and returns more.)
- The grouped `_msg` — the collapsed pattern — **is the fingerprint**. Hash it for a stable id.

The fingerprint round-trips: the docs state patterns from `collapse_nums prettify` are valid input to
`pattern_match()`. So yesterday's fingerprint becomes today's filter:

```logsql
_time:1d offset 3d {environment="si1.mt1", team="cav"}
  _msg:pattern_match("<DATETIME> checkout: upstream <W> timeout after <N>s")
  | stats count() hits, count_uniq(trace_id) traces
```

That is the whole trend engine, with **no state database**. History lives in VictoriaLogs; the job stores only
fingerprints and human verdicts.

---

## 5. Stage B — evidence

For each candidate (capped, see §8), gather signals. Each is a cheap aggregate; none returns log lines.

| Signal | How | Why it matters |
|---|---|---|
| **Baseline** | same `pattern_match` over `_time:1d offset 1d … offset 7d` | The denominator for everything. One batched query per offset, not per candidate. |
| **Novelty** | zero hits across the 7-day baseline | Strongest single signal. A genuinely new error is almost always worth a look. |
| **Trend** | today ÷ median(baseline) | Distinguishes a regression from background. |
| **Blast radius** | `count_uniq(trace_id)`, `count_uniq(service_name)` | 10,000 lines from one hot retry loop is one bug; 200 distinct traces is an outage. **Rank on traces, not lines.** |
| **Spread** | `stats by (_time:1h) count()` → non-zero buckets | All-in-one-hour = a burst that may be over. Spread across 20 hours = ongoing. |
| **Still live** | hits in `_time:1h` | Decides "investigate now" vs "post-mortem". |
| **Severity** | `log.level`, plus stack-trace markers in the exemplar | `fatal`/`panic` outrank `error`. |
| **Propagation** | do the same `trace_id`s error in another `service_name`? | Crossing a service boundary means someone else's morning too. |

The trace-based blast radius is the signal that most changes what the report says. Line counts are dominated by
whichever service logs most enthusiastically; distinct traces measure how many real requests were harmed.

---

## 6. Stage C — ranking, and the R1 vs R2 comparison

Both rankers consume the same bundle and emit the same shape: `[{fingerprint, rank, score, rationale}]`.

### R1 — deterministic

Arithmetic, reproducible, arguable in a code review. Starting weights (expect to tune these — that is the point
of the pilot):

```
novelty      = 3.0  if unseen in 7-day baseline, else 0
regression   = 2.0 * clamp(log2(today / median(baseline)), 0, 3)
blast        = 1.5 * log10(1 + distinct_traces)
propagation  = 1.0  if trace_ids span > 1 service_name
severity     = 1.0  fatal/panic, 0.4 error
liveness     = 0.8  if still occurring in the last hour
spread       = 0.5 * (non_empty_hour_buckets / 24)

score = (novelty + regression + blast + propagation + severity + liveness + spread)
        * suppression_multiplier
```

`suppression_multiplier` is `0` for an active suppression, `0.15` for a fingerprint classified as steady-state
noise (§7).

### R2 — LLM ranks

One call. Input: the bundle **blinded** — `score`, `rank` and every R1-derived field stripped, candidate order
randomised per run. Without blinding, R2 anchors on R1's ordering and the experiment measures agreement rather
than judgement. Output: a ranked list with a one-sentence rationale per finding, and an explicit
`informational: true|false` judgement. Its prompt states the derivative principle and the fact that a flat
high-volume error is not news, so both rankers are aiming at the same target.

R2's **self-consistency** is a measurement, not an assumption: rank the same bundle twice on a sample of runs and
record the correlation. A ranker that disagrees with itself is one you cannot debug on the morning it is wrong.

### The comparison protocol

This is the part that makes it a real experiment rather than a preference:

1. Every run stores both rankings in the bundle output, and the HTML shows **two rank columns side by side** with
   disagreements highlighted.
2. Whoever triages marks each surfaced finding `actionable` / `informational` / `already-known` /
   `bad-fingerprint`. One click in the HTML, appended to `verdicts.jsonl` keyed by fingerprint + run date.
   **This is the only human input the system needs, and without it the comparison is vibes.**

   The fourth value is a verdict on the *tool*, not the error: it means `collapse_nums prettify` grouped badly,
   lumping unrelated failures together or splitting one across several findings. It is the only signal that will
   catch mis-fingerprinting, and a rising rate is a defect report rather than data. Verdicts are observational
   only — they must never feed back into scoring or suppression, or the labels stop being independent of the
   ranker and the comparison measures nothing.
3. After ~15 runs, compute per ranker: precision@5 against the verdicts, how often an `actionable` finding was
   ranked below an `informational` one, and Spearman correlation between the two rankers.
4. Decide. Likely outcome, stated in advance so it can be falsified: R1 wins on stability and debuggability, R2
   wins on the `informational` judgement, and the end state is R1 for ranking with R2 as a demoter (the fourth
   option from the original question). If the data says otherwise, follow the data.

Cost note: R1 is free. Running both daily costs one extra model call, which is the cheapest experiment available.

---

## 7. The "informative error" problem

Named explicitly because it is the thing most likely to sink the report's credibility. Three mechanisms, in order
of preference:

1. **Code anchoring (best).** `semantic_code_search` on the fingerprint reveals the call site. `log.Error` on a
   handled fallback path reads very differently from an unhandled panic, and the surrounding code usually settles
   the question. This is the strongest reason to have GitLab in the loop at all.
2. **Flatness.** Any fingerprint with ≥14 days of history, coefficient of variation below a threshold, and no
   linked incident is auto-classified steady-state and drops to the collapsed section. Reversible: a rate change
   promotes it straight back.
3. **Explicit suppressions.** `suppressions.yaml` in the repo, keyed by fingerprint hash:

```yaml
- fingerprint: 9f2c1a…
  pattern: "<DATETIME> cache miss for key <W>"
  reason: "Expected on cold start; tracked in CAV-1183"
  expires: 2026-12-01        # required — no permanent silent ignores
  added_by: "@someone"
```

The expiry is not bureaucracy. Permanent suppressions are how a report quietly stops covering the thing that
eventually breaks. Expired entries reappear in the report with a "suppression lapsed" note.

---

## 8. Access path and token budget

**Corrects an earlier assumption in this design.** The VictoriaLogs HTTP API is closed — access is forced
through SSO-approved channels, and the VictoriaLogs MCP server holds scoped tokens. "Call `/select/logsql/query`
from a script" is not available, so the original cost strategy does not survive contact.

### MCP is a protocol, not a language model

**An MCP client does not have to be an LLM**, and this is what saves the budget. The token is set in the MCP
client configuration, so it is a static credential any process can present — the collector is a small program
that speaks MCP JSON-RPC, calls `tools/call` for `query` / `hits` / `facets`, and gets JSON back at **zero model
cost**. The rest of the design is untouched: code writes the bundle, and stages C and D stay deterministic and
free.

**Transport does not constrain this.** Either mode works for a script:

- **stdio** (the server's default) — the collector spawns the server as a subprocess with the configured env and
  talks over stdin/stdout. For a scheduled job this is the *simpler* option: no listening socket, no network
  exposure, credential handling identical to any other subprocess.
- **http / sse** — the collector posts JSON-RPC with the bearer header.

Start from whichever the existing MCP config already uses; there is no reason to add a transport.

> **Do not extract the token and call VictoriaLogs directly**, even though the credential would probably work
> against `/select/logsql/*`. The HTTP API is closed to force access through an approved channel; routing around
> the wrapper is precisely what that control exists to prevent. The MCP server *is* the sanctioned path, and a
> script using it properly is inside the policy, not skirting it. If anyone proposes the shortcut on performance
> grounds, the answer is no.

### Token budget

| Stage | Model calls | Notes |
|---|---|---|
| A discover, B evidence | 0 | MCP client in code |
| C rank (R1), D render | 0 | pure functions over the bundle |
| C rank (R2) | 1 | blinded bundle ≈ 4–6k tokens |
| Anchoring write-up | 1 batched | emitted findings only |
| **Total per run** | **2** | **under 15k tokens** |

### If collection ever has to be agent-driven

Kept as a contingency, not a plan — it applies only if the credential later becomes user-bound and cannot leave
an interactive session. An agent would run the queries and **write** `bundle.json` without reasoning about the
contents; C and D stay code, so R1 and the report remain free, but collection stops being free.

The five rules below are mandatory in that mode. **Rules 2 and 3 are worth adopting regardless** — they make the
collector faster and the payload smaller whoever is driving it:

1. **Aggregate-only.** Never call the `query` tool without a terminating `stats` / `top` pipe. A tool result is
   text in a context window; a thousand log lines is the end of the run.
2. **One query for the whole baseline.** `| stats by (service_name, _msg, _time:1d) count()` over the 8-day
   window returns the entire per-day series sparsely, in one round trip, instead of seven. It is also exactly
   the shape the report's sparkline needs.
3. **Exemplars after ranking, not during discovery.** Discovery returns fingerprints and counts only. Fetch the
   one raw exemplar per finding once you know which 3–5 will be emitted. This removes the largest single
   contributor to payload size.
4. **Hourly spread only for emitted findings.** `stats by (_time:1h)` across 25 candidates is 600 rows; across
   5 it is 120.
5. **Cap discovery at ~40 rows**, not 200. The tail is carried as aggregate counts, not as rows.

Collection in that mode costs roughly 6–10k tokens on top of the two calls above — within bounds, but with a much
thinner margin.

Three further rules apply either way:

- **Cap candidates at ~25 with a ranker-neutral prefilter.** Discovery may return 200; the bundle carries the
  union of *everything novel*, *top-N by distinct traces* and *top-N by hits*, plus aggregate counts for the
  tail. It must **not** be capped by R1 pre-score: R1 choosing what R2 is allowed to rank confounds the
  comparison in §6. Both rankers see the same neutral set; a finding neither ever sees stays invisible to the
  experiment, which is a limitation to state rather than to hide.
- **Anchor after ranking, never before.** `semantic_code_search` and `list_merge_requests` run only for findings
  that will be emitted — roughly 3–5 calls, not 25.
- **One exemplar line per candidate, truncated.** Never a sample of N log lines.

---

## 9. Stage D — emission

**HTML report** — always written, the durable artifact, self-contained in the same style as the training deck.
Contains: ranked findings with both rank columns, sparkline of the 7-day baseline per finding, the exemplar, the
code anchor, the **exact LogsQL to reproduce each finding** (paste-into-vmui, per the deck's own rule), the
collapsed steady-state section, and the verdict buttons that write `verdicts.jsonl`.

**Jira above threshold** — score above a cutoff *and* (new or regressed). Never for steady state.

- **Idempotency by fingerprint.** Search for an open issue carrying the fingerprint hash in a labelled field
  before creating. If found, comment with today's numbers rather than opening a duplicate. Getting this wrong
  produces 30 issues for one bug in a fortnight and kills the project.
- **Cooldown.** A fingerprint that produced an issue does not produce another for N days even after the issue is
  closed, unless the rate changes materially.
- The sink interface is deliberately tiny — `find_by_fingerprint`, `create`, `comment`, `close` — so the
  non-official Atlassian MCP sits behind four methods and can be swapped or audited in one file.

---

## 10. Host comparison

The recommendation is to defer this properly: build a plain CLI, verify it on a dev machine, and let the host be
a scheduler that runs one command. Then the choice is reversible.

| | GitLab CI schedule | Kubernetes CronJob | Dev machine / local cron |
|---|---|---|---|
| Reaching VictoriaLogs | Via the VL MCP endpoint | Via the VL MCP endpoint | Via the VL MCP endpoint |
| **Holding the VL token** | CI variable + generated MCP config | k8s secret + generated MCP config | The MCP config already on the machine — **works today, nothing to arrange** |
| Secret handling | Native CI variables | Native k8s secrets | Weakest — a real concern for the Jira token |
| Reproducibility | Good, pinned image | Good, pinned image | Poor |
| Fits the pilot | Yes | Overkill on day one | **Yes — start here** |
| Data-residency posture | Depends on runner | Inside the perimeter | Inside the perimeter |

**A compliance question worth answering before the host is chosen:** the reason the official Atlassian MCP is
off-limits probably also constrains where this job may run and what may leave the network. Log content quoted
into a Jira issue is log content leaving VictoriaLogs, and an exemplar line may carry personal data. Two things
follow: get the constraint stated by whoever owns it, and **redact exemplars before they reach either the model
or Jira** — the fingerprint is already redacted by construction, which is a useful accident of `collapse_nums`.

---

## 11. Build order

1. **Discovery + bundle.** Stage A and B writing `bundle.json`. No ranking, no output. Verify by eye against
   vmui: the fingerprints should look like real error classes.
2. **R1 + HTML.** Deterministic ranking and the report. Now usable by a human daily, with zero model cost.
   *This is the milestone worth reaching before anything else — it is independently valuable.*
3. **Verdict capture.** The buttons and `verdicts.jsonl`. Start accumulating ground truth immediately, because
   the comparison in step 5 needs history.
4. **GitLab anchoring.** `semantic_code_search` and MR correlation on emitted findings.
5. **R2 + the A/B.** Add the LLM ranker and the side-by-side columns. Run for ~15 days.
6. **Jira sink.** Last, and only once the threshold has been calibrated against real verdicts. Filing issues from
   an untuned ranker is the fastest way to lose the team's trust.
7. **Schedule it** on the chosen host, and widen beyond `team="cav"` — which is one line, since `team` is a
   stream label.

---

## 12. Open questions

- ~~Can a non-interactive scoped VL token be issued?~~ **Answered:** the token lives in the MCP client config, so
  a plain program can present it. §8 assumes this.
- **What exactly does the token scope cover?** Still open, and the one that matters most. A scope narrower than
  the query returns fewer results rather than an error — a report that looks fine and is quietly incomplete.
  Verify by comparing an MCP result against the same query run in vmui as a human.
- **What is the token's lifetime and renewal path?** A static credential still expires eventually, and expiry at
  06:00 must fail loudly rather than produce an empty report.
- Is `min(_time)` usable directly, or is `row_min` required? (Design uses `row_min`, which is documented.)
- What is the real name and value set of the level field in this deployment? The filled-in VictoriaLogs training
  deck records this — reuse it rather than rediscovering. Field names stay configurable either way.
- Is `trace_id` present and well-populated across `cav` services? The whole blast-radius signal depends on it.
  Check coverage % in the vmui Overview tab before committing to the weights.
- Which Atlassian MCP, and what does its tool surface actually support for search-by-label? Idempotency depends
  on it.
- Retention: does it cover the 7-day baseline plus the 14-day flatness window? If retention is shorter, the
  flatness classifier needs its own small state file.
