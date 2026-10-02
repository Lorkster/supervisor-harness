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

- **Your own config** (`~/.supervisor/config.json`, or `SUPERVISOR_HOME`) is trusted.
- **A config file inside the workspace** (`supervisor.config.json` in a repository)
  is not. The following are ignored there, and a run says which were ignored:
  - turning on command execution;
  - the quality bars and drift thresholds;
  - a provider's `type`, `base_url`, `api_key`, `api_key_env`, `region`, `profile`
    and `params`.

  A repository therefore cannot send your API key to another host, give itself a
  shell, or lower the bar its own work is judged against.
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
- The run store (`.supervisor/`) holds prompts, absolute paths and agent output.
  - Known credential shapes (provider keys, `Authorization` headers) are redacted
    before they are written. Other secrets are not, so treat the store as
    sensitive.
  - It writes its own `.gitignore`, so it is not committed by accident.

### Commands

**Command execution is off by default.** Only your own config can turn it on
(`policy.allow_command_execution`). When it is on:

- Only the project's check runners can be started (`pytest`, `npm`, `make`, …), and
  only by execution agents. Verification runs criterion commands under the same rules.
- There is no shell:
  - no `;`, `|`, `&&`, redirection or globs;
  - no program passed inline (`python -c`, `node -e`);
  - every path a command names must be inside the agent's scope.
- Commands run **without your credentials**: variables named like API keys, tokens,
  secrets and passwords, and the AWS credential variables, are removed from their
  environment.
- A timeout stops the whole process tree, including what a `.cmd` shim started on
  Windows.

**This is not a sandbox.** A check runner runs whatever the repository tells it
to:
- `npm test` runs a line of `package.json`;
- `pytest` imports the repository's `conftest.py`;
- `make` runs its Makefile;
- `npx` and `python -m pip` fetch code that was never in the repository.

Turn command execution on only for repositories you trust, or run the harness in
a container or a VM.

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
