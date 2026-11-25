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
injection. One model, three runs; the code would not have stopped it.

## Not done

No sandbox, no network denial, no per-run approval, no `run_tests`. Those are
the roadmap's command-execution phase.
