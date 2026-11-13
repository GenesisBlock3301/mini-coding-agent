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

Run `python -m agent.cli` with no task for flags, limits and exit codes.

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
| `agent/chat.py` | Interactive session - a REPL over `run_loop`; not a plan milestone, see `docs/interlude-repl.md` |
| `agent/tools.py` | Tool schemas and implementations |
| `agent/policy.py` | Workspace boundary and limits, enforced in code |
| `agent/tracing.py` | JSONL event traces, secrets excluded |
| `evals/` | Task fixtures, runner, reports |
| `playground/` | The agent's only writable workspace |

`.env` and anything real live outside `playground/`.

## Progress

- [x] 1. One DeepSeek request: response, duration, usage; legible failures
- [x] 2. `list_files` + workspace boundary
- [x] 3. `read_file` with size and output limits
- [x] 4. Tool call -> execute -> tool result -> model
- [x] 5. Multiple tool calls per response; stopping rules
- [ ] 6. `replace_text`, exactly one match
- [ ] 7. Read -> edit against a fixture project
- [ ] 8. Deliberate failure injection
- [ ] 9. Repetition and context growth
- [~] 10. JSONL traces (`journal.py` covers runs; `tracing.py` still a stub)
- [ ] 11. 10 eval tasks + runner
- [ ] 12. 20 tasks, baseline comparison report
- [ ] 13. FastAPI, isolated workspace per run
- [ ] 14. Persistent memory: resume by default + a memory file outside the workspace
- [ ] 15. Docker, docs, demo

## Session notes

Kept in `notes/` — one entry per session: goal, what I implemented, the failure
I triggered, expected vs observed, what I learned, open questions, next step.
