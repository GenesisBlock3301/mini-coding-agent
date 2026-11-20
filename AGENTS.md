# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.12+ terminal coding agent packaged with setuptools. Runtime code is in `agent/`: `cli.py` and `chat.py` provide the command line entry points; `loop.py`, the `tools/` package, and `policy.py` implement the agent loop, tools, and workspace boundaries; `journal.py`, `storage.py`, and `memory.py` handle durable state. Configuration and dependencies are defined in `pyproject.toml`. Design notes live in `docs/`.

## Build, Test, and Development Commands

- `python3.12 -m venv .venv && . .venv/bin/activate && pip install -e .` creates a local editable install.
- `python -m agent.cli --help` displays one-shot CLI usage.
- `python -m agent.chat --help` displays interactive CLI usage.
- `python -m agent.tracing <run_id>` inspects a saved run journal.

Run commands from the project directory to use it as the default workspace. Set model credentials in `~/.config/mini-coding-agent/.env`; never commit credentials.

## Coding Style & Naming Conventions

Follow the existing Python style: four-space indentation, `snake_case` for functions and variables, and `PascalCase` for classes. Keep modules focused on their existing responsibility and preserve clear type annotations. No formatter or linter is configured in `pyproject.toml`; keep changes consistent with neighboring code.

## Testing Guidelines

No automated tests or coverage requirement are currently shipped. For a change, run the relevant CLI help or tracing command and exercise the affected behavior on a disposable project. Do not use a sensitive or production workspace for manual checks.

## Commit & Pull Request Guidelines

Recent commits use short, imperative summaries, often with a scope (for example, “Register Session 12…”). Keep commit subjects concise and describe the change. Pull requests should explain the behavior change, list validation performed, and link related issues when applicable. Include sample terminal output for CLI-facing changes.

## Security & Configuration

Keep `.env` files, API keys, journals, and memory outside the target workspace. Workspace restrictions are part of the agent’s safety boundary; changes to path handling should preserve containment and symlink checks in `agent/policy.py`.


# Efficient Engineering Rules

## 1. Repository Exploration
- Never scan the entire repository without a reason.
- Start with the relevant entry point.
- Inspect dependencies only when necessary.
- Reuse information already discovered.

## 2. Implementation
- Make the smallest correct change.
- Avoid unrelated refactoring.
- Do not rewrite entire files unnecessarily.
- Do not introduce abstractions without justification.

## 3. Tool Usage
- Prefer targeted grep/search.
- Avoid dumping entire files into context.
- Keep terminal outputs concise.
- Do not repeat successful commands.

## 4. Testing
- Run targeted tests first.
- Run broader tests when the change warrants it.
- Never rerun unchanged tests without a reason.
- Report test results and failures accurately.

## 5. Agent Behaviour
- Avoid unnecessary subagents.
- Do not create lengthy plans for simple tasks.
- Ask before expanding the scope.
- Do not repeatedly summarize the same context.

## 6. Final Response
Report only:
- Files changed
- Implementation summary
- Tests executed
- Remaining risks
