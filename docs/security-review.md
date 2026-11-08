# Security review, 2026-10-02

A full review of supervisor-harness at `163a965`, done before handing the
harness to a student group. The aim was that students who install and use it
don't inherit a pile of security issues. Every candidate from every source is
listed below with a verdict, and every confirmed issue is fixed in the same
change as this document. [`SECURITY.md`](../SECURITY.md) is the summary for
users.

*Last verified on 2026-11-08 against the working tree of branch
`review/code-and-docs-2026-11` (the commit is still `8f34f4e`): every function and
limit named below still exists as described (`checked_run_id`, `unsafe_command`,
`run_bounded`, `child_environment`, `tree_wide_git`, `_path_candidates`, `_walk`,
`MAX_FILE_BYTES`, the `search` bounds), though the rules in fixes 3 and 4 have been
widened since, as [the second review](#second-review-2026-11-08) records. The first
review itself is not repeated: [what it did not cover](#added-since-this-review)
is listed at the end.*

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
1. Run ids must be a single name (`checked_run_id`, in `store/runstore.py`): one
   to 128 characters of letters, digits, `_`, `.` and `-`, starting with a letter
   or digit, with no `..`.
2. `unsafe_command` (in `core/dod.py`) applies the inline-source rule.
3. Both command paths go through `dod.run_bounded`, which runs the command with
   `child_environment()` and kills the whole process tree on timeout. That covers
   issues 3 and 4. The names removed are those ending in `API_KEY`, `TOKEN`,
   `SECRET`, `PASSWORD`, `PASSWD` or `CREDENTIAL(S)`, the AWS access-key, secret
   and session-token variables, and anything starting `ANTHROPIC_`,
   `OPENROUTER_` or `OPENAI_`. Since the review it removes `SUPERVISOR_*` as well
   (a check inherited the run's `SUPERVISOR_HOME` and failed for it), and the
   second review widened the rule (R7 below).
4. `search` (in `core/tools.py`) refuses a pattern over 300 characters or one
   that repeats a group that itself repeats (`risky_repetition`, a heuristic that
   errs towards refusing; it was `_NESTED_REPETITION` when this was written),
   searches only the first 2,000 characters of each line, and stops after 15
   seconds, saying so. The second review also moved the match into a process
   that is killed at that limit (R9 below).
5. Files over `MAX_FILE_BYTES` (5,000,000 bytes) are refused by `read_file` and
   skipped by `search`.

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

## Second review, 2026-11-08

A code review of the working tree of branch `review/code-and-docs-2026-11`, on
top of `8f34f4e`, after the autonomy work this document lists under
[Added since this review](#added-since-this-review) had landed. It is not the
full review of 2026-10-02 repeated: it read what had changed and what that
review had not reached.

**What it covered.** Three read-only reviewers, each on one third of the package,
and a fourth pass over the documents and diagrams:

| Reviewer | Read |
| --- | --- |
| security fences and I/O | the toolbox and command fence, `core/dod.py`, `core/external.py`, `core/baseline.py`, `core/worktree.py`, `core/processes.py`, `config.py`, `store/redaction.py`, `install.py`, the eval harness |
| store, concurrency and performance | `store/` (log, lock, snapshot, index, lessons), `serde.py`, `ids.py`, the fold, and what a long run costs |
| orchestration logic | `core/supervisor.py`, `core/phases.py`, `core/drift.py`, `contracts.py`, `cli.py`, `mcp_server.py` |
| documents and diagrams | every claim in the docs and both rendered diagrams against the code |

About 45 findings were confirmed. Each was reproduced before it was fixed, and
each fix has a test that fails without it, in `tests/test_review2026_*.py`
(one file per area; the file names say which).

### Confirmed and fixed: security

Severity is the reviewers', except where marked \*, which this document rates
itself because the report did not.

| # | Issue | Severity | Fixed in |
| --- | --- | --- | --- |
| R1 | **An unrecognised approval decision approved the task.** `parse_decision` turned anything it did not know (`"rejct"`, `"deny"`, an empty string) into an approval, so a typo for "reject" started work the owner had refused. | high | `parse_decision` returns `None`; `Supervisor.approve` reports the entry in `detail.not_applied` and the CLI refuses `--task ID:word` with exit 2. `test_review2026_core_approve.py`, `_core_cli.py` |
| R2 | **`approve` had no phase guard.** A stray call restarted a finished run, re-recorded its lessons, and flipped a task under the agent that was working on it. | high | `Supervisor.approve` acts only in `awaiting_approval` (or to renew a stale grant) and only on a task that is still `proposed`; anything else comes back unapplied. `test_review2026_core_approve.py` |
| R3 | **A model could loop for ever on a tool it was not offered.** A conversation offered only its finishing tool, whose model kept calling another, neither advanced the call count nor counted as idle: 41 calls and still going. | high | `Supervisor._work_a_stint` counts an answer made only of refused calls as idle. `test_review2026_core_stint.py` |
| R4 | **`cmd.exe` ran arguments given to a `.cmd` shim.** On Windows `npm`, `npx` and `yarn` are batch files, and `cmd.exe` reads `&`, `|`, `<`, `>`, `^`, `%` and `!` even inside the quotes Python adds: `npm --version "&echo>marker"` ran a second command, past the metacharacter fence, which only sees the shell's quotes. | high | `dod.shim_refusal`, applied by `unsafe_command`, `Toolbox._scope_refusal` and `run_bounded`. `test_review2026_security_commands.py` |
| R5 | **A planted `state.json` could make `supervisor delete` remove any directory.** A run's recorded worktree and branch were read from `.supervisor/runs/*/state.json`, which a repository can ship; the cutoff sweep and `delete` acted on them, and stopped every process inside the named directory first. | medium-high | `worktree.release_refusal`: only exactly `<store>/runs/<id>/worktree` and branch `supervisor/<id>` (not a link). `processes.too_broad_to_clear` refuses a drive root, the home directory or anything above it. `test_review2026_security_git.py` |
| R6 | **The path fence was lexical.** `src/../../x` started with `src/`; `.GIT`, `.git.` and `.git ` are `.git` on NTFS; `-C../..` attached a path to a short option; the program's own path (`//evil/share/python.exe`, `../python`) was never judged. | medium | `collapse` before every scope and floor check; `Toolbox._floor_refusal` compares case-insensitively without trailing dots and spaces; `_path_candidates` reads attached values; `_program_refusal` and `dod._foreign_program`. `test_review2026_security_commands.py`, `_security_files.py` |
| R7 | **The credential scrub had gaps.** A check inherited variables ending `_KEY`, `_PAT`, `_PASS`, `_AUTH` or `_DSN`; those starting `AWS_` (only three were named) or `GOOGLE_APPLICATION`; `DATABASE_URL` and `SSH_AUTH_SOCK`; and whatever `api_key_env` a configured provider named, which no pattern can know. | medium | `dod._CREDENTIAL_NAME`, `register_secret_env`, `child_environment`. `test_review2026_security_commands.py` |
| R8 | **Git ran a repository's own commands.** `core.fsmonitor` in `.git/config` is run by `git status`, a hook by `commit` and `worktree add`, and a `git.exe` in the current directory is found first on Windows. Read-only lookups were as exposed as writes. | medium | every harness git goes through `baseline.git_argv`: `core.fsmonitor=false`, an empty `core.hooksPath`, `--no-optional-locks`, git found on absolute `PATH` entries only, the scrubbed environment; commits use `--no-verify` (`worktree.close`). `test_review2026_security_git.py` |
| R9 | **The ReDoS guard missed shapes it was written for.** `(a|aa)+$`, `(a?){34}a{34}` and other bounded repeats of a group were not refused, and nothing stopped a match once it had started: `re` never releases the GIL, so no thread could be abandoned. | medium | `tools.risky_repetition` refuses a repeated group holding an alternation or another repetition and any bounded repeat of a group; the match runs in a worker process killed at the time budget (`_grep`). `test_review2026_security_files.py` |
| R10 | **A workspace config could widen the envelope, disable expiry, flip the backend and spend the owner's budget.** Lists were replaced by a plain merge, so `scope_envelope: []` meant the whole workspace; `envelope_max_age_days`, `backend`, parallelism, turn counts and attempt bounds were settable from a repository; so was a provider's `default_model`. | medium | `config._narrow_scope`; `PROTECTED_SETTINGS` and `PROTECTED_PROVIDER_KEYS` widened; a bound below 1 is raised and reported; a non-UTF-8 workspace file is ignored and reported. `test_review2026_core_config.py` |
| R11 | **The clean-checkout verification ran with command execution off.** `Supervisor._check_clean` did not test `policy.allow_command_execution`, so a verifier-passed command criterion and a `package-lock.json` were enough for the harness to run `npm ci` and that command on a snapshot of the tree. (This was the entry marked open in this document at `8f34f4e`.) | medium\* | `_check_clean` is skipped, with a note on the run, when command execution is off. `test_review2026_core_clean_gate.py` |
| R12 | **Analysis tool arguments were fenced only where the schema named a path.** `{"target": "../../../Windows/win.ini"}`, a path inside a nested object or list, a NUL byte and a `file:` address on another machine all went through. | medium-low | `external._Fence` walks dicts and lists, any key; `fence` refuses NUL and off-machine `file:` URLs, and keeps URLs for a network tool. `test_review2026_security_files.py` |
| R13 | **Inline source got through the allow-list.** `python -m timeit "..."`, `runpy`, `code`, `pdb`, and `node --import data:...` / `--require https://...` carry a program in the command line as `-c` does. | medium\* | `dod._PYTHON_SOURCE_MODULES`, `dod._preloaded_url`, in `inline_source_flag`. `test_review2026_security_commands.py` |
| R14 | **Command output was unbounded and decoded with the locale's code page.** A command could fill the harness's memory inside its timeout. | low-medium\* | `dod._Capture` keeps one million characters from each end, with a marker; output is decoded as UTF-8. `test_review2026_security_commands.py` |
| R15 | **Redaction missed common credential shapes, and was quadratic on a private-key header.** GitLab, npm, Hugging Face and Google OAuth tokens, JWTs, passwords in URLs, quoted `Authorization`, `x-api-key`, secret-named assignments; tuples, sets, bytes and dict keys were not walked; 552 KB of unmatched headers took 14.5 s. | low-medium\* | `store/redaction.py`: the patterns, a 4,096-character cap on a key body, linear time. `test_review2026_store_redaction.py` |
| R16 | **Windows names that are not files.** `a.txt:ads` writes an alternate data stream, `CON` and `aux.txt` open devices, and `name.` is `name`; `edit_file` could leave a file half-written. | low\* | `tools.windows_name_refusal` (applied on Windows); `edit_file` swaps in a temporary file (`_replace_atomically`). `test_review2026_security_files.py` |
| R17 | **The eval harness trusted its inputs.** Fixture files could be written outside the workspace; a repository or commit starting with `-` was read by git as an option; a timed-out acceptance command left its children running. | low\* | `evals/fixtures.py`, `evals/runner.py`, `evals/score.py`. `test_review2026_store_evals.py` |
| R18 | **An inspection criterion read through a link that leaves the workspace, and a huge file whole.** | low\* | `dod` inspection: links are not followed, the walk is pruned and capped, `MAX_FILE_BYTES` applies. `test_review2026_security_files.py` |

### Fixed in the same pass: not security

Correctness and cost findings, each with a test in `tests/test_review2026_*.py`:

- **A model's `"false"` read as true** in a checkpoint or drift answer (`contracts._bool`).
- **A dependency that could never be satisfied**, a cycle or a self-reference, was
  approved and the run ended "approved, not finished"; it is dropped when the plan
  is proposed, and a rejected dependency is dropped when its dependent is approved.
- **A clean `done` on the last allowed turn was refused**, **mechanical lessons were
  recorded twice** on re-entering `improving`, and **a continuation's brief named
  the first turn's result file**, so a follow-up overwrote it.
- **Blocking work ran on the event loop**: project tests, an install and an agent's
  shell command, each inside a coroutine.
- **CLI**: no-such-run is exit 2 everywhere; `report` and `abandon` exit 1 when
  nothing was recorded; `approve` exits 1 if any decision was not applied; read-only
  commands no longer create `.supervisor/`; `supervisor mcp -w` reaches the server;
  stdin is read as UTF-8; `uninstall --purge` prunes git's worktree registrations.
- **The store**: a torn multibyte write no longer makes the log unreadable; a lesson
  holding U+2028 survives the next rewrite; ids from one process are strictly
  increasing (200,000 in a loop gave 56 duplicates); a stale lock is not broken
  after it was re-taken; two sessions on one run no longer hide each other's events;
  confidence decay is one flat step per run, not compounding.
- **Cost**: the snapshot was serialised, fsynced and renamed on every emit (a run was
  quadratic in its length); it is now compact and written every 100 events and at
  phase edges. `open` folds only the tail; the index syncs only what moved;
  `from_jsonable`, the fold and contradiction detection stopped rescanning.

### Accepted, or left

| Candidate | Verdict |
| --- | --- |
| Deduplicating the weak-criteria list | skipped: not fixed in this pass |
| The snapshot still duplicates turn data that is on the log | left: it is a cache, and compact now |
| `supervisor explain` still reads the whole log | left: it answers "how did it get here" from the record, by design |
| `evals/score.py` keeps `shell=True` for the owner's acceptance commands | accepted: they are the owner's own command lines, and it now kills the whole process tree on timeout |
| The regex scan cannot see `a*a*a*b` | accepted: polynomial, and the worker kill bounds it at the time budget |
| Node `--import`, `--require`, `-r`, `--loader` are refused only with a URL-scheme value | accepted: a file name is a file the repository holds, which is "check runners run project code" |
| Agent result files under `results/` and briefs under `packets/` are not redacted | accepted and documented in `SECURITY.md`: redaction is applied to events, the one record every claim is judged against |
| `uninstall --purge` runs `git worktree prune` as a bare `git`, not through `baseline.git_argv` | noticed after the fixes; not changed |

## Added since this review

The review was of `163a965`. Everything below was built afterwards, and none of it
has had a review like this one. It is listed so that nobody reads "reviewed"
as covering it. [`autonomy-plan.md`](autonomy-plan.md) has the design of each.

- **Envelope approval** (`policy.approval: "envelope"`, `supervisor run
  --grant-envelope`). The harness approves tasks its deterministic gate passes.
  `approval`, `veto_review` and `requirements_ledger` are protected settings, so a
  workspace cannot turn the mode on or the review off; the mode refuses to start
  without command execution, a baseline commit, the run's branch, `require_tests`
  and `require_fails_before`.
- **Tool servers** (`tool_servers`, `core/external.py`). The harness starts a
  command from the owner's own config and lends its tools to the run's agents.
  The setting is protected; a repository cannot add one. What a lent tool may do
  is the owner's per-server choice (`allow: read-only` or `all`), and every path
  it names is held to the run's tree and the server's `read_roots`.
- **The run's branch and worktree** (`policy.execution_worktree`, on by default).
  An autonomous execute-mode run works in a worktree under `.supervisor/runs/`,
  never in the owner's tree, installs the project's dependencies there (`npm ci`,
  under the command-execution switch), and commits at the end. It never pushes.
- **The clean-checkout check** (`policy.clean_check`, `core/clean.py`). Runs the
  final gates' checks again in a scratch worktree of what the run would commit,
  with the project's dependencies installed (`npm ci`). It runs project code
  through `run_bounded`, and it waits on `policy.allow_command_execution` as the
  run's own worktree install does: with the switch off it is skipped and the run
  says so. It did not wait at `8f34f4e`; see R11 in the second review.
- **Page observation** (`core/observe.py`, the optional `observe` extra). Builds
  the product with the run's own command, serves it from a local server and
  photographs it in a headless browser. The build waits on the command-execution
  switch and goes through `run_bounded` and `unsafe_command`; the browser opens a
  page the run just wrote.
- **Model judgements that can only tighten**: the veto reviewer, the goal
  expectations and the coordinator. Each is a model's output acting on a task, and
  each is bounded by the harness refusing a pick it cannot carry out.

The open question for the next review is the same as for this one: a student
points the harness at a repository they did not write. Envelope approval and tool
servers change what runs without a person watching, which is the part to read
first.
