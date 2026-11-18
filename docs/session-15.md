# Session 15 — It Works on My Machine, and Other Things That Are Not True

**Goal (from the plan):** add Docker, documentation, and a local-project demo.
**Done when:** the documented setup works in a fresh environment and a user can
inspect changes and recovery in the local terminal workflow.

The demo must show workspace selection, the provider and budget banner, the
journal location, changed paths, and an incomplete run followed by recovery.
Verify the resulting files independently of the agent's explanation. Document
when to use a disposable fixture before trying the workflow on real code.

Every criterion before this one could be checked by running something. This one
can only be checked by *someone else* running something, and the whole session
is about taking that seriously rather than reading your own README with your own
`.env` already on disk and declaring it clear.

What the project has that helps:

- `requirements.txt`, a `README.md` that already documents providers, the
  workspace flag, failure injection and the retry variables, and a `.env`
  pattern where the key is never in a file the agent can read.
- Three artefacts that are already demos in everything but name:
  `scripts/crash_demo.py` (a controlled failure, free), `scripts/fault_matrix.py`
  (twelve runs, no network), and the Session 12 report (the evaluation results).
  The plan asks for exactly those three things — *a successful task, a
  controlled failure, and the evaluation results* — and two of them already run
  without a key.
- A boundary that has been attacked on purpose since Session 2, and a workspace
  model (`--workspace`, `WORKSPACE_DIR`, disposable builds) that maps cleanly
  onto a mounted volume.

What it does not have, and what will break first in a fresh environment:

- **State and code are mixed in one directory.** `runs/` holds journals,
  `playground/` is the writable workspace, `fixtures/` is read-only source of
  truth, `.env` is a secret, and all four live in the repository. A container
  has to separate them and the separation does not exist yet.
- **`playground/` is generated.** `scripts/make_fixtures.py` builds it, it is
  gitignored, and it contains symlinks pointing outside the repo. A fresh clone
  has an empty workspace and the first documented command reads from it.
- **The Ollama path assumes localhost.** `OLLAMA_CONTEXT_LENGTH=32768 ollama
  serve` on the host is not reachable from inside a container by default, and
  the `context_warning` that protects against silent truncation is only useful
  if the connection works at all.
- **Nothing has ever been installed from scratch.** The versions that work are
  the versions on one machine, in one virtualenv, accumulated over fifteen
  sessions.

---

## A. Decisions to lock before writing code

| #    | Decision                                      | Options                                                                          | My pick + why |
|------|-----------------------------------------------|-----------------------------------------------------------------------------------|---------------|
| D147 | Base image                                    | `python:3.12-slim` / `-alpine` / a distro base                                     | **`python:3.12-slim`.** |
| D148 | What is in the image                          | code only, state mounted / code plus fixtures / everything, state in a named volume | **Code, fixture templates, tests and report; all generated state mounted.** |
| D149 | User                                          | root / a non-root user, and who owns the mounted workspace                          | **UID 10001 by default; host UID/GID override for Linux bind mounts.** |
| D150 | How the key arrives                           | `-e DEEPSEEK_API_KEY` / `--env-file` / a mounted secret / never, local provider only | **Runtime environment file**, never an image layer. The actual name is `DEEPSEEK_APIKEY`. |
| D151 | The entrypoint                                | local CLI / interactive chat / a command argument that selects                      | **`agent.cli` entrypoint**; override with `python -m agent.chat`. |
| D152 | Ollama from a container                       | documented host networking / unsupported in the image / a compose file with both    | **Document `host.docker.internal`** and the context-length setting. |
| D153 | How "fresh environment" is proved             | a clean container / a second machine / CI that builds from a clean checkout         | **No-cache image build and runtime tests**, with no host env or cache. No second-machine test. |
| D154 | Dependency pinning                            | exact `==` / ranges / a lockfile with hashes                                        | **Exact direct pins** in `requirements.txt`; transitive dependencies remain unpinned. |
| D155 | The demo's form                               | one script / a README walkthrough / a recording                                     | **One script plus README commands.** |
| D156 | Whether the demo spends money                 | fully scripted and free / one live task / both, selected by a flag                  | **Scripted and free by default; `--live` opts in.** |
| D157 | What the README promises                      | a learning project / a usable tool / a tool with a stated threat model              | **Learning project with explicit threat model and limits.** |

**Hardest two: D148 and D157.**

D148 because the project's directories were never designed as a deployment
boundary and three of them are load-bearing in different ways. `fixtures/` is
read-only input that belongs *in* the image — it is source of truth and Session
7 proved builds from it are byte-identical. `runs/` is durable state that must
outlive a container, because a journal is how a crashed run is recovered and a
container that is replaced takes every unmounted journal with it. The workspace
is the one thing that must be *writable and disposable*, which is the opposite
of both. Session 14's memory file is a fourth category: state that must persist
and must never be in the image, or everyone who pulls it inherits your facts.

D157 because the plan's own warning deserves to be in the README rather than
only in a doc: *Docker alone is not a complete security boundary*, and neither
are path checks. A README that says "runs untrusted tasks safely in a container"
would be the single most dangerous sentence in this repository. The accurate
claim is narrower and worth stating precisely: the agent can only reach files
under one directory, it cannot execute commands, and the container bounds what a
mistake can touch — not what an attacker can do.

**And the sneaky one: D153.** The only honest test of a fresh environment is one
with nothing of yours in it: no `.env`, no `playground/`, no virtualenv, no pip
cache, no `runs/`. The temptation is to build the image on the machine that has
all five and declare the build reproducible. A `.dockerignore` that omits `.env`
is not a security feature if the file is copied in at build time by a `COPY . .`
you did not re-read.

---

## B. Sequential build order

### Phase 0 — Separate state from code

- [x] **Step 1 — Name the four categories.**
  - Build: nothing. For every directory in the repo, write down whether it is
    code, read-only input, durable state, or disposable state.
  - Test: the list covers `agent/`, `scripts/`, `tests/`, `fixtures/`, `evals/`,
    `runs/`, `runs/workspaces/`, `playground/`, `.env`, and Session 14's memory.
  - Review: anything you cannot classify is a thing that will be mounted wrong.

- [x] **Step 2 — Make the locations configurable.**
  - Build: `RUNS_DIR`, the workspace root and the memory path all come from
    configuration with today's values as defaults.
  - Test: the whole suite passes with all three pointed somewhere else.
  - Review: this is the change that makes the container possible and it is
    worth making even if the container is never built.

### Phase 1 — The image

- [x] **Step 3 — Build it, small and non-root.**
  - Build: D147, D148, D149. A `.dockerignore` written *before* the
    `Dockerfile`, excluding `.env`, `runs/`, `playground/`, `.venv`,
    `__pycache__` and `.pytest_cache`.
  - Test: `docker run` the image and `cat /app/.env` — there must be nothing to
    read. Then check the image size and what is in it.
  - Review: a secret that was ever in a layer is in the image forever, even if a
    later layer deletes it.

- [x] **Step 4 — Mount the state.**
  - Build: D148. A volume for `runs/`, a volume or bind for the workspace, a
    read-only mount for memory if Session 14 built it.
  - Test: run a task, kill the container, start a new one, `--resume` the run.
    The journal must be there, and the edited file must be too.
  - Review: that test is the entire argument for mounting `runs/`. If a replaced
    container cannot resume, the durability from Sessions 5 to 8 stops at the
    container boundary.

- [x] **Step 5 — Permissions, which is where this always goes wrong.**
  - Test: a bind-mounted workspace owned by the host user, written by a
    non-root container user.
  - Review: if the answer is "run as root", say which guarantee you traded and
    whether a demo is worth it.

### Phase 2 — The documentation

- [x] **Step 6 — Write for a machine that has nothing.**
  - Build: a quickstart that starts at `git clone` and ends at a successful
    task, naming every prerequisite, including the generated `playground/` that
    a fresh clone does not have.
  - Test: follow it yourself, literally, in a clean container, pasting commands
    without fixing anything silently.
  - Review: every command you had to adjust is a documentation bug, and the
    adjustment you made without noticing is the one the next person will not.

- [x] **Step 7 — Document the limits as prominently as the features.**
  - Build: D157. The threat model in plain words: one directory, no command
    execution, no network for the agent, path checks are not a sandbox, the
    container bounds a mistake rather than defeating an attacker.
  - Review: the plan's sentence about Docker belongs here verbatim rather than
    softened.

- [x] **Step 8 — Record what the project actually is.**
  - Build: the invariant list, the fifteen sessions' decisions, and what is
    deliberately *not* built: no command execution, no browsing, no package
    installation, no multi-agent, no compaction, no retrieval.
  - Review: the deferred list is a feature of the design. A reader who cannot
    tell "not built yet" from "decided against" will assume the first.

### Phase 3 — The demo

- [x] **Step 9 — Three acts, free by default.**
  - Build: D155 and D156. *A successful task*: one fixture case end to end.
    *A controlled failure*: `crash_demo.py --edit`, which kills a real process
    mid-write and shows the resume decide correctly. *The results*: the Session
    12 report.
  - Test: the whole demo runs with no API key — scripted model, real tools, real
    journal, real verifier — and a flag turns the first act live.
  - Review: a demo that needs a key and a working network is a demo that fails
    in front of an audience. Two thirds of this one already does not.

- [x] **Step 10 — The fresh-environment proof.**
  - Build: D153. A clean machine or a container with nothing cached: clone,
    build, run the demo, run the tests.
  - Test: the tests pass inside the image too — 460-odd of them, no key, no
    network, in a couple of seconds.
  - Review: this is the session's done-when. Everything else today is in
    service of this one run by someone who is not you.

> **Review checkpoint.** Hand the README to someone who has never seen the
> project and watch without helping. Every question they ask is a line you owe
> the document.

**Execution record, 2026-11-18.** `agent/`, `scripts/`, `tests/`, `evals/`
code and reports, and `docs/` are source; `fixtures/` is read-only input;
`.runtime/journals/` and workspace-scoped memory are durable state;
`.runtime/eval-workspaces/` and `playground/` are disposable; `.env` is a
runtime secret. The image contains only the source and read-only inputs. Its
size was 52,238,880 bytes and it ran as UID 10001. `.dockerignore` excludes
the secret, generated workspaces, journals and local state.

A no-cache build succeeded. In the fresh image, the documented fixture setup
and test command passed **656 tests**, with two Git-history tests skipped
because the image has no Git checkout. The offline three-act demo passed its
fixture verifier, named its journal and changed path, showed both crash
positions and recovery, and printed the Session 12 report. A first container
crashed after editing `container_recovery.txt` (exit 137); a replacement
container using the same two bind mounts rebuilt the journal and reported the
applied change without repeating it. On the host, both the edited file and
journal remained owned by the host user. The image was not tested on a second
machine and has no hash-locked transitive dependency set.

---

## C. The failure table

| What happens | Expected | Notes |
|---|---|---|
| `.env` is copied into the image | must be impossible | `.dockerignore` first, then verify the layers |
| a fresh clone runs the first README command | it works, or the README says to generate `playground/` first | today it does not exist |
| the container is replaced mid-run | the journal survives; `--resume` finishes it | this is why `runs/` is mounted |
| a bind-mounted workspace is root-owned afterwards | must not happen | D149 |
| Ollama on the host, agent in the container | documented, or declared unsupported | not silently broken |
| no API key and no local model | a legible error, immediately | Session 1's criterion, still |
| the demo is run offline | two of three acts work | D156 |
| `pip install` resolves to new versions | pinned, or the build is not reproducible | D154 |
| someone runs it against their real repository | the README told them what the boundary is and is not | D157 |
| the image is pulled by someone else | no key, no journals, no memory, no workspace of yours | D148 |

---

## D. Brainstorming space

**Scope decision taken before Phase 0:** nothing new in the agent. If a session
15 change requires touching `loop.py`, it is a session 16 and this is a
fifteen-session project. The exception is configuration — Step 2 — because
"where does state live" is a packaging question that happens to be answered in
code.

Things I haven't decided:

- D147 through D157. None are closed.
- Whether the image should contain `tests/` and `fixtures/`. Running the suite
  inside the image is the strongest possible fresh-environment proof and it
  makes the image bigger than the thing it ships.
- Whether to publish the image at all, or to document the build. Publishing
  means owning its supply chain.
- Whether the demo should be recorded. A recording is reproducible for the
  viewer and not for the runner, which is the opposite of what this project has
  valued for fifteen sessions.

Things that surprised me:

- _(fill in during Phases 1 and 3)_

Questions for review:

- The project has four kinds of directory and one `.gitignore` that treats them
  as two. Which of today's mount decisions should have been a repository layout
  decision in Session 1?
- If someone runs this against a real repository instead of `playground/`, which
  of the fifteen sessions' guarantees still hold and which quietly assumed a
  small, disposable, hostile-to-nobody workspace?
- The README documents what the agent does. Does anything document what it
  *refuses* to do, in a form a user can check rather than trust?

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
> **A resumed run uses its recorded workspace; independent runs never share a budget or journal.** (Session 14)
> **Memory may change what the agent does. It may never change what the agent is allowed to do.** (Session 14)
> **If it only works on the machine that built it, it does not work.** (new, proposed)

Fourteen of these constrain the agent. The last one constrains the project, and
it is the only one that cannot be tested by the person who wrote it. That is
also the argument for putting the whole list in the README: an invariant nobody
can find is a comment, and every one of them was earned by a failure this
project reproduced on purpose.

---

## E. Known limits of the finished thing

- **A container is not a security boundary.** It bounds the blast radius of a
  mistake. A real attacker with a task prompt and a shared filesystem is a
  different threat model and this project has never claimed to meet it.
- **Path checks are not a sandbox.** Session 2 said so, and shipping does not
  change it: a hostile process sharing the workspace defeats every check in
  `policy.py`.
- **The agent cannot run anything.** No command execution means no `pytest`,
  which means the agent never sees its own change fail — the evaluator does, and
  only afterwards. That is a deliberate scope decision and the largest single
  gap between this and a production coding agent.
- **One provider, one model family, one language.** Everything measured was
  measured there.
- **The eval suite is small and was written by the agent's author.** Twenty
  cases is a smoke test with a report attached.
- **Reproducible today is not reproducible next year.** Pinned dependencies do
  not pin a hosted model, and the provider can change the thing being measured
  without a commit on your side.

---

## F. Self-test, for the whole project

1. Someone clones the repository on a machine with Python and nothing else.
   What is the first command that fails, and does the README predict it?
2. The container is replaced while a run is editing a file. What is on disk,
   what is in the journal, and what does `--resume` do?
3. Name everything in the image that would be a leak if it were published.
4. A user asks: "is it safe to point this at my work repository?" Answer in
   three sentences, without overclaiming and without refusing to answer.
5. Which parts of the demo need a network, and what happens to the other parts
   when there is none?
6. Of the fifteen invariants, pick the three that are enforced by code and the
   three that are enforced only by a sentence somewhere. Are they the ones you
   would have guessed in Session 1?
7. What would you have to build before letting this agent run a test suite
   itself — and which of the fifteen sessions' decisions would that break?

And the one that has no next session to punish it:

8. The agent says it fixed the bug. The verifier agrees. The tests pass. Name
   every remaining way the change could still be wrong, and say which of them
   this project can detect, which it can only report, and which it cannot see
   at all. That gap — between *done* and *verified*, and between *verified* and
   *correct* — is the thing the whole course was about, and it does not close.
