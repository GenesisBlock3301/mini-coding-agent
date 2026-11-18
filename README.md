# Mini Coding Agent

A small coding agent built from scratch (Python + DeepSeek API, no agent
framework) to understand how an agent chooses actions, where it fails, and
whether a change actually improves it.

It runs in the terminal, like Codex or Claude Code. Model provider requests
are outbound; the application does not run an HTTP API.

Plan and milestones: [`mini-coding-agent-plan.md`](docs/mini-coding-agent-plan.md)

## Quickstart

Prerequisites: Python 3.12, Git, and a model provider for live tasks. The
offline demo and tests need no key or network after dependencies are installed.

```bash
git clone https://github.com/GenesisBlock3301/mini-coding-agent.git
cd mini-coding-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/make_fixtures.py
python scripts/demo.py
```

For live use, run `cp .env.example .env` and set `DEEPSEEK_APIKEY`, or
configure `PROVIDER=ollama` and start Ollama. The demo's first act can use the
configured provider with `python scripts/demo.py --live`. Start with the
generated `playground/` or a disposable fixture before pointing the agent at
real code. Inspect the edited files and verifier output independently of the
agent's final explanation.

## Usage

```bash
python -m agent.cli "what's in the awkward folder?" --tools
python -m agent.cli --resume=<run_id>     # continue a crashed run
python -m agent.chat                      # interactive session (interlude)
python -m agent.chat --continue           # latest session in this workspace
python -m agent.chat --workspace=/path/to/project
```

With `--tools`, the agent can create a new UTF-8 text file using `write_file`.
It cannot overwrite an existing path; use `replace_text` to edit an existing
file. File creation is limited to 64 KB and an existing parent directory.

Against a fixture project — a fresh workspace per run, graded by a checker the
agent cannot see:

```bash
W=$(python scripts/fixture.py build tail_window)
python -m agent.cli --tools --workspace=$W --max-steps=8 \
    "$(python scripts/fixture.py task tail_window)"
python scripts/fixture.py verify tail_window --workspace=$W
```

Run `python -m agent.cli` with no task for flags, limits and exit codes.

Reading a run after it is over:

```bash
python -m agent.tracing <run_id>              # a timeline, tool output bounded
python -m agent.tracing <run_id> --json       # one row, for the eval suite
python -m agent.tracing <run_id> --absences   # what that generation cannot say
python -m agent.tracing --list 20             # the most recent runs
python scripts/prune_runs.py                  # what retention would do (dry)
```

`tracing.py` writes nothing: a trace is a reading of the journal, so the two
can never disagree. `--full` prints tool output verbatim — that is the
workspace's own content, so treat it the way you would treat the workspace.

Failure injection — no key and no network, except where noted:

```bash
python scripts/crash_demo.py --edit     # crash either side of a write, then resume
python scripts/crash_demo.py --replay   # the same edit twice: atomic != idempotent
python scripts/fault_matrix.py          # every case x every crash point, as rows
CRASH_AFTER_TOOL=2 python -m agent.cli --tools "..."   # failpoint in the real path
```

Retries are read from `.env` (`MAX_ATTEMPTS`, `RETRY_BASE_DELAY_S`,
`RETRY_MAX_DELAY_S`, `RETRY_JITTER`, `RETRY_AFTER_CAP_S`) and printed in the
run banner. A request that may already have reached the server is never sent
again once a tool has written.

Going in circles, and growing too big to send:

```bash
python -m agent.cli --tools --no-repeat-stop "..."   # count repeats, never stop
python scripts/repeat_audit.py                       # would the rule have
                                                     # stopped past runs?
```

The same call is stopped after 3 failures or 5 successes
(`MAX_REPEATED_FAILURES`, `MAX_REPEATED_CALLS`), counted per tool with its
`path` canonicalised, cumulatively across the run and across a `--resume`.
Two thresholds because a repeated failure is a misunderstanding and a
repeated success is usually verification - the audit script is how those
numbers were chosen, not taste. A run whose history would not fit
`CONTEXT_WINDOW` stops before the request rather than after the provider
rejects it.

### Providers

One adapter, two providers — both speak the OpenAI chat-completions shape, so
`PROVIDER` only selects defaults (base URL, model, whether a key is needed,
timeout, pricing).

```bash
PROVIDER=deepseek python -m agent.cli "..." --tools   # hosted, needs a key
PROVIDER=ollama   python -m agent.chat               # local, free, no key
```

`PROVIDER` unset means deepseek, and every `DEEPSEEK_*` variable is unchanged.

**Ollama serves 4096 tokens of context by default**, whatever the model
supports, and silently drops the oldest messages on overflow — which corrupts
an agent's tool-call history rather than failing loudly. Start the server with
`OLLAMA_CONTEXT_LENGTH=32768 ollama serve`; both entry points check for this
and warn.

## Layout

| Path | Responsibility |
| --- | --- |
| `agent/cli.py` | Accept the task, print result + duration + usage |
| `agent/model.py` | Model adapter — the only provider-aware module (DeepSeek or local Ollama) |
| `agent/loop.py` | Execution loop, history, tool-call dispatch |
| `agent/budget.py` | Limits, running totals, estimated spend — every way a run stops |
| `agent/journal.py` | Append-only run journal; crash recovery via `--resume` |
| `agent/retry.py` | Retry policy: which failures get another request, and when |
| `agent/repetition.py` | What counts as the same call, and how often it may be made |
| `agent/context.py` | How big the conversation is, and how wrong that number is |
| `agent/replay.py` | What a resumed run may re-run — an interrupted edit is not replayed blind |
| `agent/chat.py` | Interactive session - a REPL over `run_loop`; not a plan milestone, see `docs/interlude-repl.md` |
| `agent/tools.py` | Tool schemas and implementations |
| `agent/policy.py` | Workspace boundary and limits, enforced in code |
| `agent/tracing.py` | Reads a journal back: a timeline, or one row per run |
| `evals/` | Eval runner and reports (Session 11) |
| `fixtures/` | Fixture projects: a broken `workspace/` the agent sees, a `verify/` it does not |
| `scripts/fixture.py` | Build, reset and verify a fixture workspace |
| `scripts/crash_demo.py` | Kill a run at a chosen point and show what survives |
| `scripts/fault_matrix.py` | Every fixture case × every crash point, graded by the verifier |
| `scripts/repeat_audit.py` | Replays past journals through the repetition rule |
| `scripts/prune_runs.py` | Retention: never the newest run, never an unfinished one |
| `.runtime/journals/` | Durable execution journals used by tracing and resume |
| `.runtime/eval-workspaces/` | Disposable per-evaluation fixture copies |
| `.runtime/migrations/` | Checksum manifests from explicit storage migrations |
| `playground/` | The default writable workspace; `--workspace=` overrides it per run |

`.env` and anything real live outside `playground/`.

`RUNS_DIR`, `EVAL_WORKSPACES_DIR`, `WORKSPACE_DIR`, and
`MINI_AGENT_MEMORY_DIR` can move journals, disposable evaluation workspaces,
the active workspace, and human-maintained notes independently. A resumed run
uses the workspace recorded in its journal. An explicit conflicting
`--workspace` is refused before a model request; a missing recorded workspace
is also refused. `--continue` selects the most recent journal started in the
selected workspace, including a finished session. Use `--resume=<id>` when
the exact session matters.

## Container

The image contains source, fixture templates, tests and the Session 12 report.
It runs as UID 10001 by default. It contains no `.env`, journals, memory or
host workspace. Build from a fresh checkout:

```bash
docker build --no-cache -t mini-coding-agent .
docker run --rm --entrypoint python mini-coding-agent scripts/demo.py
docker run --rm --entrypoint sh mini-coding-agent -c \
  'python scripts/make_fixtures.py && touch playground/.gitkeep && env -u WORKSPACE_DIR -u RUNS_DIR \
   -u EVAL_WORKSPACES_DIR -u MINI_AGENT_MEMORY_DIR python -m pytest -q'
```

The test command clears deployment paths because fixture tests exercise the
project's default generated `playground/`. To keep state across containers,
create three host directories and mount them. On Linux, use your host UID and
GID for writable bind mounts:

```bash
mkdir -p .local-state/journals .local-state/workspaces .local-state/memory
docker run --rm -it --user "$(id -u):$(id -g)" --env-file .env \
  --mount type=bind,src="$PWD/.local-state",dst=/state \
  --mount type=bind,src="$PWD/playground",dst=/workspace \
  mini-coding-agent --tools "list the files in this workspace"
```

For a read-only memory container, add
`--mount type=bind,src="$PWD/.local-state/memory",dst=/state/memory,readonly`.
The interactive `/approve-memory` command needs a writable memory mount;
otherwise edit notes on the host. Notes remain outside the workspace either
way. To resume after replacing the container,
run the same mount command with `--resume=<run_id>`; both the journal and
workspace must still be present. For host Ollama, set
`OLLAMA_BASE_URL=http://host.docker.internal:11434/v1` in the container and
start Ollama with a context length large enough for the run. Send hosted keys
at runtime through an environment file; never add `.env` to the image.

## Boundary and limits

The agent's tools can read and edit only under one selected directory. It has
no command execution, browsing, package installation, multi-agent work,
compaction or retrieval. Path checks are not a sandbox, and Docker alone is
not a complete security boundary: a hostile process sharing the workspace can
defeat path checks. The container bounds what a mistake can touch; it does not
make untrusted tasks safe by itself. The verifier checks fixture outcomes, but
a passing fixture does not prove a change to a real project is correct.

Generated execution data lives under the gitignored `.runtime/` directory:

```text
.runtime/
├── journals/          # Execution history and crash recovery
├── eval-workspaces/   # Disposable fixture copies
└── migrations/        # Migration checksum manifests
```

These directories are created when needed. Fixture templates remain in
`fixtures/`; evaluation result tables remain in `evals/results/`. Journals are
recovery data: deleting them loses history and the ability to resume those
runs. Evaluation workspaces can be discarded once their changes no longer
need inspection.

For older checkouts with a `runs/` directory, migrate explicitly:

```bash
python scripts/migrate_runtime.py
```

The migration checks for conflicts before moving data, preserves journal
bytes, verifies moved data with checksums, and removes only the emptied old
directories. Rerunning it after completion is a no-op. Existing journal and
result records keep their original bytes; tracing, the eval result reader,
and workspace entry points resolve their old artifact paths to the new
locations. Historical session notes may still refer to `runs/`.

## Docs

`docs/mini-coding-agent-plan.md` is the course plan. One doc per session, each
with the decisions it locked, the build order, a failure table, the invariants
carried forward and a self-test — written before the code and updated with what
actually happened. Session 13 is implemented; Sessions 12, 14 and 15 remain
open in the progress list below.

## Progress

- [x] 1. One DeepSeek request: response, duration, usage; legible failures
- [x] 2. `list_files` + workspace boundary
- [x] 3. `read_file` with size and output limits
- [x] 4. Tool call -> execute -> tool result -> model
- [x] 5. Multiple tool calls per response; stopping rules
- [x] 6. `replace_text`, exactly one match
- [x] 7. Read -> edit against a fixture project (harness, three cases, 15/15 on
      the Phase 3 tally; the Phase 4 one-variable experiment returned a null
      result, which is recorded rather than retried)
- [x] 8. Deliberate failure injection (retries, and a resumed run that will not
      replay an interrupted edit blind; 12-row fault matrix)
- [x] 9. Repetition and context growth (3-failure / 5-success rule, cumulative
      and resume-proof; a context check that refuses a step it cannot finish.
      Audited against all 91 existing journals: it stops none of them, and the
      offence-grade threshold the plan implies would have stopped the Session 8
      live run)
- [x] 10. Run traces (`tracing.py` reads journals rather than writing a second
      stream; latency, model id and the tool menu added to the journal first;
      tool output bounded by default. A survey of all 132 journals found six
      schema generations where the doc assumed three)
- [x] 11. 10 eval tasks + runner (fresh workspaces, independent checkers,
      JSONL rows and summaries, deadlines and spend ceiling; scripted 10/10,
      live 10/10 then 30/30. These small fixtures remain a smoke test; see
      [`evals/session-11-report.md`](evals/session-11-report.md))
- [x] 12. 20 tasks, baseline comparison report (registered change did not ship)
- [x] 13. Safe creation of new files in the selected workspace
- [ ] 14. Workspace-aware resume and persistent memory (week-long poisoning audit remains)
- [x] 15. Docker, docs, and a local-project demo (fresh container verified)

## Evaluation suite

```bash
python -m evals.run --mode golden --repeats 1   # offline harness check
python -m evals.run --mode live --repeats 3    # configured provider; needs prices
python -m evals.run --summary evals/results/<suite>.jsonl
```

The runner defaults to three repetitions and a $1 estimated suite ceiling.
Hosted runs require configured prices or `--prices HIT MISS OUTPUT` in USD
per million tokens. `--cases tail_window` selects cases; `--timeout` and
`--check-timeout` bound worker and checker execution. Every run gets a fresh
workspace. Results are fsynced after each case; fatal provider errors or
uncertain spend stop the suite while preserving completed rows. Existing
result files are never overwritten. Negative controls (`noop`, `wrong_file`,
`double_edit`, `half_edit`) deliberately demonstrate failing grades.

## After Session 15

[`docs/post-session-15-roadmap.md`](docs/post-session-15-roadmap.md) sketches
where this goes once the course is finished: a local-first `mini-agent` you
install and run from any project directory, with patching,
and controlled command execution. **Everything in it is PLANNED — NOT
IMPLEMENTED**, and nothing starts until all 15 sessions are done. The
15-session plan remains the only active work.

## Session notes

Kept in `notes/` — one entry per session: goal, what I implemented, the failure
I triggered, expected vs observed, what I learned, open questions, next step.

### Sessions 13–15

Session 13 added create-only `write_file` for UTF-8 text files in the selected
workspace. It refuses existing files and missing parent directories, caps
content at 64 KB, and records interrupted creates for safe replay. See the
[Session 13 plan](docs/session-13.md). Session 14 verifies workspace-aware
resume and memory; Session 15 validates the local workflow in a fresh
environment. Workspace path checks and Docker are not security sandboxes.

Chat reads optional human-maintained notes from `MINI_AGENT_MEMORY_DIR` (by
default `.runtime/memory`), keyed by workspace. The path and hash appear at
startup; edit or delete the text file by hand. `python -m agent.chat
--continue` selects the latest journal for the current workspace.
To save a fact, use `/remember <exact user statement>` followed by
`/approve-memory`; `/cancel-memory` discards the proposal. Only an exact
single-line message previously supplied by the user is accepted. The saved
note is read at the start of a new session.

A container image and offline demo are available through `Dockerfile`
and `python scripts/demo.py`. The offline demo has run in the image as a
non-root user. The container suite passed 656 tests with two Git history checks
skipped because the image contains no Git checkout. A controlled interrupted
edit resumed in a replacement container with mounted workspace and journal;
the already-applied edit was detected and not repeated. Run
`python scripts/memory_eval.py --memory-file NOTES --output RESULTS` for the
offline paired memory check; it verifies plumbing, not live model quality.

### Session 12: paired comparisons

`evals.compare` evaluates one registered prompt sentence against the existing
CLI prompt. It uses 20 tasks × 3 repetitions × 2 arms, interleaves A/B on fresh
workspaces, runs the four reserved cases last, and prints progress without
intermediate scores. It refuses uncommitted or changed live protocols, changed
fixtures/source, and an estimate above the configured suite ceiling. Known
usage survives provider errors; fatal errors or uncertain billing stop the
suite and preserve completed rows. Existing CLI defaults remain unchanged.

```bash
# Freeze decisions, fixture hashes, configured budgets and historical usage.
python -m evals.register --baseline evals/results/session-11-live-three.jsonl \
  --output evals/results/session-12-protocol-v2.json
# Commit that protocol before live execution. Run offline first.
python -m evals.compare --protocol evals/results/session-12-protocol-v2.json \
  --mode golden --output /tmp/session-12-offline.jsonl
python -m evals.compare --protocol evals/results/session-12-protocol-v2.json \
  --mode live --output evals/results/session-12-live-complete.jsonl
# Regenerates every comparison statistic and decision from durable rows alone.
python -m evals.compare --report evals/results/session-12-live-complete.jsonl
```

The registered rule requires at least 10 percentage points of success gain,
McNemar p≤0.05, no increase in unexpected-change runs, token and latency ratios
at most 1.25, and no reversal on the reserved cases. Partial and scripted
results cannot ship. Repeated observations are correlated; the report states
this limitation and includes a conservative interval based on case means.
See [Session 12 results](evals/session-12-report.md) for evidence and limitations.
