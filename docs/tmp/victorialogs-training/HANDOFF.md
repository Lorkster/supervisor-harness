# Handoff: finish the VictoriaLogs training deck

**Deliverable:** `index.html` in this directory — a self-contained 31-slide presentation that teaches our team
to search our logs in VictoriaLogs. Open it by double-clicking; no server, no build, no network.
Arrow keys / space to move, `g` for the slide grid, `n` for speaker notes, `f` for fullscreen; Ctrl/Cmd+P prints
to PDF, one slide per page.

**State:** the VictoriaLogs half is written and verified against the upstream docs (September 2026). The
*our-deployment* half is deliberately blank. There are **73 placeholders**, rendered as yellow chips, and slide 1
shows a live count of how many remain.

**Your job:** replace every placeholder with a fact you have verified against the live instance, then commit.
The deck is done when the counter on slide 1 reads `All placeholders filled`.

---

## The one edit point

Everything site-specific lives in a single `CONFIG` object at the top of the `<script>` block in `index.html`,
under the banner `EDIT ME`. **You should not need to touch anything else.** The slides read from `CONFIG` and
render it.

Find what is left at any time:

```bash
grep -o '{{FILL[^}]*}}' docs/tmp/victorialogs-training/index.html | sort | uniq -c | sort -rn
```

Three placeholders live in slide prose rather than in `CONFIG` (a curl base URL used twice, and a Grafana
dashboard link). `grep` will find them; edit them in place.

---

## What each field wants, and how to get it

Use the hosted `mcp-victorialogs` MCP server. Every read tool requires `start` as an **RFC3339 UTC timestamp** —
`2026-09-09T00:00:00Z`, not `1h`. `end` is optional.

| `CONFIG` key | What to put | How to find it |
|---|---|---|
| `org` | Team or org name as the team says it | Ask, or take it from the repo |
| `vlUrl` | API base, no trailing path | Deployment config / ingress |
| `vmuiUrl` | `<vlUrl>/select/vmui/` | Confirm it loads |
| `mcpUrl` | The hosted MCP endpoint | The MCP client config |
| `tenant` | `AccountID:ProjectID`. `0:0` unless we are multi-tenant | `flags` tool; or the tenant selector in vmui |
| `retention` | e.g. `30d` | `flags` tool — look for `-retentionPeriod` |
| `clusterOrSingle` | single-node or cluster | `flags` tool, plus the deployment manifests |
| `volume` | Logs/day and bytes/day, rounded | Overview tab: logs/sec × 86400; or `hits` over 24h |
| `shipper` | What ships our logs | Look for the `collector` field's values, and the k8s manifests |
| `streamFields[]` | The fields that actually form `_stream`, most-used first, each with what it identifies and rough cardinality | `stream_field_names`, then `stream_field_values` per field for the count. **Do not read this off the shipper config** — what is configured and what is stored differ. |
| `keyFields[]` | Level, trace/correlation id, HTTP status, latency. Exact field names, their value sets, and what each is good for | `field_names` for names, `field_values` for the value sets. Note the **coverage %** in the Overview tab — say so in `desc` if a field is only on some services. |
| `msgShape` | One sentence: is `_msg` already-unpacked JSON, logfmt, free text, or does it vary per service? | Read 20 raw entries in JSON mode across several services |
| `services[]` | Our services with their `{...}` stream selector and a note on what their logs look like | `streams` tool; cross-check against the repos you have access to. Extend the array beyond three if we have more — the slide renders however many you give it. |
| `cookbook[]` | Six real queries for six real questions. `q` is the LogsQL, `why` is when to reach for it | Write them, **then run each one** and confirm it returns sensible rows. Replace any of the six suggested questions if a more useful one exists for us. |
| `gotchas[]` | Traps in our data that have cost someone time | The interesting ones: a field that is a string where you would expect a number, a service that double-encodes JSON, a field renamed mid-history, a shipper that drops something, a level field with inconsistent casing. Find them by querying, not by guessing. |

---

## Rules

1. **Every value comes from a query you actually ran.** Not from the shipper config, not from a manifest, not
   from the service source. Those tell you what *should* arrive; only a query tells you what is stored.
2. **Every cookbook query gets executed once** and seen to return plausible rows before it goes in. A query in a
   training deck that returns nothing teaches the team that the tool is broken.
3. **Leave a gap honest rather than fill it with a guess.** A wrong field name here teaches the whole team the
   wrong field name. If you cannot verify something, leave the placeholder and list it in your summary.
4. **Do not restructure the deck.** If our reality contradicts a teaching slide — say our `_msg` is always
   pre-parsed so `unpack_json` is irrelevant to us — add a sentence to the relevant card rather than deleting the
   slide; people will meet those cases in other systems.
5. **Keep it self-contained.** No CDN links, no external fonts, no images. It must work from a USB stick.

---

## Extras worth adding if you have the access

These are not required, but they raise the deck from good to genuinely ours:

- **A real screenshot** of our vmui Query tab, base64-embedded, replacing the mock on slide 9. Keep the numbered
  legend beside it.
- **A seventh and eighth cookbook entry** drawn from the last two incidents — the queries someone actually ran.
- **Per-service notes** in `services[]` that name the specific log lines worth knowing: the one that means a
  retry, the one that means a circuit breaker opened.

## Acceptance checklist

- [ ] `grep -c 'FILL' index.html` returns 0 matches inside `CONFIG` and slide prose
- [ ] Slide 1 counter reads `All placeholders filled`
- [ ] Every query in `cookbook[]` has been run against the live instance and returned rows
- [ ] Every field name mentioned appears in `field_names` output for a real time range
- [ ] Deck opens from `file://` with no console errors and no network requests
- [ ] Slides 5, 6, 16, 26, 28 (the config-driven ones) show no clipped content at 1280×720
