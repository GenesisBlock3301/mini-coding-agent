# Series Script — Building an AI Coding Agent from Scratch

Eight videos, 20–30 minutes each, plus an optional 3-minute trailer.
Claim wordings for each topic are in [`docs/video-notes.md`](docs/video-notes.md).
Source material for each session is in `docs/session-NN.md`.

## Topic map (15 topics → 8 videos)

| Video | Topics | Sessions |
|---|---|---|
| 1. What is an agent, and the first model call | 01 What is an agent, 02 Connecting an LLM | 1 |
| 2. Tools and the agent loop | 03 Building tools, 04 The agent loop | 2–5 |
| 3. Keeping it safe | 05 Agent safety | 2–3, 14 |
| 4. How AI edits code | 06 How AI edits code | 6, 13 |
| 5. Budgets and the interactive CLI | 07 Cost control, 08 Interactive CLI | 5, REPL interlude |
| 6. When agents fail | 09 Failures, 10 Loops and context, 11 Observability | 8–10 |
| 7. Does it actually work? | 12 Evaluation, 13 Experiments, 14 Memory | 11, 12, 14 |
| 8. Wrap-up and what's next | 15 Limits, demo, roadmap | 15 |

Docker was removed from the repo. Do not cover it.

## Every video: the same shape

1. Recap and the problem (1–2 min)
2. The idea on one slide or diagram (3–4 min)
3. Code walkthrough, typing only the 5–10 lines that matter (8–12 min)
4. Break it on purpose, then show the fix (3–5 min)
5. One-line takeaway and next-episode teaser (1 min)

Git tag per video (`ep01` … `ep08`) so viewers can check out your exact state.
Use a disposable fixture project, never a real one. Never show the `.env`.

---

## Trailer (3 min, optional)

- **Hook:** "This agent fixed a bug in a project it had never seen. Here is how it works."
- **Show:** a fixture task end to end, one path-escape refusal, one crash and resume.
- **End:** series list and what viewers need (Python 3.12, an API key or local Ollama).

---

## Video 1 — What Is an AI Agent, and the First Model Call

**Promise:** you will understand the difference between a chatbot, an agent and a coding agent, and make your first model request.

**Outline**
1. Chatbot vs agent vs coding agent. Draw the plan's loop (`model → tool call → result → model`).
2. What we are building, and what we are not (no shell, no browsing, no multi-agent). It can run code in exactly one way: `run_python`, one trusted `.py` file with no shell (see Video 3).
3. A chat model is a function: messages in, one message out. It keeps no state.
4. Tokens, `finish_reason`, usage and duration. Show `agent/model.py`'s `ModelResponse`.
5. Credentials live outside the workspace (`~/.config/mini-coding-agent/.env`).

**Demo:** send one prompt, read text, tokens and duration. Send it twice and compare tokens.
**Break it:** wrong API key → one readable error line, not a traceback (`ModelError.kind`).
**Takeaway:** the model proposes text. It does nothing by itself.
**Source:** `docs/session-01.md`, `agent/model.py`, `agent/cli.py`.

---

## Video 2 — Tools and the Agent Loop

**Promise:** the model will read a real file on its own and answer from it.

**Outline**
1. What a tool is: a name, a JSON schema and a function. The model never runs it.
2. Build `list_files` and `read_file` (size and output limits, invalid UTF-8).
3. Tool schema: what the model sees vs what your code enforces.
4. The loop: call model → execute → append result with the matching tool-call ID → repeat.
5. Several tool calls in one turn, and why every result must match its ID.
6. Stop rules: step limit, tool-call limit, duration (exit code on failure).

**Demo:** "Explain what `calculator.py` does". Watch the tool calls print.
**Break it:** an unknown tool name and malformed arguments come back as tool results, not crashes.
**Takeaway:** the loop is about 30 lines. The rest of the project is making it safe.
**Source:** `docs/session-02.md` to `session-05.md`, `agent/loop.py`, `agent/tools/`.

---

## Video 3 — Keeping It Safe

**Promise:** the model can ask for anything. Your code decides what happens.

**Outline**
1. Rules enforced by code, not by the prompt. The workspace boundary in `agent/policy.py`.
2. Attack table: `../`, absolute paths, symlinks. The refusal must not reveal whether an outside path exists.
3. File content is untrusted data. Show a file that says "ignore your instructions".
4. Memory also sits outside the workspace, so the agent cannot rewrite its own instructions.
5. Honest limits: path checks are not a full sandbox against hostile concurrent filesystems.
6. The one tool the boundary does not contain: `run_python` runs a workspace `.py` file as your OS user (30 s limit, 8,000-byte output, no shell, API keys not passed to the child). Each run asks for approval, and on macOS it runs under `sandbox-exec` (no network, writes only in the workspace and temp). Elsewhere a script can touch files and the network outside the workspace. So the injection above is only as safe as this tool is trusted. Say it plainly, and use disposable projects.

**Demo:** run the agent against `../../etc/passwd` and a symlink to it; show the refusal.
**Break it:** the prompt-injection file. The agent may try; policy still refuses.
**Takeaway:** a model can be talked into anything. A boundary in code cannot.
**Source:** `docs/session-02.md`, `session-03.md`, `agent/policy.py`, `agent/tools/execution.py`, `tests/test_policy.py`.
**Also show:** the folder trust prompt (`agent/trust.py`), asked before the folder's `.env` is loaded.

---

## Video 4 — How AI Edits Code

**Promise:** edit a file so a wrong edit changes nothing.

**Outline**
1. Why editing is different: every earlier tool was read-only.
2. `replace_text` and the exactly-one-match rule. Zero or many matches → error, file unchanged.
3. Write-then-rename: temp file, `fsync`, `os.replace`. A reader sees old or new, never half.
4. `write_file`: create-only, atomic no-clobber (`os.link`). Never overwrites.
5. Journal the intent before the write so a crash can be recovered (preview of video 6).

**Demo:** fix the subtraction bug in a fixture and show the diff.
**Break it:** an anchor matching two places → refused, then the model asks for more context.
**Takeaway:** a refusal that leaves the file untouched is a feature.
**Source:** `docs/session-06.md`, `session-07.md`, `session-13.md`, `agent/tools/filesystem.py`.

**Say carefully:** two different guarantees (see `docs/video-notes.md`, topic 06). A hard kill can leave a hidden `.agent-edit-*` staging file; the target is never partial.

---

## Video 5 — Budgets and the Interactive CLI

**Promise:** the agent stops before it spends too much, and you can talk to it in a session.

**Outline**
1. Four limits: steps, tool calls, time, and tokens or spend. Check the budget before the next request and leave headroom.
2. Cost from configured prices only. Unpriced spend is refused, not guessed.
3. Input is billed at a cache-hit and a cache-miss rate. Why a resent prefix matters.
4. The chat REPL: conversation history, session state, `--resume` and `--continue`.
5. Why a limit stops explicitly and reports edits already made.

**Demo:** set a tiny `--max-steps`, hit it, read the incomplete report.
**Break it:** `--max-spend` with no prices configured.
**Takeaway:** an agent without a budget is a loop that bills you.
**Source:** `docs/session-05.md`, `docs/interlude-repl.md`, `agent/budget.py`, `agent/chat.py`.

---

## Video 6 — When Agents Fail

**Promise:** timeouts, repeats and growing context do not crash it, and you can see afterward what happened.

**Outline**
1. Retry and backoff: exponential with jitter, bounded by attempts and the run clock.
2. Reads are safe to resend. Writes are not. There is no idempotency key, so after a write the agent does not blindly retry.
3. Repeated identical failing calls → a stop rule.
4. Context growth: measure, bound large outputs, stop cleanly before the limit. Compaction deferred on purpose.
5. The JSONL journal: one record per event, fsynced. `journal.heal()` repairs a torn final record.
6. Crash recovery: kill the process mid-run, then `--resume`.

**Demo:** kill the process between a tool call and its result, then resume and inspect the trace.
**Break it:** a model that fails on command (timeouts, malformed JSON). Show the journal.
**Takeaway:** failures are the normal case. Design for them first.
**Source:** `docs/session-08.md` to `session-10.md`, `agent/retry.py`, `agent/repetition.py`, `agent/context.py`, `agent/journal.py`, `agent/replay.py`, `agent/tracing.py`.

---

## Video 7 — Does It Actually Work?

**Promise:** measure the agent without asking the agent.

**Outline**
1. "Done" from the model is not success. An independent checker the agent cannot see.
2. Fixtures: a fresh byte-identical workspace per run, provably reset.
3. The 20-task suite and its categories. Reserved held-out cases.
4. The A/B comparison: write the hypothesis and decision rule down first, then run 120 interleaved runs.
5. The result: a 1.7-point gain, McNemar p=1.0. It failed the rule committed in advance. Report it as a result.
6. Memory in brief: human-approved notes outside the workspace, hash in the banner.
7. The poisoned-memory audit: wrong hints in memory, 30/30 pass, the file on disk won.

**Demo:** run a fixture through the checker; show a pass and a deliberate fail.
**Break it:** show a wrong memory sending the agent to a file that does not exist, and the recovery.
**Takeaway:** decide how you will judge it before you look at the numbers.
**Source:** `docs/session-11.md`, `session-12.md`, `session-14.md`, `docs/session-14-step8-audit.md`.

**Where the files are:** `evals/`, `fixtures/` and most `scripts/` are no longer on `main`. They live at the git tag `main-with-evals`. Check that tag out for this video, and tell viewers the packaged agent leaves them out.
**Say carefully:** the audit is a sanity check (one model, 5 cases, 3 repeats). Long-run poisoning with agent-written memory is untested because that write path does not exist.

---

## Video 8 — Wrap-Up and What's Next

**Promise:** you know what this agent is, what it is not, and where to go next.

**Outline**
1. The whole picture in one diagram: `cli → loop → model / tools / policy / journal`.
2. The invariants you built, as a checklist (boundary in code, one-match edits, no clobber, journal before write, files beat memory).
3. What it is not: not a sandbox. `run_python` runs trusted scripts with your user's permissions and can reach files and the network outside the workspace. No shell, no package installs, no browsing. Say the limits as clearly as the features.
4. How it compares to tools like Claude Code, Codex and Aider.
5. The roadmap: `apply_patch`, multi-edit, `run_tests`, delete, and why each needs new safety work. `run_python` is the first step into execution, and the roadmap's sandbox and approval work (Docker included) is what would make it safe to widen.
6. Where to get the code and how to follow along.

**Demo:** the three-act run: a successful task, a controlled refusal, an interrupted run followed by recovery.
**Takeaway:** the agent is small. The engineering around it is the point.
**Source:** `docs/session-15.md`, `docs/post-session-15-roadmap.md`, `README.md`.

---

## Before you record

- [ ] Write the Session 1 try-it steps against the real CLI and confirm they match.
- [ ] Rehearse every demo on a disposable fixture.
- [ ] Tag the repo at each video's end state.
- [ ] Large terminal font (about 20pt), dark theme, notifications off.
- [ ] Check `.env` and keys are never on screen.
- [ ] Put the repo link, tag, timestamps and requirements in each description.
- [ ] State the rough API cost, and mention local Ollama as a free option.
