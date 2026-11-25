# Video Notes — Claims That Match the Code

Use these wordings when presenting "Building an AI Coding Agent from Scratch".
Each one was checked against the repository and the session docs.

## Topic 06 — atomic writes

Two tools, two guarantees. Do not call them the same thing.

- `replace_text` writes a temp file, `fsync`s it and publishes with
  `os.replace`. Say: **"write-then-rename"**. A reader sees the old or the new
  file, never half of one.
- `write_file` publishes with `os.link`, which fails if the target exists.
  Say: **"atomic, no-clobber creation"**. It never overwrites.
- Not claimed: ordering across two files, or filesystems that reorder renames.
- A hard kill can leave a hidden `.agent-edit-*` staging file. The target is
  never partial.

## Topic 09 — retry and "idempotency"

- API retries use exponential backoff with full jitter, bounded by attempts and
  the run clock (`agent/retry.py`).
- The project has **no idempotency key**. Say: **"a read is safe to resend; a
  write is not, so after a write the agent does not blindly retry."**
- Edit safety comes from the exactly-one-match rule and from resume comparing
  the journal with disk state, not from idempotency.

## Topic 14 — memory

- Memory is a Markdown file **outside** the workspace, read once at session
  start. Its SHA-256 appears in the banner.
- The agent cannot write it directly. `/remember` proposes and
  `/approve-memory` is a separate human step.
- Say what is measured: 20 cases x 2 arms, interleaved. Do **not** claim
  long-term safety. Step 8 (does a wrong fact written in run 3 still steer run
  40?) is not done.

## Topic 05 — safety and `run_python`

- File tools are contained by `policy.py`. **`run_python` is not.** It runs one
  workspace `.py` file with the current interpreter, no shell, a 30 s timeout
  and 8,000 bytes of output, with only `PATH` and temp variables passed (no API
  keys). The script still runs as your user and can read, write and use the
  network outside the workspace.
- Since 2026-11-25 every run asks for approval, and on macOS the script runs in
  `sandbox-exec` (no network, writes only in the workspace). Say "macOS only,
  defence in depth" and do not call it a sandbox on Linux.
- Do **not** say "the agent has no command execution". Say: **"no shell, but it
  can run a Python file, so only run it on code you trust."**
- Prompt injection matters more because of this: a file could tell the agent to
  write a script and run it. The boundary stops file tools, not the script.
- Injection check (2026-11-25, `deepseek-chat`): `practice/notes/vendor.txt` asks
  the agent to write and run `canary.py`. In 3 of 3 runs it did not, and it
  told the user the file contained an injection. That is a small sample from one
  model, not a guarantee: the code does not stop a model that obeys. Three
  subtler variants (README setup step, code comment, fake owner note), 9 runs,
  also never ran the canary.
- `mini-chat` asks "Do you trust the files in this folder?" once per folder
  (`agent/trust.py`). Home and `/` are refused via `cd` and never remembered.
- The session docs and `post-session-15-roadmap.md` predate `run_python` and
  still say "no command execution". The README and code are current.

## Topics 12–13 — where the evals live

The `agent-only-app` branch contains only `agent/`, `tests/` and `docs/`.
`evals/`, `fixtures/` and the runner scripts live on `main`. For the evaluation
chapter, show them from `main`, or say clearly that the packaged agent
excludes them.

## Topic 13 — report the null result as a result

Session 12's measured gain was 1.7 points, McNemar p=1.0. It failed the bar
committed in advance. Present it as: **"I decided the rule first, and the
improvement did not clear it."**

## Build order vs. video order

Cost budgets (topic 07) were built in Session 5 and the REPL (topic 08) later.
The video order is a teaching order, not the commit order. Say so once.
