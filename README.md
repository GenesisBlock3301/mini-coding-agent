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

_TODO (Session 1)_

## Layout

| Path | Responsibility |
| --- | --- |
| `agent/cli.py` | Accept the task, print result + duration + usage |
| `agent/model.py` | DeepSeek adapter — the only provider-aware module |
| `agent/loop.py` | Execution loop, history, tool-call dispatch |
| `agent/tools.py` | Tool schemas and implementations |
| `agent/policy.py` | Workspace boundary and limits, enforced in code |
| `agent/tracing.py` | JSONL event traces, secrets excluded |
| `evals/` | Task fixtures, runner, reports |
| `playground/` | The agent's only writable workspace |

`.env` and anything real live outside `playground/`.

## Progress

- [ ] 1. One DeepSeek request: response, duration, usage; legible failures
- [ ] 2. `list_files` + workspace boundary
- [ ] 3. `read_file` with size and output limits
- [ ] 4. Tool call -> execute -> tool result -> model
- [ ] 5. Multiple tool calls per response; stopping rules
- [ ] 6. `replace_text`, exactly one match
- [ ] 7. Read -> edit against a fixture project
- [ ] 8. Deliberate failure injection
- [ ] 9. Repetition and context growth
- [ ] 10. JSONL traces
- [ ] 11. 10 eval tasks + runner
- [ ] 12. 20 tasks, baseline comparison report
- [ ] 13. FastAPI, isolated workspace per run
- [ ] 14. Docker, docs, demo

## Session notes

Kept in `notes/` — one entry per session: goal, what I implemented, the failure
I triggered, expected vs observed, what I learned, open questions, next step.
