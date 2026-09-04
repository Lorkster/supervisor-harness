# Setup examples

Worked setups, end to end, with the reasoning for each choice. The first is the
one this document was written for; the rest are variations on it.

- [Claude Code on Bedrock, and Cursor on corporate SSO, on one machine](#claude-code-on-bedrock-and-cursor-on-corporate-sso-on-one-machine)
- [Adding autonomous runs to that setup](#adding-autonomous-runs-to-that-setup)
- [One store across every project](#one-store-across-every-project)

---

## Claude Code on Bedrock, and Cursor on corporate SSO, on one machine

**The situation.** Claude Code CLI is authenticated to Amazon Bedrock through
environment variables — `CLAUDE_CODE_USE_BEDROCK=1`, a region, and a Bedrock API
key in `AWS_BEARER_TOKEN_BEDROCK`. Cursor and the Cursor CLI are signed in
through a corporate SSO login. Two hosts, two completely different credentials,
the same repositories.

### The short version: the harness needs none of it

In host-delegated mode — the default, and what `init` configures — **the harness
makes no model calls at all**. It writes briefs, scores drift, verifies criteria
and persists the run; the host executes every packet with its own models, its own
tools and its own credentials. Neither the Bedrock token nor the Cursor session
is read by the harness, passed through it, or written to its store.

So there is no provider to configure, no key to copy, and nothing that has to
know Bedrock is involved. `supervisor providers` says as much:

```
routing   every stage -> host
host      available: yes   delegated: yes   host: claude-code
```

Everything below is about the two hosts finding the harness, not about auth.

### 1. Install, once per project

```bash
pip install -e .          # or: pip install supervisor-harness
cd ~/code/your-project
supervisor init --host both
```

That writes seven files, and each host reads a different subset:

| File | Read by |
| --- | --- |
| `.claude/skills/supervise/SKILL.md` | Claude Code |
| `.claude/commands/supervise.md` | Claude Code — the `/supervise` command |
| `.mcp.json` | Claude Code — registers the `supervisor` MCP server |
| `.cursor/rules/supervisor.mdc` | Cursor, and the Cursor CLI |
| `.cursor/commands/supervise.md` | Cursor — the `/supervise` command |
| `.cursor/mcp.json` | Cursor, and the Cursor CLI |
| `supervisor.config.json` | the harness |

Both MCP files carry the same server, because the two hosts look in different
places: Claude Code reads `.mcp.json` at the repository root, and Cursor's
documented project location is `.cursor/mcp.json`. The Cursor CLI picks up the
same MCP servers as the Cursor editor, and reads `.cursor/rules` as well, so one
`init` covers both of them.

Restart each host afterwards; neither rescans for MCP servers while running.

### 2. Use it from either host

From Claude Code or Cursor:

```
/supervise Add rate limiting to the login endpoint so credential stuffing is blocked
```

or just ask for the work to be supervised. The host spawns the sub-agents,
against Bedrock in Claude Code's case and against whatever Cursor is signed in
to in Cursor's — and the harness supervises both identically, because it never
sees the difference.

From the Cursor CLI the same rule applies, since `cursor-agent` loads
`.cursor/rules`.

### 3. Tell the CLI which host it is in

`supervisor init --host both` leaves both `.claude/` and `.cursor/` in the
repository, and host detection reads the environment first and the filesystem
second. Inside a host that is unambiguous — Claude Code and Cursor each set
their own environment variables. From a plain terminal it is not:

| Where you run it | Detected | Confidence |
| --- | --- | ---: |
| inside Claude Code | `claude-code` | 1.0 |
| inside the Cursor CLI | `cursor` | 0.7 |
| a plain terminal, both directories present | `claude-code` | **0.2** |

That 0.2 is the harness saying it is guessing, and the tie is broken
alphabetically rather than meaningfully. It matters only for commands that
record or report the host — `start`, `run`, and the resume-fidelity note that
fires when a run continues under a different one. Set it explicitly where it
matters:

```bash
export SUPERVISOR_HOST=cursor      # or claude-code
```

### 4. Both hosts share one run store

Runs from both hosts land in the same `<project>/.supervisor/`, so
`supervisor runs`, `supervisor status` and `supervisor explain` answer across
them. A run started in Cursor can be resumed from Claude Code: the event log is
the run, and the host is not part of what it depends on.

The one thing that changes is recorded rather than assumed. Resuming a run under
a different host adds a note to the log — *"resumed under a different
environment: host 'claude-code' -> 'cursor'"* — because the thresholds, the
quality bars and the routing all come from the resuming process. It does not
make the resume unfaithful; it removes the silence.

---

## Adding autonomous runs to that setup

Host-delegated covers `/supervise` from either host. `supervisor run` is the
other backend: the harness drives the models itself, with no host involved, which
needs a route to a real provider. On this machine Bedrock is already reachable,
so it is the obvious one.

```bash
pip install 'supervisor-harness[bedrock]'
```

The configuration goes in your **trusted** config, not the project's:

```jsonc
// ~/.supervisor/config.json
{
  "providers": {
    "bedrock": { "type": "bedrock", "region": "eu-west-1" }
  },
  "routing": {
    "default": "host",
    "drift": "bedrock:eu.anthropic.claude-sonnet-4-5-20250929-v1:0|host"
  }
}
```

`providers.*.type`, `region`, `profile`, `base_url` and the API-key settings are
refused from a workspace file on purpose: a repository you have merely pointed
the harness at could otherwise decide where a credentialed request goes. Put
them under your home directory, where you own the file. See
[Which config files are trusted](../README.md#which-config-files-are-trusted).

**Your existing environment is the credential.** The Anthropic SDK's Bedrock
client reads `AWS_BEARER_TOKEN_BEDROCK` exactly as Claude Code does, so a shell
that can run `claude` against Bedrock can run `supervisor run` against it too,
with only the region configured above.

**One thing to know if you also have `AWS_PROFILE` set**, which a corporate AWS
machine usually does. The SDK treats a Bedrock API key and AWS credentials as
mutually exclusive:

> Cannot specify both `api_key` and AWS credentials (`aws_access_key`,
> `aws_secret_key`, `aws_session_token`, `aws_profile`)

The harness used to pass `AWS_PROFILE` through to the SDK, so a machine with both
— a token for Bedrock and a profile for everything else, which is the ordinary
state of the setup at the top of this page — could not construct the provider at
all. It no longer infers the profile from the environment: the SDK resolves the
whole credential chain itself, so nothing was gained by doing so. A profile you
write in the config file is still passed, and if you set both deliberately, the
SDK will tell you.

Check it before a run rather than during one:

```bash
supervisor providers
```

```
bedrock   available: yes   region: eu-west-1   dependency_installed: yes
```

**A cheap local model is the better fit for the `drift` stage** if you have one,
because it is called after every turn. `"drift": "ollama:qwen3.8-code:latest|host"`
falls back to the host when Ollama is not running.

---

## One store across every project

By default each project keeps its own runs in `<project>/.supervisor/`. Point
`SUPERVISOR_HOME` at one directory to share a single store, an index and a
lessons library across all of them:

```bash
export SUPERVISOR_HOME=~/.supervisor
```

Then `supervisor runs` answers across every project, and a lesson learned in one
reaches the briefs of the next — marked as borrowed, and ranked below a lesson
learned in the project actually being worked on.

It is worth pairing with `supervisor delete --older-than 90` or
`supervisor prune-lessons` on occasion: a shared store holds every prompt and
every agent's full output, for every project, indefinitely.

---

## Sources

The two host behaviours above are from the vendors' own documentation, and are
worth re-checking if either changes:

- [Claude Code on Amazon Bedrock](https://code.claude.com/docs/en/amazon-bedrock)
  — `CLAUDE_CODE_USE_BEDROCK`, the region resolution order, and
  `AWS_BEARER_TOKEN_BEDROCK` as "a simpler authentication method without needing
  full AWS credentials".
- [Cursor: Model Context Protocol](https://cursor.com/docs/context/mcp) —
  `.cursor/mcp.json` for a project, `~/.cursor/mcp.json` globally.
- [Cursor CLI](https://cursor.com/docs/cli/using) — the CLI uses the same MCP
  configuration as the editor, and loads `.cursor/rules`.
