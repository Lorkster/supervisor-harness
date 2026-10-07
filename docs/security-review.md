# Security review, 2026-10-02

A full review of supervisor-harness at `163a965`, done before handing the
harness to a student group. The aim was that students who install and use it
don't inherit a pile of security issues. Every candidate from every source is
listed below with a verdict, and every confirmed issue is fixed in the same
change as this document. [`SECURITY.md`](../SECURITY.md) is the summary for
users.

## How

| Source | What it covered |
| --- | --- |
| Bandit 1.9 | the whole package: 16 findings |
| Local model runs ([security-eval](https://github.com/Lorkster/security-eval)) | four runs on two snapshots (4 files, then 18), conditions baseline, triage of Bandit's output, and the supervised harness itself; `qwen3.8-code` on Ollama |
| Manual review | the toolbox and command fence, path handling, the configuration trust boundary, providers and redaction, the run store, the MCP server, the installer |

Each confirmed issue was reproduced before it was fixed, and each fix has a
test that fails without it (`tests/test_security_review.py`,
`tests/test_http_providers.py`).

The threat model: a student points the harness at a repository they did not
write. Text in that repository can steer the models, and the harness acts with
the student's permissions and credentials.

## Confirmed and fixed

| # | Issue | Severity | Reproduced as |
| --- | --- | --- | --- |
| 1 | **A run id could name a directory outside the store.** `supervisor delete RUN` removed `runs/<RUN>` recursively, and `../../x` named a directory outside the store; `.` named every run. The MCP server's `supervisor_report` created directories the same way. | medium | `delete_run("../../victim")` deleted `victim/` |
| 2 | **A criterion command could carry any program inline.** An agent's shell refused `python -c` and `node -e`, but the verifier running a criterion's command (model output) did not. | medium, when command execution is on | `unsafe_command('python -c "…"')` returned no refusal |
| 3 | **Commands inherited the user's credentials.** Check runners run project code (`npm test`, `conftest.py`, a Makefile), and they ran with `ANTHROPIC_API_KEY` and AWS credentials in their environment. | medium, when command execution is on | `os.environ` in a child process held the keys |
| 4 | **A command timeout did not stop a `.cmd` shim's child.** On Windows `npm`, `npx` and `yarn` are `.cmd` shims, run inside `cmd.exe`; the timeout killed `cmd` and the program kept running. | low | a 1-second timeout returned after 7.1 s |
| 5 | **The search tool could be made to run for hours.** It compiled a model-written regex with no bound; `re` has no timeout. | low | `(a+)+$` on 40 characters hung until the test runner killed it |
| 6 | **A file of any size was read whole** by `read_file` and `search`. | low | by inspection: memory grows with the file |

Fixes:
1. Run ids must be a single name (`checked_run_id`).
2. `unsafe_command` applies the inline-source rule.
3. Both command paths go through `dod.run_bounded`, which removes credential-named
   variables from the environment and kills the whole process tree on timeout.
   That covers issues 3 and 4.
4. `search` refuses nested repetition and overlong patterns, searches each line
   only so far, and stops at a time budget, saying so.
5. Files over 5 MB are refused by `read_file` and skipped by `search`.

## Found by the local runs, not security

| Issue | Effect | Fixed |
| --- | --- | --- |
| Scope paths written by the planner as absolute paths, for lenses and tasks (P7) and for the run envelope (P8) | every agent scored as drifting on turn 1, and was stopped | #75, #76 |
| Tool results cut to 8,000 characters per round without a marker | agents reviewed files they had only partly seen | #76 |
| A repeated turn's findings stored twice | inflated finding counts | #75 |
| Ollama's schema grammar collapsing on long prompts | a 90,000-token review came back as `{"findings": []}`, reading as "found nothing" | here |
| Directory names matched against the whole absolute path | a workspace under a directory called `build` or `target` showed no files | here |

## Not issues, or accepted

| Candidate | Source | Verdict |
| --- | --- | --- |
| `subprocess` imported (B404, ×4) | Bandit | informational |
| `subprocess` call / partial path (B603, B607) in `audit.py`, `baseline.py` | Bandit | false positive: fixed git argv, no model input |
| `subprocess` call (B603) in `dod.py`, `tools.py` | Bandit | the command fence; hardened by fixes 2 and 3 |
| "hardcoded password" `'x'`, `'-'`, `'--'`, `'pass'` (B105, ×4) | Bandit | false positive: argv tokens and an enum value |
| string-built SQL in `store/index.py` (B608, ×2) | Bandit | false positive: table names from a module constant, values bound |
| insecure deserialisation of the config file (CWE-502) | local model | false positive: `json.loads` |
| path traversal in `read_file` / `write_file` / `edit_file` / `delete_file`, including via symlink | local model | false positive: paths are resolved, then checked to be inside the workspace |
| command injection through shell metacharacters in `run_command` | local model | false positive: no shell; metacharacters and globs refused |
| file permissions when writing the config | local model | false positive: it writes an example with no secrets |
| `SUPERVISOR_ROUTE_*` and `OLLAMA_HOST` redirect requests | local model | by design: the environment is the user's and trusted |
| an API key accepted from a workspace config | local model | false positive: `api_key` is stripped from workspace files |
| `tree_wide_git` bypassable with `sh -c` by unscoped agents | local model | false positive since the allow-list became universal; the stale docstring that suggested it is corrected |
| `_walk` symlink race (CWE-367) | local model | outside the threat model: needs a second local actor racing the filesystem during a run |
| no size limit on parsing a config file | local model | negligible: the file is the user's or the repository's own |
| redaction catches only known credential shapes (CWE-532) | local model | accepted and documented: the store is sensitive (`SECURITY.md`) |
| check runners run project code; `npx` and `python -m pip` fetch code | local model, manual | accepted and documented: command execution is off by default, and `SECURITY.md` says to enable it only for trusted repositories or in a container |
| bare extensionless words are not fenced as paths | local model | accepted: documented in `_path_candidates`, bounded by the allow-list |
| a workspace config may choose routing among configured providers | manual | accepted and documented: don't configure a provider a codebase must not reach, or pin routes in the environment |
| on Windows a program name is also looked up in the current directory | manual | within "check runners run project code": a repository able to plant `pytest.bat` can already run code through `conftest.py` |

## Left open by the review, closed since

- **An agent could finish without covering its scope.** In the 18-file run, the
  security lens read 4 files, said in its own self-assessment that it had not
  read the providers, `mcp_server.py` or `install.py`, and was accepted after
  one turn of six. An analysis agent that says "done" with turns left, having
  read under `policy.min_scope_coverage` (half) of its scope, is now sent back
  once with the unread files named. Every analysis agent's coverage is noted
  on the run's log. The setting is protected: a workspace config cannot lower it.
- **Dependencies were not audited.** `.github/workflows/audit.yml` runs
  `pip-audit` over everything a user installs, with every extra (43 packages
  when added, none with a known vulnerability). It runs on every change to
  `pyproject.toml` and weekly, because a new advisory turns a green run red
  without a push.
