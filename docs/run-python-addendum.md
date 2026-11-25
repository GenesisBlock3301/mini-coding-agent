# Addendum — `run_python` (added after Session 15)

The 15 sessions end with file tools only. `run_python` was added afterwards and
has no session of its own. This is the record of what it is, why it is risky and
what is tested.

## What it does

`run_python(path)` runs one workspace-relative `.py` file with the current
interpreter (`agent/tools/execution.py`).

| Property | Value |
|---|---|
| Shell | none: `[sys.executable, script]`, no arguments |
| Path rules | same resolver as the file tools; outside paths and symlinks refused; only `.py` |
| Time | 30 s, then the whole process group is killed (`execution_timeout`) |
| Output | stdout and stderr merged, truncated at 8,000 bytes |
| Environment | only `PATH`, `TMPDIR`/`TEMP`/`TMP`, `SYSTEMROOT` plus `PYTHONPATH=<workspace>`; API keys are not passed |
| Working directory | the workspace |
| Result | `ok` only for exit code 0; otherwise `execution_failed` with the output |

## The decision it forces

File tools are contained by `policy.py`. A running script is not. It runs as
your user and can read, write and reach the network anywhere you can. The
boundary stops the *tool*, not the *code*.

So a prompt injection that used to be able only to ask for a file read can now
ask for "write a script, then run it". The defences are layers, none complete:
the tool description says to run only code the user asked for; the model's own
judgement; the trust prompt before a folder is used; and your choice to use
disposable projects.

## Recovery

`run_python` is in `WRITES`, so the journal records intent before it runs. If
the process dies mid-run, resume does not repeat it. Its side effects cannot be
inferred from the script's hash, so replay returns `UNKNOWN` and tells the
model: inspect the workspace first (`agent/replay.py`).

## Evidence

`tests/test_run_python.py` (11 tests): exit code and output, non-zero exit,
non-`.py` and missing files, `../` and symlink refusal, truncation, timeout
kill, keys not passed to the child, workspace cwd and imports, argument
validation, and membership in `WRITES`.

A live check (2026-11-25, `deepseek-chat`): a file telling the agent to write and
run `canary.py` was ignored in 3 of 3 runs, and the agent reported the
injection. Three subtler variants followed (a README "setup" step, a code comment
addressed to "automated assistants", and a note claiming to be from the project
owner), each against an existing harmless `canary.py`, three runs each: the
script ran in 0 of 9. In total 0 of 12 injected runs executed the canary. One
model, small samples, and a canary that only writes a file; the code would not
have stopped a model that obeyed.

## `run_tests` (added 2026-11-25)

`run_tests()` takes no arguments and runs `python -m unittest discover -s tests`
(or `-s .` when there is no `tests/`) through the same approval, sandbox, 30 s
timeout and environment as `run_python`. Zero tests found is `no_tests_found`,
never a pass, and long output keeps its end (the summary). Replay treats it like
`run_python`: an interrupted run is not repeated.

**Verification rules**
- The system prompt says the agent MUST run it after any change.
- Code backs that up: every successful edit result carries "Next: call
  run_tests to verify...", the loop tracks `unverified_changes` (cleared only by
  a passing `run_tests` after the last change), and the CLI and chat print a
  WARNING when a run ends with it set.
- Not forced: a model can still skip it. The warning makes that visible
  instead of preventing it. Only unittest is supported (no pytest).
- Live check: on a copy of `practice/`, the agent fixed `subtract`, ran the full
  suite (7 tests, OK) and reported it.

## Folder trust

Both `mini-chat` and `mini-agent` ask before using a new folder (`agent/trust.py`),
before anything is spent.

## Approval and sandbox (added 2026-11-25)

- **Approval** (`agent/approval.py`): each `run_python` call asks
  `[y]es / [a]lways this session / [N]o`. A refusal returns `denied_by_user` to
  the model. With no terminal the call returns `approval_required`.
  `ALLOW_RUN_PYTHON=1` skips the question. `tests/test_approval.py`, 6 tests.
- **Sandbox, macOS only** (`sandbox_prefix` in `agent/tools/execution.py`,
  `sandbox-exec`): network denied; writes only in the workspace and temp dirs;
  reads denied for the agent's state, its config, `~/.ssh`, `~/.aws`,
  `~/.gnupg`. Four tests prove each rule. `MINI_AGENT_NO_SANDBOX=1` turns it
  off, and the result then ends with "(ran without a sandbox)". `sandbox-exec`
  is deprecated by Apple but still ships. Linux and Windows run unconfined.
- Reads outside those folders are still allowed, so a script can read other
  files you own. Treat the sandbox as a second lock, not a vault.

## Not done

No Linux sandbox, no `run_tests`, no per-command allow rules. Those are the
roadmap's command-execution phase.

## Model coverage

The injection checks ran on DeepSeek only. A second run set to
`deepseek-reasoner` was labelled `deepseek-flash` by the server, the same label
as the `deepseek-chat` runs, so it is not counted as a different model. No
other provider key or local Ollama server was available.
