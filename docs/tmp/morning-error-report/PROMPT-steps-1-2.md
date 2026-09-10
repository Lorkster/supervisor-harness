# Prompt for the agent building steps 1 and 2

*Paste everything below the rule as the opening message. Carry `DESIGN.md` alongside it — the prompt refers to it
as the spec and does not restate it.*

---

You are building the first two milestones of a morning error report over VictoriaLogs. `DESIGN.md` sits beside
this message. **Read it in full before writing any code.** It is the specification; this prompt only sets scope,
the verification bar, and the traps.

## Scope — build exactly these two milestones

**Step 1 — discovery and the evidence bundle.** Stages A and B from the design, writing a versioned
`bundle.json`. No ranking, no report.

**Step 2 — deterministic ranking (R1) and the HTML report.** The R1 formula from §6 and the self-contained HTML
report from §9.

At the end of step 2 the tool must be genuinely useful to a developer every morning, on its own, with **no model
involved in ranking or rendering**. Under Lane A (see constraints) the whole run costs nothing; under Lane B only
collection costs tokens. Either way, `render` from a saved bundle is free and offline — that property is the
point of stopping here, and it is what makes the tool testable. Do not compromise it.

## Explicitly out of scope — do not build these

R2 / any LLM ranking. GitLab anchoring or `semantic_code_search`. Jira, or any issue sink. The scheduler or any
host packaging. Verdict-capture buttons.

Do not build them "while you are in there", and do not add abstractions in anticipation of them beyond the one
concession below. If you think the design is wrong about a later milestone, say so in your report rather than
pre-empting it in code.

**The one concession:** give each finding in the HTML a `data-fingerprint` attribute so step 3 can attach verdict
capture without touching the renderer.

## Constraints that are not negotiable

- **The VictoriaLogs HTTP API is closed.** Access goes through the VictoriaLogs MCP server with a scoped token,
  because it must go through SSO-approved channels. Read §8 of the design — it sets out two lanes, and
  **answering which lane applies is your first task, before writing code**:
  - **Lane A**: a non-interactive scoped token exists and the server runs HTTP transport, so your collector is a
    small MCP client speaking JSON-RPC. No model in the loop, collection costs nothing. Prefer this.
  - **Lane B**: the token cannot leave an interactive session, so an agent runs the queries and writes the
    bundle. Then the five aggregate-only rules in §8 are mandatory, not advisory.
  Whichever lane, `render` must stay pure code over the bundle: the report is free either way.
- **Read-only.** The tool calls only the VL MCP server's read tools (`query`, `hits`, `facets`, `field_values`,
  `streams`). Every one of them is read-only by construction, so exploration cannot damage anything.
- **Runs on a dev machine, one command, no host dependencies.** No Docker required to run it, no CI, no cluster.
- **Self-contained HTML.** No CDN, no external fonts, no network calls from the report. Embedded base64 only.
- **Nothing hardcoded that varies by deployment.** Field names (level field, trace id field), the stream
  selector, the instance URL and the window all come from a config file. Pilot values are
  `{environment="si1.mt1", team="cav"}`, but the code must not assume them.
- Python unless the repo you are working in clearly says otherwise. Standard library plus an HTTP client, and
  under Lane A a minimal MCP client — JSON-RPC `initialize` then `tools/call` is a short file; reach for an SDK
  only if the handshake needs it. Keep dependencies near zero.

## Shape of the CLI

Something equivalent to:

```
report collect --config cfg.yaml --out bundle.json [--explain]
report render  --bundle bundle.json --out report.html
report run     --config cfg.yaml --out report.html     # collect + render
```

- `--explain` prints every LogsQL query it issues, verbatim, so a human can paste it into vmui. The design's own
  training material tells people to demand this; honour it.
- `--replay` semantics come free: `render` never touches the network.
- `bundle.json` carries a `schema_version` from the first commit.

## Traps that will cost you a day each

1. **`copy _msg as raw_msg` before `collapse_nums`.** Otherwise every exemplar in the report is a pattern full of
   `<N>` placeholders that nobody can search for. §4.
2. **Do not fetch the baseline per candidate.** 25 candidates × 7 days is 175 queries. Add `_time:1d` to the
   grouping and get the whole per-day series from **one** query over the 8-day window:
   `| stats by (service_name, _msg, _time:1d) count()`. Rows are sparse, so absent days cost nothing, and the
   shape is exactly what the sparkline wants. Join by fingerprint in code. Reserve `pattern_match()` for
   drill-down: the reproduce-this-finding query shown in the report, and the "still live in the last hour" check.
3. **Check retention before trusting novelty.** If retention is shorter than the baseline window, *every*
   fingerprint looks new and the report is 100% false positives. Detect the effective earliest timestamp, degrade
   to however many baseline days actually exist, and put a visible warning in the report. Do not silently
   proceed.
4. **Windows are explicit and UTC.** "Yesterday" is ambiguous across timezones and DST. The bundle records the
   exact `start`/`end` it used, and the report displays them.
5. **A field that is missing is not a field that is zero.** If `trace_id` coverage is partial, blast radius is
   understated and the ranking silently skews. Measure coverage during collection, record it in the bundle, and
   show it in the report. If coverage is below ~50%, say so loudly — the weights in §6 assume it is good.
6. **`stats` on a high-cardinality group can be expensive.** Keep the discovery `limit` in place, and make the
   candidate cap configurable.
7. **A scoped token that under-covers looks like good news.** If the token's scope is narrower than your query,
   VictoriaLogs returns fewer results rather than an error, and the report reads as a quiet morning. Before you
   trust any number, run one discovery query through the MCP path and the identical query in vmui as a human,
   and confirm the counts match. Record that check in the bundle metadata.

## Verification bar

Do not report this as working until all of the following are true and you have shown the output:

- [ ] `collect` runs against the live instance and produces a bundle; you have eyeballed the fingerprints in vmui
      and they correspond to real error classes rather than to mis-collapsed noise.
- [ ] A **golden bundle** is committed, and R1 scoring is unit-tested against it as a pure function — same input,
      same ranking, every time.
- [ ] `render` works fully offline from that golden bundle.
- [ ] The report opens from `file://` with no console errors and makes no network requests.
- [ ] Every LogsQL query the tool issues appears under `--explain`, and you have pasted at least the discovery
      query and one baseline query into vmui and confirmed the numbers match the bundle.
- [ ] The retention check has been exercised — deliberately request a baseline longer than retention and confirm
      the warning appears rather than a report full of "new" findings.
- [ ] Token scope verified: one query run through the MCP path and the same query run in vmui by a human return
      the same counts.
- [ ] Token expiry fails loudly — the run exits non-zero with a clear message rather than emitting an empty
      report.

State plainly what you ran and what it returned. If something does not work, say so with the output; a partial
result reported honestly is worth more than a claim.

## Report back with

1. Which lane you are on and why — the token answer you got, and from whom. If Lane B, the measured token cost
   of one collection run.
2. The commands to run it, and a real example of the HTML output.
3. The fingerprints you got, and your own judgement on whether `collapse_nums prettify` grouped this deployment's
   errors sensibly — it is the assumption the whole design rests on, and this is the first time anyone will see
   it against real data. If it grouped badly, that is the single most important thing to tell us.
4. Actual `trace_id` coverage, and whether the §6 weights are defensible given it.
5. Anything in `DESIGN.md` that turned out to be wrong, unbuildable, or more expensive than it looked.
