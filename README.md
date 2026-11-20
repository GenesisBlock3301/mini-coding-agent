# Mini Coding Agent

A small terminal coding agent that reads and edits files inside a selected project directory. It supports DeepSeek and a local Ollama server, records every run in a journal, and can resume an interrupted run.

## Install

Requires Python 3.12 or newer. From a checkout of this branch:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install .
mini-agent "Explain the code in src/app.py"
```

`pipx install .` is another option if you use pipx. The installable package contains only `agent/`. The repository keeps `docs/`, while fixture projects, evaluation outputs, and the old test harness are removed from this branch.

The [design notes](docs/mini-coding-agent-plan.md) document how the agent was built; some describe historical test files that are no longer shipped.

## Configure a model

Keep credentials outside the project the agent will edit:

```bash
mkdir -p ~/.config/mini-coding-agent
cp .env.example ~/.config/mini-coding-agent/.env
```

Edit that file to set `DEEPSEEK_APIKEY`. For a local model, set `PROVIDER=ollama`, start Ollama with `OLLAMA_CONTEXT_LENGTH=32768`, and choose `OLLAMA_MODEL`. You can instead set these variables in your shell or point `MINI_AGENT_CONFIG_FILE` at another file. Environment variables take precedence over the config file.

If you install directly from a package without a source checkout, create the same config file with an editor and add `PROVIDER=deepseek` and your `DEEPSEEK_APIKEY`. The command does not need to run from this repository.

A fresh run uses the current directory as its workspace. Run the command from the project you want to edit, or choose another directory with `--workspace=DIR` or `WORKSPACE_DIR`. The agent refuses a workspace containing its state or config, so it cannot edit its own journal or credentials through file tools.

## Use

```bash
mini-agent "Fix the bug in src/app.py"       # edits the current directory
mini-agent --workspace=/path/to/project "Fix the bug in src/app.py"
mini-agent --resume=<run_id>                 # uses the workspace recorded in the journal
mini-agent-chat                              # interactive conversation in the current directory
mini-agent-chat --continue                   # latest session for this workspace
mini-agent --help
```

The agent offers `list_files`, `list_folders`, `read_file`, `replace_text`, create-only `write_file`, and `run_python`. `run_python` accepts one workspace-relative `.py` file, runs it with the current Python interpreter for at most 30 seconds, and returns at most 8,000 bytes of output. Python code runs with the agent user's operating-system permissions and can access files or network resources beyond the workspace; use it only with trusted code. An interrupted execution is never automatically repeated. The agent prints the selected workspace, model, budget, journal ID, tool results, and changed paths. Check files yourself after a run; the final answer is the model's account of its work.

By default journals and human-approved memory live under `~/.local/state/mini-coding-agent/`, outside the installed package and selected workspace. `MINI_AGENT_STATE_DIR` moves both; `RUNS_DIR` and `MINI_AGENT_MEMORY_DIR` can override them separately. `python -m agent.tracing <run_id>` shows a bounded timeline. In chat, `/remember <exact earlier user statement>` proposes a note and `/approve-memory` saves it for the next session. The startup banner prints the memory path and hash.

## Docker

The image contains the installed agent, not example projects or test data. Build it locally:

```bash
docker build -t mini-coding-agent .
mkdir -p "$HOME/.local/state/mini-coding-agent"
docker run --rm -it --user "$(id -u):$(id -g)" \
  --env-file "$HOME/.config/mini-coding-agent/.env" \
  --mount type=bind,src="/absolute/path/to/project",dst=/workspace \
  --mount type=bind,src="$HOME/.local/state/mini-coding-agent",dst=/state \
  mini-coding-agent "Explain src/app.py"
```

Replace `/absolute/path/to/project` with an existing directory. Reuse the same mounts and pass `--resume=<run_id>` after replacing a container. For host Ollama, set `OLLAMA_BASE_URL=http://host.docker.internal:11434/v1` in the runtime environment. If bind-mount permissions differ on your system, use a UID and GID with write access to the project and state directories.

## Limits

File tools are restricted to the selected workspace. The Python execution tool runs trusted scripts without a shell, but it is not a security sandbox and does not restrict a script's filesystem or network access. The agent does not install packages or browse. Another process that can alter the workspace can also defeat path checks. Use disposable projects for unfamiliar code and inspect changes after execution.
