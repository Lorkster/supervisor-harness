# Security

## Reporting a vulnerability

Report it privately through GitHub's
[private vulnerability reporting](https://github.com/Lorkster/supervisor-harness/security/advisories/new),
not in a public issue. Include what you ran, what happened, and what you expected.

## What the harness protects, and what it does not

The harness drives models that read, and sometimes change, a repository. The
repository may be one you did not write, and text in it can steer a model. This
is what stands between that and your machine, stated as precisely as the code
enforces it.

### Configuration

- **Your own config** (`~/.supervisor/config.json`, or `config.json` under
  `SUPERVISOR_HOME`) is trusted.
- **A config file inside the workspace** (`supervisor.config.json`, or
  `.supervisor/config.json`, in a repository) is not. The following are ignored
  there, and `supervisor providers` says which were ignored or corrected:
  - turning on command execution (`policy.allow_command_execution`);
  - the quality bars (`require_tests`, `require_security_review`,
    `require_code_quality`, `require_negative_test`, `require_liveness_review`,
    `require_fails_before`, `min_dod_criteria`), the checkpoint pass mark, the
    drift settings (`drift_threshold`, `drift_hard_threshold`,
    `model_drift_check`, `drift_check_every`), `min_scope_coverage` and
    `clean_check`;
  - how tasks are approved and reviewed: `approval`, `veto_review` and
    `requirements_ledger`;
  - what a run may spend, and for how long: `max_parallel_agents`,
    `default_max_turns`, `execution_max_turns`, `max_checkpoint_iterations`,
    `max_task_attempts`, `max_coordinator_decisions`, `max_unreported_dispatches`,
    `max_analysis_lenses`, `max_lessons_in_brief`, and `envelope_max_age_days`
    (how long a grant stands, so a workspace cannot keep its own alive);
  - `backend`, which decides whether the harness answers for itself;
  - `tool_servers`, because the harness runs each one's command;
  - `home`, which says where run history is written;
  - a provider's `type`, `base_url`, `api_key`, `api_key_env`, `region`, `profile`,
    `params` and `default_model`.

  A repository therefore cannot send your API key to another host, give itself a
  shell, spend your API budget, approve its own work, or lower the bar its own
  work is judged against.
- The scope envelope is the one run limit a workspace file may set, and it can
  only **narrow** it. `scope_envelope` entries are kept only if they lie inside
  the envelope you already have; `scope_envelope_forbidden` is your list plus the
  workspace's, never replacing yours. An empty list from a repository neither
  widens the envelope nor empties your forbidden paths.
- A workspace config that is not valid UTF-8, or cannot be read, is ignored whole
  and reported rather than failing the load. `max_parallel_agents`,
  `default_max_turns` or `execution_max_turns` below 1, from any file, is raised
  to 1 and reported (a zero would admit no agent, or mean "unlimited" to one
  reader and "no turns" to another).
- A workspace file **may** add an analysis lens, but not redefine an existing one.
- A workspace config **may** choose routing: which of *your configured* providers
  and models analyse it. If a provider must never see a given codebase, don't
  configure it, or pin the route with `SUPERVISOR_ROUTE_*` environment variables.

### Agents and files

- Agents can **read** any file in the workspace, including `.env` files.
  - What they read goes to the model provider the run is routed to.
  - A symlink that points outside the workspace is not followed.
  - Files over 5 MB are not read whole.
- Only **execution** agents can **write**, and only inside their scope. No agent
  writes into `.git`, `.hg`, `.svn` or the harness's own store, whatever its scope
  says.
- On the autonomous backend, an execute-mode run works in a git worktree on its
  own branch, `supervisor/<run id>`. Your working tree is not written, and nothing
  is pushed. A host-delegated run works in place: the harness cannot fence a
  host's own tools, so your host's permission model is the fence there.
- The run store (`.supervisor/`) holds prompts, absolute paths and agent output.
  - Known credential shapes are redacted before an event is written to the log:
    provider keys, GitHub, GitLab, npm, Hugging Face and Google OAuth tokens, AWS
    key ids, JWTs, private-key blocks, `Authorization` and `x-api-key` headers,
    passwords inside URLs, and values assigned to secret-named variables, among
    others. A secret in any other shape is not caught.
  - What agents wrote to `results/` and the briefs in `packets/` are files, not
    events, and are **not** redacted.
  - Treat the store as sensitive.
  - It writes its own `.gitignore`, so it is not committed by accident.

### Commands

**Command execution is off by default.** Only your own config can turn it on
(`policy.allow_command_execution`). When it is on:

- Only the project's check runners can be started (`pytest`, `npm`, `make`, …), and
  only by execution agents. Verification runs criterion commands under the same rules.
- There is no shell:
  - no `;`, `|`, `&&`, redirection or globs;
  - no program passed inline: `python -c`, `node -e`, `python -m timeit`, `runpy`,
    `code` or `pdb`, and a Node `--import`, `--require`, `-r`, `--loader` or
    `--experimental-loader` whose value has a URL scheme (`data:`, `https:`). Those
    Node flags with a plain file name are allowed;
  - every path a command names must be inside the agent's scope.
- The path fence reads what the OS would. `..` is collapsed before a path is
  checked; the floor (`.git`, `.hg`, `.svn` and the harness's store) is compared
  case-insensitively and ignores trailing dots and spaces; values attached to short
  options (`-C../..`) are checked as well as `--flag=path`; and the program's own
  path is checked: a UNC path, or one outside the workspace, is refused for agents.
- When the program is a `.cmd` or `.bat` shim (`npm`, `npx` and `yarn` on Windows),
  an argument containing `&`, `|`, `<`, `>`, `^`, `%`, `!`, `"`, `(`, `)` or a
  line break is refused, because `cmd.exe` would run it as a second command even
  inside quotes.
- Commands run **without your credentials**: variables whose name ends in
  `API_KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `CREDENTIAL(S)`, `_KEY`,
  `_PAT`, `_PASS`, `_AUTH` or `_DSN`, or contains `API_KEY`; those starting `AWS_`,
  `GOOGLE_APPLICATION`, `ANTHROPIC_`, `OPENROUTER_` or `OPENAI_`; `DATABASE_URL`
  and `SSH_AUTH_SOCK`; the `api_key_env` of every provider you configured; and the
  harness's own `SUPERVISOR_*` settings, are removed from their environment.
- A timeout stops the whole process tree, including what a `.cmd` shim started on
  Windows.
- Output is read as it is written, decoded as UTF-8, and only the first and last
  million characters of each stream are kept, with a marker where the middle was
  cut.
- The harness's own `git` calls run with `core.fsmonitor` off, `core.hooksPath`
  pointed at an empty directory and `--no-optional-locks`, in the same scrubbed
  environment, with `git` found only on absolute `PATH` entries (never the current
  directory). Its commits use `--no-verify`. A repository's `.git/config` and hooks
  therefore do not run when the harness looks at it.

**This is not a sandbox.** A check runner runs whatever the repository tells it
to:
- `npm test` runs a line of `package.json`;
- `pytest` imports the repository's `conftest.py`;
- `make` runs its Makefile;
- `npx` and `python -m pip` fetch code that was never in the repository.

Turn command execution on only for repositories you trust, or run the harness in
a container or a VM.

The file tools have their own limits:
- `search` refuses a pattern that repeats a group holding an alternation or another
  repetition, and any bounded repeat of a group; the match then runs in a separate
  process that is killed when the time budget ends. A pattern like `a*a*a*b` passes
  the check and is bounded by that kill.
- `write_file` on Windows refuses a `:` in a name (an alternate data stream), a
  device name (`CON`, `aux.txt`) and a name ending in a dot or a space. `edit_file`
  replaces the file atomically.
- `supervisor delete` and the cutoff sweep give back a run's worktree and branch
  only if the recorded worktree is exactly `<store>/runs/<id>/worktree` and the
  branch exactly `supervisor/<id>`: a repository can ship a planted
  `.supervisor/runs/*/state.json`, and nothing it names is trusted. Stopping the
  processes inside a directory refuses a drive root, your home directory and
  anything above it.
- The clean-checkout verification (`policy.clean_check`) installs dependencies and
  runs criteria, so it is skipped, with a note on the run, when command execution
  is off.

### Analysis tool servers

An MCP server you name under `tool_servers` in your own config is started by the
harness, with your rights, and lends its tools to a run's agents. By default only
tools that change nothing outside the server's own session are lent, every path
argument is held to the run's tree and the `read_roots` you list, and results
reach agents marked as data. Arguments are checked wherever they sit, in nested
objects and lists and under any key, not only where the schema names a path. A NUL
byte and a `file:` address on another machine are refused. A network tool keeps
the URLs it is given. Name only servers you would run yourself.

### The MCP server

The MCP server takes instructions from your host agent:
- A run id must be a single name, and cannot point outside the store.
- A result is read only from that run's `results` directory.

The host agent itself runs under your host's own permission model.

## Reviewed

A full review on 2026-10-02 covered:
- the toolbox, path handling and the command fence;
- the configuration trust boundary;
- the providers and redaction;
- the run store, the MCP server and the installer.

It used Bandit over the whole package, local-model runs through
[security-eval](https://github.com/Lorkster/security-eval), and manual review.
Everything it found is fixed above or listed here as a stated limit; the full
triage is in [`docs/security-review.md`](docs/security-review.md).

That review predates the run envelope, envelope approval, the run's own branch
and the analysis tool servers described above. A second review on 2026-11-08, of
the working tree of `review/code-and-docs-2026-11`, read the security fences and
file I/O, the store and its concurrency, and the orchestration logic, and its
confirmed findings are fixed above; see
[Second review, 2026-11-08](docs/security-review.md#second-review-2026-11-08) for
what was found and what was left.

The dependencies, with every extra, are checked with `pip-audit` on every change
to them and weekly ([`audit.yml`](.github/workflows/audit.yml)).
