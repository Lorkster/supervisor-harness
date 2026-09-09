# Prompt for the finishing agent

*Paste everything below the rule as the opening message. It is written to work cold, with no prior conversation.*

---

You are finishing a training deck that teaches our team to search our logs in VictoriaLogs. The VictoriaLogs half
is written and verified against upstream docs. The half about **our** deployment is deliberately blank, and that
is your job.

**Files** (in this repo, on `main`):

- `docs/tmp/victorialogs-training/index.html` — the deck. Self-contained, 31 slides, opens from `file://`.
- `docs/tmp/victorialogs-training/HANDOFF.md` — **the spec. Read it in full before you touch anything.** It lists
  every placeholder, what it wants, and which tool produces it.

**Your access that the previous agent did not have:** the source of the services that produce these logs, and the
live VictoriaLogs instance via the hosted `mcp-victorialogs` MCP server.

## What to do

1. **Read `HANDOFF.md` completely.** It is the requirements document; this prompt only adds emphasis.
2. **Fill the `CONFIG` object** at the top of the `<script>` block in `index.html`. That is the single edit point
   for everything site-specific. Three placeholders live in slide prose instead (two curl base URLs, a Grafana
   link) — `grep -o '{{FILL[^}]*}}' index.html` finds them all.
3. **Verify the four UI slides** (8, 9, 11, 12). They carry a `VERIFY` chip because their vmui mocks were drawn
   from the public playground, not from our instance. `HANDOFF.md` lists the exact controls to confirm by name.
   Fix or replace them, then delete the `flag:` line from each slide.
4. **Commit to `main` and push.** One commit is fine. Say in the message what you verified and what you could not.

## Rules that matter more than finishing

- **Every value comes from a query you actually ran against the live instance.** Not from the shipper config, not
  from a Kubernetes manifest, not from reading the service source. Those tell you what *should* arrive; only a
  query tells you what is stored. Use the source code to know what to *look for*, then confirm it is there.
- **Run every cookbook query once** and see it return plausible rows before it goes in the deck.
- **Leave a gap honest rather than fill it with a guess.** A wrong field name in a training deck teaches the whole
  team the wrong field name, and they will trust it for months. An unfilled placeholder is embarrassing; a
  confident wrong answer is expensive. If you cannot verify something, leave the placeholder and list it in your
  report.
- **Do not restructure the deck**, change its design, or delete teaching slides. If our reality contradicts a
  slide — say our logs arrive pre-parsed so `unpack_json` is irrelevant to us — add a sentence to that card rather
  than removing it; people meet those cases in other systems.
- **Keep it self-contained.** No CDN links, no external fonts, no network requests. It must work from a USB stick.
  Embedded base64 images are fine and encouraged.
- **Do not fabricate access.** If the MCP server or vmui is unreachable, stop and say so. Do not infer the answers
  from the source code and present them as observed.

## Tool notes that will save you a round of errors

- `start` is **required** on `query`, `hits`, `facets`, `field_names`, `field_values`, `streams` and both
  `stats_*` tools, and must be **RFC3339 UTC** — `2026-09-09T09:00:00Z`, not `1h`. `end` is optional and defaults
  to the newest log.
- `query` defaults to `limit: 1000`. Lower it. On wide JSON logs, a thousand entries will end your context.
- Every tool is read-only; there is no write path, so you cannot damage anything by exploring.
- Prefer the discovery funnel over guessing: `streams` / `field_names` → `field_values` / `facets` → `hits` →
  `query`. Counts are cheap, logs are expensive.
- Use the `documentation` tool rather than your own recall for LogsQL syntax. It returns resource URIs; read them.
  LogsQL moves, and several widely-circulated summaries of it are wrong — `extract` uses `<name>` placeholders,
  not regexp groups, and `format`, the range/length/IP filters and query options are commonly misquoted too.

## Done means

- `grep -o '{{FILL[^}]*}}' docs/tmp/victorialogs-training/index.html` returns nothing, or the survivors are listed
  in your report with the reason each could not be verified.
- No `flag:` lines remain in `index.html`, or the survivors are explained.
- Slide 1's counter reads `All placeholders filled`.
- The deck opens from `file://` with no console errors and makes no network requests.
- Slides 5, 6, 16, 26 and 28 — the config-driven ones — show no clipped content at 1280×720. Check by loading the
  page and comparing each slide's `scrollHeight` against its `clientHeight`.

## Report back with

1. What you filled in, and the query you ran to establish each non-obvious value.
2. What you could not verify, and why.
3. Anything you found in our logs that surprised you — a field that is a string where it should be a number, a
   service double-encoding JSON, inconsistent level casing. Those belong in `CONFIG.gotchas`, and if you found
   more than three, say so; the array can grow.
4. Anything in the deck's *teaching* content that is wrong for us, so a human can decide whether to change it.
