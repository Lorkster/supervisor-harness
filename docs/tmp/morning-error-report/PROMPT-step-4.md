# Prompt for the agent building step 4

*Paste everything below the rule as the opening message. Carry `DESIGN.md` alongside it. Steps 1–3 must already
be merged and working.*

---

You are adding **domain anchoring** to a morning error report over VictoriaLogs. `DESIGN.md` sits beside this
message; read §7 and §8 before writing code. Discovery, the bundle, the R1 ranker, the HTML report and verdict
capture are built.

## What this milestone is for

Today a finding says *this error is new and hit 200 traces*. After this milestone it says *this error is new, hit
200 traces, comes from `checkout/payment.go:214`, and that line changed in `!4812` merged six hours before it
first appeared*.

That second sentence is the difference between a report someone reads and a report someone acts on. It is also
the main mechanism the design has for separating a real defect from a logged-and-handled fallback (§7) — the
surrounding code usually settles the question that volume never can.

## Two capabilities, two mechanisms

They look like one feature and are not. Build them separately.

### 4a — Code anchor: which line emits this?

**Recommended primary: local ripgrep over shallow clones.** Zero tokens, deterministic, works offline, no
licensing, and — see the compliance note below — no log content leaves the network. Maintain a
`service_name → repo` map in config, keep shallow clones in a cache directory, refresh on a TTL.

**Turning a fingerprint back into something searchable is the whole problem.** The fingerprint contains `<N>`,
`<UUID>`, `<IP4>`, `<DATETIME>` placeholders where values were interpolated. Split the pattern on its
placeholders and search for the **longest static run**.

This works for a reason worth understanding: the placeholders sit exactly where the format verbs are in the
source. A log line `upstream checkout timeout after 30s` from `log.Error("upstream %s timeout after %ds", ...)`
fingerprints to `upstream <W> timeout after <N>s`, and its longest static run — `timeout after` — is present
verbatim in the source. Searching the *whole* message never matches; searching a run that spans a verb never
matches. Splitting on placeholders is what makes it work.

Expect this to fail sometimes: messages assembled from variables, wrapped errors, messages emitted by a
dependency rather than your code. **Report no anchor rather than a wrong one.** A confident link to the wrong
line is worse than a blank field, because it sends someone to read code that is not involved.

**Optional enhancement: `semantic_code_search`** via a GitLab MCP, for the cases ripgrep misses. Treat it as a
fallback, not the primary — see the two risks below.

### 4b — Change correlation: what shipped just before this appeared?

This needs the API. For each emitted finding, take `first_seen` from the bundle and ask GitLab for merge requests
merged into the default branch of the mapped repo in the window `[first_seen - 24h, first_seen]`. Show them with
author and title. Correlation, not causation — label it as such in the report, and do not rank on it in this
milestone.

## Two risks to resolve before you build on the MCP

1. **Licensing.** `semantic_code_search` is an AI feature and may require a tier or a Duo licence you do not
   have, and may not exist at all on a self-managed instance. Check what your GitLab actually exposes before
   designing around it. If it is unavailable, 4a via ripgrep still delivers the milestone.
2. **Compliance — raise this before writing the code.** The reason the official Atlassian MCP is off-limits for
   this project may apply here too. Sending an error message to a semantic search backed by an AI service is log
   content leaving your network. Ripgrep over a local clone avoids the question entirely; the API call for 4b
   sends only a timestamp and a project id, which is a much easier case to make. Get the constraint stated by
   whoever owns it rather than assuming either way.

## Cost discipline

**Anchoring runs after ranking, only for findings that will be emitted.** Roughly 3–5 per run, not 25. If you
find yourself anchoring every candidate, you have put the stage in the wrong place in the pipeline.

Anchor results are cached by fingerprint — the same error tomorrow does not re-clone, re-search or re-query.
Cache invalidates when the repo's default branch head changes.

## Out of scope

R2 or any LLM ranking. Jira. The scheduler. Any change to R1's scoring — the deploy correlation is displayed,
not scored, in this milestone.

## Verification bar

- [ ] For at least three real findings, the anchor points at a line that a human agrees emits that message.
- [ ] For at least one finding where the anchor is genuinely ambiguous, the report shows no anchor rather than a
      guess.
- [ ] Change correlation returns plausible MRs for a finding whose first-seen is known to follow a deploy — and
      returns nothing, cleanly, for one that does not.
- [ ] The cache works: a second run against the same bundle issues no new clones or searches.
- [ ] Steps 1–3 tests still pass and the golden-bundle R1 ranking is byte-identical — you have changed no scoring.
- [ ] The report still opens from `file://` with no network requests. Anchor data is baked in at render time.

## Report back with

1. Anchor hit rate across a real run: how many emitted findings got a code anchor, how many correctly.
2. Which mechanism you used, and what your GitLab actually supports.
3. The compliance answer you got, and from whom.
4. Cases where the fingerprint could not be turned into a searchable literal — those are a signal about the log
   messages themselves and may be worth fixing at source.
