# Post-Session-15 Roadmap — a local-first interactive coding assistant

> **STATUS: PLANNED — NOT IMPLEMENTED.**
> Every capability in this document is a proposal. None of it is built, none of
> it is started, and nothing here changes `agent/`, `evals/`, `scripts/` or
> `tests/` until Sessions 1–15 are complete and the roadmap is explicitly
> approved. The 15-session plan in
> [`mini-coding-agent-plan.md`](mini-coding-agent-plan.md) is unchanged and
> remains the only active work.

**Goal.** After Session 15, extend the agent into something installable that
runs from any project directory:

```bash
cd ~/Projects/my-fastapi-app
mini-agent
```

```text
You: Create a FastAPI authentication module.

Agent: Inspecting project structure...
Agent: Creating auth.py...
Agent: Updating main.py...
Agent: Running relevant tests...
Agent: Reporting results.
```

Runtime and tools execute locally. Inference may be remote (DeepSeek, OpenAI,
Anthropic). This stays a learning instrument: the point is to understand what
each capability costs in safety and reliability, not to ship a product.

**The one-line summary of this whole document:** the loop is finished, the
boundary is young, and every milestone past M3 widens what a mistake can do.

---

## 1. What already exists and is reusable

Read this before proposing anything. Most of the hard parts are built — what is
missing is mostly the parts that make mistakes expensive.

| Capability | Where | Reusable for |
|---|---|---|
| Agent loop: steps, batches, tool-call id matching | `agent/loop.py` (S4–S5) | everything |
| Workspace override, already a parameter not a constant | `policy.workspace_root(override)` (S2, D1) | M1 |
| Path boundary: absolute, containment, symlink, null-byte, no existence oracle | `agent/policy.py` (S2) | M1, M4–M6 |
| Schema-driven argument validation, one source of truth | `agent/tools.py` `_ARG_SPECS` (S6, D41) | every new tool |
| Writing-tool registry with an import-time check that writers take `path` | `tools.WRITES` (S8, D73) | M4–M6 |
| Atomic single-match edit | `tools.replace_text` (S6) | M5 |
| Append-only journal, fsync per record, torn-tail healing | `agent/journal.py` (S6–S8) | M4–M8 |
| Crash recovery: intent/outcome pairing, `--resume` | `journal.rebuild`, `loop._start` (S6, S8) | M4–M8 |
| Replay safety by file fingerprint — an interrupted edit is never repeated blind | `agent/replay.py` (S8, D72/D73) | M4–M6 |
| Retry policy separating *retryable* from *safe to retry* | `agent/retry.py` (S8, D69) | M8 |
| Repetition rule: canonical signature, cumulative, resume-proof | `agent/repetition.py` (S9) | M8 |
| Context measurement and a step that refuses to start | `agent/context.py` (S9, D86) | M9 |
| Every limit in one frozen, printable value | `budget.Budget` (S5, D31) | M2, M9 |
| Interactive REPL over `run_loop`, with session budgets and `:reset` | `agent/chat.py` (interlude) | M2, M10 |
| Provider table: two providers, one adapter | `model.PROVIDERS` (S3 interlude) | M11 |
| Disposable fixture workspaces + an independent verifier | `scripts/fixture.py`, `fixtures/` (S7) | every milestone's tests |
| Fault injection across crash points, graded by the verifier | `scripts/fault_matrix.py` (S8) | M4–M7 |
| Rule auditing against recorded journals | `scripts/repeat_audit.py` (S9) | M8 |
| *(S10–S15, not yet built)* traces, eval suite, FastAPI, memory, Docker | — | M9, M12, M13 |

**The invariants this roadmap may not break.** They were each paid for with a
session and they are the acceptance criteria behind every milestone below.

> No rejection may depend on whether the target exists. (S2)
> No file's contents may change what the agent is permitted to do. (S3)
> The prompt is not a boundary. (S4)
> Every tool call gets an answer — including the ones you refuse to run. (S5)
> A tool that does not succeed changes nothing on disk. (S6)
> The model's account of what it did is a claim; the workspace is the record. (S7)
> A retry is an action, not a correction. (S8)
> Doing the same thing twice is evidence, not an offence. (S9)

---

## 2. What is missing, and what it will cost

### 2.1 Capability gaps

| Gap | Today | Needed by |
|---|---|---|
| No packaging | no `pyproject.toml`, run via `python -m agent.cli` | M10 |
| Workspace defaults to `<project>/playground`, anchored to the repo | `policy.DEFAULT_WORKSPACE` | M1 |
| `runs/` is a directory inside this repository | `cli.RUNS_DIR` | M1, M2 |
| **`--resume` does not read the workspace back from the journal** | `cli._open_resumed_run` takes `root` from the flag/default, while `run_started` has recorded `workspace` since S7 (D62) | M2 |
| No file creation, deletion, rename, or multi-file edit | `WRITES == {"replace_text"}` | M4–M6 |
| No command execution of any kind | — | M7 |
| No approval or consent mechanism; every tool runs unattended | — | M3, M6, M7 |
| No ignore rules; `list_files` caps at 200 entries / 8KB | `tools.MAX_ENTRIES` | M1 |
| `read_file` caps at 64KB with no offset, so a large file cannot be read in parts | `tools.MAX_READ_BYTES` | M4 |
| Symlink *components* are rejected outright | `policy._check` step 5 | M1 |
| No concurrent-modification detection between read and edit | `replace_text` matches text, not a hash | M5 |
| Single provider family (OpenAI-shaped) | `model.PROVIDERS` | M11 |
| No installation, update or config story | — | M10, M11 |

### 2.2 Dependencies this will pull in

Today the whole project is `httpx`, `python-dotenv`, `pytest`. Expect to add,
in roughly this order, and only when a milestone needs it: a packaging backend
(`hatchling` or setuptools, M10), a prompt/REPL library if `chat.py`'s
hand-rolled loop proves insufficient (`prompt_toolkit`, M10 — defer as long as
possible), an ignore-file parser (`pathspec`, M1), and an Anthropic/OpenAI SDK
*only if* their wire formats stop fitting the one adapter (M11). Resource
limits on POSIX need no dependency (`resource`, `subprocess`); on Windows they
need a different mechanism entirely, which is an unresolved decision below.

### 2.3 Security risks, ordered by blast radius

1. **`run_command` is the whole game.** Every other milestone widens what the
   agent can write; this one widens what it can *do*. The plan already says
   path checks are not a sandbox and Docker alone is not a security boundary.
   A command allowlist is not one either: `python -c ...`, `make`, `npm test`
   and `git` all run arbitrary code by design, and a project's own test suite
   is attacker-controlled content the moment the repository is not yours.
2. **Prompt injection becomes code execution.** S3's invariant — file contents
   are data, never instructions — is enforced today by the fact that the worst
   a file can talk the model into is reading or editing another file inside the
   workspace. After M7, a `README.md` saying "run `curl … | sh`" is one
   approval click from succeeding, and after M12 (memory) a single injection
   can persist across sessions.
3. **The workspace becomes the user's real code.** Every fixture to date is
   disposable. From M1 the agent edits work that is not reproducible by
   `scripts/fixture.py build`, so "a tool that does not succeed changes nothing
   on disk" stops being a nice property and becomes the only thing between a
   bug and someone's afternoon.
4. **Secrets inside the workspace.** `.env`, `~/.aws`, `id_rsa`, `.git/config`
   with tokens — all currently outside `playground/` by construction, all
   inside a real project directory by default. `read_file` has no notion of a
   file it should refuse to read.
5. **Destructive operations have no undo.** `delete_file` and `rename` have no
   atomic-and-recoverable story the way `replace_text` does; the journal can
   record that a delete happened, which is not the same as being able to put
   the file back.
6. **Journals leak file contents.** Tool results are recorded in full, so
   `runs/*.jsonl` will contain source code and anything secret the agent read.
   S10 excludes secrets from traces; a global `~/.mini-agent/runs/` makes the
   retention question real.

---

## 3. Milestones

Every milestone below is **PLANNED — NOT IMPLEMENTED**. Decisions are numbered
`R1…` to keep the `D…` series free for Sessions 10–15.

Each one owes the same four things, in the house style: a goal, what must exist
first, what "done" means in terms something other than the agent can check, and
the tests that prove it.

---

### Phase 1 — Local CLI and workspace selection

#### M1 — The agent runs in a directory it did not ship with
**PLANNED — NOT IMPLEMENTED**

**Goal.** `cd ~/any/project && python -m agent.cli --tools "…"` operates on the
current working directory, with the boundary guarantees intact.

**Prerequisites.** Sessions 1–15 complete. S13's per-run workspace isolation is
the design this generalises.

**Decisions to lock.**

| # | Decision | Options |
|---|---|---|
| R1 | What the default workspace is | cwd / cwd only when it looks like a project (`.git`, `pyproject.toml`) / always explicit |
| R2 | Whether a symlinked component inside the workspace stays a hard refusal | keep / allow when the target resolves inside the root / allow with a per-run flag |
| R3 | Which files are invisible | nothing / `.gitignore` + `.git/` / a dedicated `.mini-agent-ignore` / both |
| R4 | Whether some files are unreadable regardless of path | no / a denylist (`.env`, `*.pem`, `id_*`, `.aws/*`) / denylist + a warning in the result |
| R5 | Where run journals live | repo `runs/` / `~/.mini-agent/runs/` / `<workspace>/.mini-agent/runs/` |

**Acceptance criteria.**
- Launched from two different directories, two runs touch two different trees,
  and neither can name a path outside its own root (the S2 test suite, run
  against a cwd workspace rather than `playground/`).
- A workspace containing a symlink to `/etc` is refused by the same code and
  the same error code as today — no new rejection path, no existence oracle.
- `list_files` on a repository with `node_modules/` or `.venv/` returns
  something a model can act on rather than 200 truncated names (R3).
- Journals are written outside the workspace, so the agent cannot read or edit
  its own history with `read_file`/`replace_text` (the S14 memory argument,
  applied to traces).

**Required tests.** The whole of `tests/test_policy.py` re-run against a
temporary cwd-style root; an ignore-rule suite including a `.gitignore` with
negations; a test that the journal directory is outside the workspace for every
supported R5 choice; a test that a denylisted file is refused with the same
code whether or not it exists (S2's invariant).

---

#### M2 — A run remembers where it ran
**PLANNED — NOT IMPLEMENTED**

**Goal.** `--resume` reconstructs the workspace from the journal, not from the
shell that happens to be resuming.

**Prerequisites.** M1.

**Why it is its own milestone.** `run_started` has recorded `workspace` since
S7 (D62) and `cli._open_resumed_run` ignores it: it resolves the root from the
flag or the default. While the only workspace was `playground/` that was
invisible. With M1 it is a correctness bug with a crash in front of it — resume
from the wrong directory and S8's replay fingerprints compare a recorded hash
against a file in a different tree, which reports `UNKNOWN` and refuses, or
worse, matches a same-named file and replays an edit into it.

**Acceptance criteria.**
- Resuming from an unrelated cwd operates on the recorded workspace.
- Resuming when the recorded workspace no longer exists fails with a named
  usage error before any model call.
- A `--workspace` that disagrees with the journal is refused, not silently
  preferred (R6: refuse vs warn-and-use-the-journal vs allow with a flag).
- `plans_for` keeps producing `REPLAY`/`CHANGED`/`UNKNOWN` against the right
  tree — proven by `fault_matrix.py` run from a different directory.

**Required tests.** Resume from `/tmp` of a run recorded in a fixture
workspace; resume with a deleted workspace; resume with a conflicting
`--workspace`; the S8 crash matrix executed with the resuming process started
elsewhere.

---

#### M3 — Approval, before anything needs it
**PLANNED — NOT IMPLEMENTED**

**Goal.** One mechanism that can pause a tool call, show the user exactly what
is about to happen, and record the answer in the journal.

**Prerequisites.** M1. Deliberately placed **before** M4–M7 rather than after:
building consent once, while the only writer is `replace_text`, is how it ends
up with a single shape. Bolted onto `run_command` later it becomes a special
case that the file tools never adopt.

**Decisions to lock.**

| # | Decision | Options |
|---|---|---|
| R7 | What needs approval | nothing / every write / writes outside a per-run allowlist / by tool class |
| R8 | Granularity of "yes" | per call / per tool for this run / per path prefix / remembered across runs |
| R9 | What a refusal is | a `ToolResult` the model can work around / a hard stop with its own stop reason |
| R10 | Non-interactive runs (evals, CI, S13's server) | auto-deny / auto-approve with a flag / refuse to start |

**Acceptance criteria.**
- A refused call is still *answered* (S5's invariant) and the run continues or
  stops per R9 — never an unanswered `tool_call_id`.
- The approval prompt shows the diff/command verbatim, not the model's
  description of it (S7's invariant: the claim is not the record).
- Every decision is journalled with the intent, so a resumed run does not
  re-ask and cannot silently re-decide.
- The eval suite (S11) runs unattended under R10 and records the policy it ran
  under as part of the configuration (S12 compares one variable at a time).

**Required tests.** Approve/deny/timeout paths; a denied call mid-batch with
two calls behind it; a resumed run that had an approved-but-uncompleted write;
a scripted non-interactive run proving no prompt can block it.

---

### Phase 2 — File creation and advanced editing

> **The warning the brief gives, restated as a rule:** `replace_text`'s replay
> logic is *not* transferable. It is safe because the tool is atomic and
> because a zero-match refusal is a detectable signal that the edit already
> landed. Neither property holds for a create, a delete or a rename, and each
> tool below owes its own answer to "what does the journal say, and what does
> the disk say, after a crash in the middle".

#### M4 — `write_file`
**PLANNED — NOT IMPLEMENTED**

**Goal.** Create a new file, or replace one wholesale, atomically.

**Prerequisites.** M1, M3.

**The crash question.** `replace_text` can tell "already applied" from "never
ran" because the anchor is gone in one case and present in the other.
`write_file` cannot: the fingerprint of a file that did not exist is `ABSENT`
(S8 already has the value), and after a crash the file either exists with the
new content or does not. That is *answerable* — but only if the intent records
the `ABSENT` fingerprint, and only if "create" and "overwrite" are different
operations with different refusals (R11: separate tools, a `mode` argument, or
an `overwrite: false` default).

**Acceptance criteria.**
- Temp-file-plus-`os.replace` within the same directory; no partial file is
  ever visible, and nothing is written outside the workspace.
- Overwriting an existing file without saying so is refused by default (R11).
- A crash between intent and outcome leaves the file entirely old or entirely
  new, proven by the fault matrix, and `--resume` never recreates a file the
  user deleted in between.
- Parent directories: created implicitly or refused, decided and written down
  (R12). Implicit creation means a typo'd path silently makes a tree.

**Required tests.** `fault_matrix.py` extended with `write_file` cases at all
four crash points; create-over-existing; create where a *directory* has that
name; path-boundary suite re-run for the new tool; a journal-replay test
proving the `ABSENT` fingerprint distinguishes the two worlds.

---

#### M5 — `apply_patch` and multi-file edits
**PLANNED — NOT IMPLEMENTED**

**Goal.** Structured, reviewable changes — several hunks, possibly several
files, applied as one logical operation.

**Prerequisites.** M4.

**The hard part is not the patch format.** It is that "one logical operation"
across N files is not atomic on any filesystem this project targets. A crash
halfway leaves three files changed and two not, and the journal must make that
state *readable* rather than pretend it did not happen (R13: all-or-nothing via
staging and a reverse journal, or per-file atomicity with an explicitly partial
outcome record).

**Also unresolved:** concurrent modification. Today `replace_text` matches on
text, so an edit against a file the user changed under it either fails to match
or — worse — matches in the wrong place. The plan's own Retry and Edit Safety
section asks for a content or hash precondition (R14), and S8 already computes
exactly that hash for the journal.

**Acceptance criteria.**
- A patch that cannot be applied in full changes nothing, or records precisely
  which parts applied — never "mostly succeeded" with no record.
- An edit whose precondition hash no longer matches is refused as stale, with
  an error code distinct from "no match" (R14).
- Fuzz: a patch whose hunks overlap, whose context is ambiguous, or that
  targets a file outside the workspace, each refused with the existing codes.

**Required tests.** Crash points *between* files, not only within one; a stale
precondition; the S7 fixtures converted to multi-file cases; a property test
that applying a patch twice is detectably refused the second time.

---

#### M6 — Delete and rename
**PLANNED — NOT IMPLEMENTED**

**Goal.** Remove and move files, under explicit policy.

**Prerequisites.** M3, M5. Last in Phase 2 on purpose: it is the only pair with
no natural undo.

**Decisions to lock.** R15: hard delete vs move-to-trash inside the journal
directory (recoverable, costs disk, leaks content) vs refuse-to-delete-ever.
R16: whether a rename across directories is one operation or a copy plus a
delete in the trace. R17: whether deleting a file the agent never read is
allowed at all.

**Acceptance criteria.**
- Deletion requires approval under every R7 setting that is not "nothing".
- A crash mid-rename leaves exactly one of the two names present, never both
  and never neither; the journal says which.
- A deletion is recoverable for the life of the run (R15), or the refusal to
  make it recoverable is written down with its reasoning.
- No tool can delete the journal, the memory file (S14), or anything outside
  the workspace — asserted, not assumed.

**Required tests.** Crash at every point of a rename; delete-then-resume;
delete of a symlink, a directory, a non-existent file, and the workspace root
itself; the full policy suite re-run per tool.

---

### Phase 3 — Command execution and verification

#### M7 — `run_command`, and the sandbox question it forces
**PLANNED — NOT IMPLEMENTED**

**Goal.** Run the project's tests, linters and builds; read the output; use it
to verify a change before reporting success.

**Prerequisites.** M3 (approval), M4–M6, and — stated plainly — a decision
about isolation that this project has so far been allowed to defer.

**This is the milestone that changes the threat model.** Everything before it
widens what the agent can *write* inside one directory. This one lets it
execute code, which means the boundary is no longer a path check. The plan
already says so twice: path checks are not a complete sandbox, and Docker alone
is not a complete security boundary.

**Decisions to lock.**

| # | Decision | Options |
|---|---|---|
| R18 | Where a command runs | host process / host with `resource` limits / container / VM / user's choice per project |
| R19 | What may run | anything with approval / an allowlist / an allowlist of *project-defined* commands read from config (which is attacker-controlled content) |
| R20 | Network during execution | inherited / denied by default / denied unless approved |
| R21 | Timeout and what an expiry means | kill and report / kill and refuse to interpret the partial output |
| R22 | Process cleanup | kill the child / kill the process group / a session or cgroup |
| R23 | Output handling | the existing 8KB/64KB caps / head+tail / a separate cap with its own stop reason |
| R24 | Whether the model may see exit codes it can loop on | yes / yes with the repetition rule armed (S9) / summarised |

**Acceptance criteria.**
- A command that spawns children and is then timed out leaves no survivors —
  verified by process accounting, not by the absence of complaints.
- A command producing 1GB of output neither fills memory nor the journal.
- A timeout produces an *explicitly ambiguous* outcome: the command may have
  completed its side effects, and the result says so rather than reporting
  failure (the S8 lesson about read timeouts, applied to processes).
- A repository whose `Makefile`/`package.json`/`conftest.py` contains hostile
  instructions cannot escalate beyond the configured R18/R20 boundary — tested
  with a deliberately hostile fixture, as `playground-evil/` already is for
  paths.
- The journal records the command, its exit code, its duration, and a bounded
  excerpt — with secrets excluded (S10).

**Required tests.** A hostile fixture repository; a fork bomb under R18's
limits; a command that writes outside the workspace; a command that reads an
environment variable holding the API key (it must not be in the child's
environment); timeout with and without partial output; crash of the *agent*
while a child runs; `fault_matrix.py` extended to command execution.

---

#### M8 — Verification as a first-class outcome
**PLANNED — NOT IMPLEMENTED**

**Goal.** "I changed X and the tests pass" becomes a claim backed by a recorded
command and its exit code, rather than a sentence the model wrote.

**Prerequisites.** M7, and S11–S12's eval suite.

**Acceptance criteria.**
- A run that edited files and did not run a verification command is reported
  differently from one that did — the distinction is in the result object and
  the exit code, not only in prose.
- The repetition rule (S9) survives contact with test loops: an agent that runs
  the same failing test five times is stopped, and one that runs it before and
  after an edit is not. Audited with `repeat_audit.py` against real journals,
  the same way the thresholds were chosen.
- Eval metrics gain "verified by command" alongside "verified by the external
  checker", and S12's report keeps them separate — the agent's own test run is
  still the agent's claim.

**Required tests.** An agent that reports success against a failing suite must
be caught by the external verifier; a run where the test command itself is
broken; repetition audit over command-running journals.

---

### Phase 4 — Developer experience

#### M9 — Long sessions: history that outlives the context window
**PLANNED — NOT IMPLEMENTED**

**Goal.** An interactive session that lasts hours without hitting S9's context
stop every twenty minutes.

**Prerequisites.** S9 (done), S12's report (the evidence base), S14's memory.

**This is where compaction finally gets decided**, and the plan defers it for a
reason this project can already state: a summary that drops a `tool_call` id
produces a history the provider rejects, and one that drops a file's exact
bytes produces an `old` that no longer matches. R25: drop-oldest with
tool-call-pair integrity, summarise with a protected tail, or re-read on
demand and keep the history short by design. The honest version of this
milestone is "measure what each option costs in task success on the S12 suite
before shipping one".

**Acceptance criteria.** No compaction strategy may produce a history that the
provider rejects, or an `old` string that no longer matches its file; measured
against the eval suite with success rate reported before and after.

**Required tests.** A 200-message session replayed through each strategy with
pairing asserted; an eval run with compaction on vs off, same seed, reported
as a delta.

---

#### M10 — Installable `mini-agent`
**PLANNED — NOT IMPLEMENTED**

**Goal.** `pipx install mini-agent` (or `uv tool install`), then `mini-agent`
from any directory.

**Prerequisites.** M1, M2.

**Decisions to lock.** R26: packaging backend and whether the repo keeps
`requirements.txt` alongside a `pyproject.toml`. R27: config location and
precedence — flags > env > `./.mini-agent.toml` > `~/.config/mini-agent/` —
and the rule that **a config file inside the workspace is attacker-controlled
content** (it may set defaults; it may never widen the boundary, grant
approval, or add an allowlisted command). R28: whether `chat.py`'s hand-rolled
REPL grows or is replaced.

**Acceptance criteria.**
- Installing into a clean environment and running against a fresh project
  works from the README alone, on a machine that is not the author's (S15's
  criterion, extended past Docker).
- No API key is ever read from inside the workspace.
- `--version` reports the installed version, and the journal records it (S12
  requires the code version alongside every result).

**Required tests.** An install smoke test in CI; a config-precedence suite
including a malicious workspace config that tries to disable the boundary.

---

#### M11 — More providers, same adapter
**PLANNED — NOT IMPLEMENTED**

**Goal.** Anthropic and OpenAI alongside DeepSeek and Ollama.

**Prerequisites.** M10.

**Note.** `model.py` is one adapter over an OpenAI-shaped wire format with a
provider table of defaults — not an abstraction over two protocols. Anthropic's
message and tool-use shapes differ enough that R29 is a real decision: a second
adapter module, or a translation layer, and which one keeps `loop.py` free of
provider knowledge. Resist a framework.

**Acceptance criteria.** Adding a provider touches `model.py` and the provider
table only; the loop, journal and tools are untouched; the provider id, model
id and context window are recorded per run; pricing stays configured, never
hard-coded (S5, D37).

**Required tests.** The existing provider suite parameterised across providers;
a recorded-response replay per provider so the suite stays offline.

---

### Phase 5 — Optional future extensions

#### M12–M14 — Gateway, hosted execution, multi-user
**PLANNED — NOT IMPLEMENTED — NOT RECOMMENDED YET**

Managed model gateway, authentication and quotas, cloud execution, remote
workspaces, multi-user orchestration, billing.

**Prerequisites.** A stable local CLI with M1–M11 complete and an eval suite
that has caught at least one real regression. Nothing here is a learning
objective of this project, and each item converts a local reliability problem
into a distributed one. S13's FastAPI interface is the only piece of this that
the original plan asks for, and it exists to teach per-run isolation rather
than to be deployed.

The engineering principle that applies: **no premature cloud infrastructure.**

---

## 4. Recommended implementation order after Session 15

```
M1  workspace from cwd          ──┐
M2  resume keeps its workspace    │  Phase 1: make it portable before
M3  approval mechanism          ──┘  anything can do more damage

M4  write_file                  ──┐
M5  apply_patch / multi-file      │  Phase 2: each with its own crash story
M6  delete / rename             ──┘

M7  run_command + isolation     ──┐  Phase 3: the threat-model change
M8  verification as an outcome  ──┘

M9  long sessions / compaction  ──┐
M10 installable package           │  Phase 4: make it pleasant
M11 providers                   ──┘

M12+ anything hosted               Phase 5: not yet, and probably not here
```

Three ordering claims worth defending:

1. **M3 before M4.** Approval built while `replace_text` is the only writer
   gets one shape. Built after `run_command` it gets two, and the file tools
   never adopt the second.
2. **M2 immediately after M1.** The resume/workspace mismatch is latent today
   and becomes a data-loss bug the moment the workspace is real.
3. **M7 after all of Phase 2.** Command execution is the only milestone that
   can damage something outside the workspace. Every crash-recovery habit
   should already be in place before it lands.

---

## 5. Architectural risks and unresolved decisions

**Risks.**

- **Sandboxing is the unsolved problem.** Nothing in M7's option list is both
  easy and sufficient. Host execution with `resource` limits is a speed bump,
  not a boundary; containers are a boundary with a bad developer experience for
  a tool whose whole point is operating on the user's real checkout. This may
  end as "documented, limited, approved per command, and honest about what it
  does not prevent" — which is acceptable for a learning instrument and must be
  stated rather than implied.
- **The security model is per-path and will need to become per-capability.**
  `policy.py` answers "may this path be touched". It cannot answer "may this
  agent run this command", "may it reach the network" or "may it read this
  secret". Those are different questions and probably a different module.
- **Prompt injection scales with capability.** Today the worst case is a
  misleading answer. After M7 it is code execution; after S14's memory it is
  persistent code execution. Every capability milestone should add a hostile
  fixture, the way `playground-evil/` did for paths.
- **Windows.** Process groups, `os.replace` semantics, symlink handling and
  resource limits all differ. Supporting it is a decision, not an afterthought;
  not supporting it should be written in the README.
- **The eval suite must grow with the tools, or it stops being evidence.**
  S11's 10 cases and S12's 20 cover read and single-edit tasks. A `write_file`
  or `run_command` with no fixture is an unmeasured capability, and S12's
  comparison method is the only thing that has been keeping opinions honest.
- **Journal size and retention.** Full tool results in `~/.mini-agent/runs/`
  means source code on disk indefinitely. S10 decides retention for a repo
  directory; a global one needs the decision again, with secrets.

**Unresolved decisions, collected.** R1–R5 (workspace, symlinks, ignore rules,
secret files, journal location), R6 (resume vs explicit workspace), R7–R10
(approval shape and non-interactive policy), R11–R12 (`write_file` semantics),
R13–R14 (multi-file atomicity, stale-edit preconditions), R15–R17 (delete and
rename), R18–R24 (execution isolation, allowlists, network, timeouts, cleanup,
output, exit codes), R25 (compaction), R26–R28 (packaging, config precedence,
REPL), R29 (second provider shape).

None are decided here. Each belongs at the top of its milestone's own document,
in the same table shape every session has used, filled in **before** code.

---

## 6. Relationship to the 15-session plan

This roadmap does not alter, reorder, replace or reinterpret
[`mini-coding-agent-plan.md`](mini-coding-agent-plan.md). Sessions 1–15 remain
the active work and their completion criteria are unchanged. The plan's
"Final Deliverables" section is the entry condition for M1.

Work begins on this document only when the user explicitly requests it after
Session 15 is complete.
