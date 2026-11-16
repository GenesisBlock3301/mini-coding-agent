# Session 13 — Many Runs at Once, and Nothing They Share

**Goal (from the plan):** expose the loop through FastAPI with a separate
workspace per run.
**Done when:** concurrent runs do not share files or conversation history.

Conversation history is already safe: `run_loop` takes its messages as an
argument and `_Run` holds every moving part of a run in one value — history,
step counter, ledger, budget, journal, changed paths. There is no module-level
conversation to share. Files are nearly safe: `workspace_root(override)`,
`--workspace`, and `WORKSPACE_DIR` have existed since Session 7, and
`scripts/fixture.py` already builds a byte-identical disposable workspace per
run under `runs/workspaces/` with a guard that refuses to touch anything it did
not create.

What is *not* safe is everything that is read from the environment once, at
import, by a single-process CLI:

- `active_provider()`, `Pricing.from_env()`, `RetryPolicy.from_env()` and
  `load_dotenv()` all turn the process environment into configuration. A server
  serving two clients with different budgets has no way to express that today.
- `agent.loop.dispatch` is a module global. `crash_demo.py` and
  `fault_matrix.py` monkeypatch it deliberately — which is fine in a subprocess
  and is a data race the moment two runs share an interpreter.
- The retry policy backs off politely, per process. Session 8 wrote the warning
  down already: *backoff cannot fix a rate limit you are causing; one process
  backing off is polite, concurrent runs need a shared limiter.* Today is when
  that bill arrives.
- There is no way to stop a run. Budgets stop it; you cannot. A server needs
  `DELETE /runs/{id}` and the loop has no seam for it.

And one shape problem that decides the whole API: **a run takes seconds to
minutes.** A synchronous `POST /run` that blocks until the agent finishes is a
request that times out in every proxy between you and it, and an HTTP client
that disconnects mid-run leaves an agent editing files for nobody.

---

## A. Decisions to lock before writing code

| #    | Decision                                  | Options                                                                             | My pick + why |
|------|-------------------------------------------|--------------------------------------------------------------------------------------|---------------|
| D123 | Process model                             | thread per run / subprocess per run / rewrite the loop async                          |               |
| D124 | API shape                                 | blocking POST / `202` + polling / `202` + server-sent events                          |               |
| D125 | Where a workspace comes from              | server builds from a named fixture / client uploads an archive / a pre-mounted path   |               |
| D126 | Workspace lifecycle                       | deleted when the run ends / kept with a TTL / kept until asked                        |               |
| D127 | Cancellation                              | none / a cooperative flag checked where budgets are / kill the subprocess             |               |
| D128 | Concurrency limit                         | unbounded / N workers and a queue / N workers and a `429` when full                   |               |
| D129 | Rate limiting                             | nothing, rely on per-run retries / one shared token bucket per key / per-provider      |               |
| D130 | Configuration scope                       | per server (env, as now) / per run in the request body / per server with per-run overrides |               |
| D131 | Authentication                            | none, localhost only / a static bearer token / per-client tokens                      |               |
| D132 | What the API exposes of a run             | the final answer only / plus the ledger / plus the trace (Session 10's D92, over HTTP) |               |
| D133 | Resume over HTTP                          | not exposed / `POST /runs/{id}/resume` / automatic on server restart                  |               |
| D134 | Where `runs/` lives                       | the repo, as now / a configured data directory / per-client directories               |               |

**Hardest two: D123 and D125.**

D123 because every option costs something this project values. Threads are
cheapest and share the interpreter — which means the `dispatch` global, any
`os.environ` mutation and any `monkeypatch`-shaped trick become races, and a
runaway tool blocks a worker you cannot kill. Subprocesses give real isolation,
a real `kill`, and a crash that takes one run instead of the server — at the
price of start-up latency and of the journal being the only channel back.
Rewriting the loop async rewrites the one seam the entire test suite depends
on: `model_fn` is injected and synchronous, and 460 tests call it that way.

D125 because it decides what this server *is*. Building from a named fixture
makes it an eval service: safe, reproducible, useless for real work. Accepting
an uploaded archive makes it a general agent service and hands untrusted input
to a filesystem boundary that the plan has already said is *not a sandbox*. A
pre-mounted path makes it a local tool with a network interface, which is
honest and does not scale past the machine.

**And the sneaky one: D130.** The CLI reads its rules from `.env` and prints
them in the banner, and that is a good design for a process that belongs to one
person. The same code inside a server means every client shares one budget, one
retry policy and one model — and the first time two clients want different
`max_steps`, the fix is to thread configuration through code that currently
reads it from the environment at import time. That is a wide, boring change, and
it is much cheaper to make before there is a server than after.

---

## B. Sequential build order

Phases 0 to 2 are offline: the scripted model makes a concurrency test
deterministic, which is the only reason concurrency is testable at all. Phase 3
is live and small.

### Phase 0 — Find what is actually shared

- [ ] **Step 1 — Inventory the globals.**
  - Build: nothing. List every module-level value that a run reads: the
    provider, the pricing, the retry policy, `dispatch`, `TOOL_SCHEMAS`,
    `RUNS_DIR`, `_ARG_SPECS`.
  - Test: for each, say whether two concurrent runs could ever want different
    values, and whether anything mutates it at runtime.
  - Review: `_ARG_SPECS` and `TOOL_SCHEMAS` are immutable after import and safe.
    `dispatch` is a global that the project's own scripts replace. That
    asymmetry is the finding.

- [ ] **Step 2 — Two runs in one process, by hand.**
  - Build: a test that runs two `run_loop` calls in two threads, with scripted
    models, two workspaces, two journals.
  - Test: both finish; neither's `changed_paths`, ledger or history contains the
    other's. Run it a hundred times.
  - Review: if this passes before you write a line of FastAPI, the loop is
    already concurrency-safe and the rest of the session is about the server. If
    it fails, fix it here where the failure is readable.

### Phase 1 — Configuration that belongs to a run

- [ ] **Step 3 — Make the rules an argument.**
  - Build: D130. `Budget`, `RetryPolicy` and the model configuration become
    values constructed by the caller; `from_env()` stays as the CLI's way of
    building them, not as how the loop finds them.
  - Test: two runs in one process with different `max_steps` and different retry
    policies, both honoured.
  - Review: this is the change that makes a server possible and it is worth
    making even if the server is never written.

### Phase 2 — The server

- [ ] **Step 4 — The job, not the answer.**
  - Build: D123 and D124. `POST /runs` returns a run id immediately;
    `GET /runs/{id}` returns status and, when finished, the result.
  - Test: a client that disconnects does not stop the run; a client that comes
    back gets the answer.
  - Review: this is where D28 shows up again — a run stopped by a limit returns
    no partial answer over HTTP either, and the status has to be able to say
    "incomplete" without a body full of half-work.

- [ ] **Step 5 — A workspace per run, and a guard.**
  - Build: D125 and D126, on top of `fixture.build` and `_require_disposable`.
    Two runs get two directories; neither path is ever supplied raw by a client.
  - Test: a request naming `../../etc` is rejected before anything is created;
    two concurrent runs of the same fixture do not collide.
  - Review: the workspace boundary now protects against a *remote* caller. Same
    code, bigger consequence — say out loud which guarantees are the boundary's
    and which were only ever "it's my laptop".

- [ ] **Step 6 — Stop a run.**
  - Build: D127. A cooperative flag checked exactly where budgets are checked
    (before a step, before a tool call), so cancellation obeys D33: the batch is
    answered, the run stops after it.
  - Test: cancel mid-batch; the journal is complete and resumable; no edit is
    half-applied.
  - Review: a cancel that interrupts a tool is a crash, and Session 8 already
    says what crashes cost. Cancel between calls, not inside one.

- [ ] **Step 7 — One limiter for all runs.**
  - Build: D129. A shared token bucket in front of the provider, because ten
    runs each backing off politely still send ten requests a second.
  - Test: ten concurrent runs against a scripted provider that records arrival
    times; the bucket's rate is visible in the gaps.
  - Review: the per-run retry policy and the shared limiter solve different
    problems — one reacts to a 429, the other prevents causing one. Keep both,
    and keep them separate.

- [ ] **Step 8 — Backpressure.**
  - Build: D128. A worker pool, a queue, and an honest refusal when it is full.
  - Test: submit twenty runs to a pool of four; nothing is lost and nothing
    silently waits forever.

> **Review checkpoint.** Two scripted runs in two threads, a hundred times, with
> no shared state and no flakes — before any of it is behind a port. A
> concurrency bug found through HTTP is a concurrency bug you will debug twice.

### Phase 3 — Live and hostile

- [ ] **Step 9 — Four concurrent fixture runs, for real.**
  - Test: four cases at once; verify all four workspaces independently; compare
    latency against the sequential numbers from Session 12.
  - Review: Session 12's self-test #8 comes due. Which of its numbers survive
    concurrency, and which were artefacts of nothing else running?

- [ ] **Step 10 — Try to break the isolation on purpose.**
  - Test: a task that asks for `../other-run/`; a symlink planted in one
    workspace pointing into another; two runs told to edit the same file name.
  - Review: all three must fail at the boundary, and the boundary must say so
    in the trace. This is Session 2 and 3's work being asked a harder question.

---

## C. The failure table

| What happens | Expected | Notes |
|---|---|---|
| a client disconnects mid-run | the run continues; the result is retrievable | D124 |
| two runs edit the same filename | impossible to collide | separate directories, D125 |
| a task reaches toward another run's workspace | refused at the boundary | Session 2, now remote |
| ten runs hit a 429 at once | one shared limiter, not ten polite backoffs | D129 |
| the queue is full | an explicit refusal, never a silent wait | D128 |
| a run is cancelled mid-batch | the batch is answered, then it stops | D33 still holds |
| a run is cancelled mid-write | must not be possible | cancel between calls only |
| the server restarts mid-run | journals survive; D133 decides who resumes | the journal is the recovery |
| a tool hangs | a per-run deadline ends it | `max_seconds` exists; who enforces it in a thread? |
| two clients, two budgets | both honoured | D130 — today's code cannot |
| an unauthenticated request | refused | D131 |
| a workspace is never cleaned up | a TTL, or a disk that fills | D126 |

---

## D. Brainstorming space

**Scope decision taken before Phase 0:** no multi-agent anything. The plan rules
it out in section 1 and nothing since has made it cheaper. Concurrency here
means *many independent runs*, not agents that talk to each other.

Things I haven't decided:

- D123 through D134. None are closed.
- Whether the server is an eval service or an agent service. D125 decides it,
  and the honest answer may be "an eval service with a documented door" —
  Session 11's suite is the only client that exists.
- Whether `runs/` should become a configured data directory now. It is
  gitignored and unbounded, and a server makes "unbounded" into an incident.
- Whether the CLI should become a client of the server. Tempting, and it would
  mean the CLI inherits every failure mode of a network for a local operation.

Things that surprised me:

- _(fill in during Phases 0 and 3)_

Questions for review:

- `agent.loop.dispatch` is replaced at runtime by two of this project's own
  scripts. Is making that impossible the fix, or is making those scripts use
  subprocesses the fix?
- A thread cannot be killed in Python. If D123 says threads and D127 says
  cancel, what exactly is being cancelled, and what happens to a run whose tool
  is blocked on a slow filesystem?
- The workspace boundary has never faced an adversary who controls the task
  text and nothing else. Over HTTP it does. Does anything in `policy.py` change,
  or does only the threat model?

---

## E1. The invariants, carried forward

> **No rejection may depend on whether the target exists.** (Session 2)
> **No file's contents may change what the agent is permitted to do.** (Session 3)
> **The prompt is not a boundary.** (Session 4)
> **Every tool call gets an answer — including the ones you refuse to run.** (Session 5)
> **A tool that does not succeed changes nothing on disk.** (Session 6)
> **The model's account of what it did is a claim; the workspace is the record.** (Session 7)
> **A retry is an action, not a correction.** (Session 8)
> **Doing the same thing twice is evidence, not an offence.** (Session 9)
> **A trace records what was done, not a copy of what was read.** (Session 10)
> **An eval you cannot fail on purpose is not measuring anything.** (Session 11)
> **A comparison you did not specify in advance is a story.** (Session 12)
> **Two runs may share a process. They may never share a directory, a budget, or a journal.** (new, proposed)

The three nouns are chosen. A directory is correctness (one run's edit must not
be another's input). A budget is accounting (a shared ledger means one client
spends another's money). A journal is identity (two runs appending to one file
produce a history that `rebuild()` will faithfully reconstruct into a
conversation that never happened).

---

## E. Known limits of this server

- **Path checks are not a sandbox.** The plan says it, Session 2 says it, and
  over HTTP it matters more: a hostile process sharing the filesystem defeats
  every check in `policy.py`, and a hostile *task* now arrives from anywhere.
- **Isolation is per directory, not per user.** Two runs cannot see each other's
  workspaces; both can see the machine's CPU, memory and disk, and a run that
  fills the disk ends every other run.
- **A shared limiter is still one process's view.** Two servers against one API
  key is the same problem one level up, and nothing here solves it.
- **Cancellation is cooperative.** A run blocked inside a tool call is not
  cancellable without killing something, and killing something is the crash
  Session 8 characterised: survivable, but not free.
- **Concurrency changes every latency number you have.** Session 12's report was
  measured with nothing else running and does not describe this system.
- **The server has no notion of a user.** D131 can authenticate a caller; it
  cannot stop that caller from asking for a workspace they should not have
  unless D125 made that structurally impossible.

---

## F. Self-test before moving to Session 14

1. Two clients submit runs with different `max_steps`. Which value does each
   run use, and which module reads it?
2. A client disconnects two seconds into a sixty-second run. What is still
   running, what is being billed, and who learns the result?
3. A run is cancelled while its batch has two unanswered tool calls. What does
   the journal look like, and can it be resumed?
4. Ten runs start at once against a provider that allows three requests a
   second. What happens, and which mechanism handled it?
5. The server is killed with `kill -9` during four concurrent runs. What exists
   on disk afterwards, and what would it take to finish those four?
6. A task says "read ../../.env". Name every layer that refuses it and which one
   would have been enough on its own.
7. Where do workspaces go when a run ends, and what is the oldest one on your
   disk right now?

And the one Session 14 will punish a wrong answer to:

8. Session 14 adds memory that outlives a conversation — and this session just
   made conversations concurrent. Whose memory is it? If two runs share one
   memory file, the isolation you built today ends at the one component designed
   to be shared, and a fact written by one client is read by the next.
