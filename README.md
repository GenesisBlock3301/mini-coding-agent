# Mini Coding Agent

A small coding agent built from scratch (Python + DeepSeek API, no agent
framework) to understand how an agent chooses actions, where it fails, and
whether a change actually improves it.

Plan and milestones: [`mini-coding-agent-plan.md`](mini-coding-agent-plan.md)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in DEEPSEEK_APIKEY
```

## Usage

```bash
python -m agent.cli "what's in the awkward folder?" --tools
python -m agent.cli --resume=<run_id>     # continue a crashed run
python -m agent.chat                      # interactive session (interlude)
```

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
| `playground/` | The default writable workspace; `--workspace=` overrides it per run |

`.env` and anything real live outside `playground/`.

## Docs

`docs/mini-coding-agent-plan.md` is the course plan. One doc per session, each
with the decisions it locked, the build order, a failure table, the invariants
carried forward and a self-test — written before the code and updated with what
actually happened. Sessions 10-15 are written and not yet built.

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
- [ ] 11. 10 eval tasks + runner
- [ ] 12. 20 tasks, baseline comparison report
- [ ] 13. FastAPI, isolated workspace per run
- [ ] 14. Persistent memory: resume by default + a memory file outside the workspace
- [ ] 15. Docker, docs, demo

## After Session 15

[`docs/post-session-15-roadmap.md`](docs/post-session-15-roadmap.md) sketches
where this goes once the course is finished: a local-first `mini-agent` you
install and run from any project directory, with file creation, patching,
and controlled command execution. **Everything in it is PLANNED — NOT
IMPLEMENTED**, and nothing starts until all 15 sessions are done. The
15-session plan remains the only active work.

## Session notes

Kept in `notes/` — one entry per session: goal, what I implemented, the failure
I triggered, expected vs observed, what I learned, open questions, next step.
