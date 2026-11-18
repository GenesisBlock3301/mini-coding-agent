# Session 10 — A Run You Can Read After It Is Over

**Goal (from the plan):** add JSONL event traces.
**Done when:** you can inspect requests, tool calls, results, usage, and stop
reasons after a run.

Read that criterion against `runs/` and it already passes, which is the second
time this project has started a session by discovering the milestone was
delivered sideways. `agent/journal.py` writes exactly one JSON record per event,
fsynced, append-only, and `rebuild()` turns it back into a conversation. Every
item on the list is in there: `run_started` carries the task, the budget and the
workspace; `model_result` carries the message and the usage split;
`tool_intent` and `tool_outcome` bracket every dispatch; `model_retry` counts
attempts; `run_finished` carries the stop reason and the totals.

`agent/tracing.py` is five lines of docstring and nothing else.

So the session is not "write a JSONL logger". It is the harder question the
accident poses: **what is a trace that the journal is not?**

- **They have different obligations.** A journal record is a recovery protocol:
  written *before* the side effect it authorises, fsynced, never rewritten, and
  `rebuild()` depends on its exact shape. A trace is an observation: it may be
  buffered, it may be lost, and nothing breaks if it is. Merging them means
  every analytics field you ever add is fsynced in the hot path before every
  tool call.
- **The journal is missing things a trace needs.** `model_result` records usage
  and `finish_reason` and does *not* record `duration_ms`, the model id, or
  which tools were offered. Those are in `ModelResponse`, in RAM, and Session 8
  settled what happens to things that live in RAM.
- **A journal is private and a trace gets shared.** The journal holds every
  tool result in full — which means it holds the contents of every file the
  agent read. That is correct for replay and it is the wrong thing to paste
  into an issue. The plan says "secrets excluded"; the real risk here is not the
  API key, which never enters a record because it lives in a header, it is the
  workspace.
- **Session 11 is the actual customer.** Two sessions from now a runner has to
  score twenty tasks three times each and produce a table of success, latency,
  tool calls, tokens, cost and stop reason. Whatever you build today is what it
  reads. If it has to re-parse conversations to count tool calls, you built the
  wrong thing.

---

## A. Decisions to lock before writing code

| #   | Decision                                     | Options                                                                             | My pick + why |
|-----|----------------------------------------------|-------------------------------------------------------------------------------------|---------------|
| D89 | Trace versus journal                         | a second stream / more fields in the journal / a *reader* that derives traces from journals | **A reader over journals. `agent/tracing.py` writes nothing.** One source of truth, nothing new in the fsync hot path, and no second file that can disagree with the first. The cost is real and was paid in Phase 1: a reader can only show what was recorded, so the three missing fields had to go into the journal before this option existed. |
| D90 | What is missing and where it goes            | `duration_ms`, model id, tools offered — into `model_result` / into a new record / not recorded | **`duration_ms`, `model` and `tools` onto `model_result`.** Latency because the derivable number (model_result.at - model_pending.at) silently includes retry backoff, so a run that slept 8s reads as a slow model. The model id because a trace that cannot name the model cannot be compared with another. `tools` as names + a 12-char digest of the schemas: names answer "what could it call", the digest catches an edited description, which is a prompt change by another name. |
| D91 | Granularity                                  | one record per event (as now) / one row per step / both, one derived from the other  | **Both, and the row is derived from the events.** One record per event stays the storage shape; the summary row is computed by the reader. A stored row would be a second source of truth for the numbers (D89 again, one level down). |
| D92 | What a trace keeps of file content           | the full tool result / first N bytes / length + hash only                            | **Full in the journal, bounded in the reader, `--full` to opt back in** - and the header names the form, so a bounded trace is never mistaken for the whole record. Bounded keeps the tool name, the arguments, the byte count, the error code, `changed_path` and the first ~120 characters, which answers which file / how big / did it change. |
| D93 | Redaction                                    | a deny-list of patterns / structural (never log an object that could contain a key) / both | **Structural, and nothing else.** The key lives in an HTTP header, `model.py` never logs a request object wholesale, and none of the 132 journals contains it - asserted by a test rather than assumed. A deny-list over `sk-...` would be a worse version of the same guarantee: it implies the key might be there and fails on the first provider with another prefix. The boundary does the rest; what this module bounds is the WORKSPACE, which is the real risk. |
| D94 | Schema version                               | a `v` field on every record / one `schema` in `run_started` / none, and treat readers as tolerant | **One `schema` in `run_started`, plus a written-down absence table.** A version on every line is a field written 1,600 times to answer a question asked once. Journals without it are generation 0 - 30 of the 132 - and every absence keeps the meaning it already earned, listed in `tracing.ABSENCES` and printed by `--absences`. |
| D95 | File layout                                  | one file per run (as now) / one stream for all runs / per run plus an index          | **One file per run, as now; the index is derived.** `--list` reads the directory and summarises. An index file would be a cache that can be wrong about the only durable record of what an agent did to someone's files. |
| D96 | Retention                                    | never delete / a `--prune` / a size cap, and who runs it                             | **An explicit `scripts/prune_runs.py`, never automatic.** Default to keeping, dry run by default, and two refusals that are not configurable: never the most recent run (someone is about to `--resume` it) and never a run with no `run_finished` (that is precisely a resumable one). |
| D97 | The reader's shape                           | `python -m agent.trace <run_id>` / a `--trace` flag on the CLI / a library Session 11 imports | **A library with a `__main__`.** `python -m agent.tracing <run_id> [--full|--json|--absences|--list]` for a person; `load`/`summary`/`timeline` imported directly by Session 11, which must never shell out to read its own numbers. |
| D98 | Chat sessions                                | one trace per session (it journals per session, D56) / per turn / not traced         | **One trace per session, because `chat.py` journals per session (D56).** Turns are visible inside it as separate `run_finished` records, which is the same shape a resumed one-shot run already has - so the reader needed no special case. |
| D99 | What Session 11 consumes                     | the trace file / the `LoopResult` in process / both, and they must agree             | **Both, and a test asserts they agree.** `tests/test_tracing.py::test_the_row_agrees_with_the_loop_result` compares the row built from disk against the `LoopResult` built in RAM for the same run. They are derived from different places; if they ever disagree, one is lying and Session 11 is about to depend on the wrong one. |

**Hardest two: D89 and D92.**

D89 because both answers are defensible and the cost shows up later. One file
means a single source of truth about a run and a schema that `rebuild()` and
your analysis both depend on — every trace field becomes a replay concern. Two
files means they can disagree, and a run that recovered correctly while its
trace says otherwise is a debugging session nobody wins. The third option —
`tracing.py` as a *reader* over journals, writing nothing — is the cheapest, and
its weakness is precise: it can only ever show what the journal already
recorded, so D90 decides whether that option is even available.

D92 because this is the decision that makes a trace safe to attach to a bug
report, and it trades directly against the thing traces are for. Hash-and-length
is safe and useless when you are trying to see why the model picked the wrong
`old`. Full content is a copy of someone's source code in a file that gets
pasted into chat windows. The honest answer is probably "full in the journal,
bounded in the trace, and the reader tells you which you are looking at" — but
then D89 cannot be "derive it from the journal" without a redacting step in
between.

**And the sneaky one: D94.** The journals already on disk have no version field,
and at least three shapes exist in `runs/` right now: pre-Session-6 records with
no `changed_path`, pre-Session-8 records with no `before` fingerprint, and
today's. `rebuild()` tolerates all three *by accident* — `.get()` with a default
everywhere — and `agent/replay.py` turned one of those absences into a real
decision (`UNKNOWN`: no fingerprint, so refuse to replay). That worked because
absence was given a meaning. Do that deliberately or stop doing it, but do not
leave a third version to luck.

---

## B. Sequential build order

Everything here is offline. There is no live phase: a trace of a scripted run
and a trace of a real one differ only in the numbers, and the numbers are not
what you are debugging today.

### Phase 0 — Read what you already have

> **Both steps here were run as read-only subagents over `runs/` — 132 files,
> 1,601 records — because the question was "what is actually on disk", and
> that is a survey rather than a judgement.**

- [x] **Step 1 — Inventory one real journal.** _(the Session 8 live run,
  field by field. Findings below.)_
  - Build: nothing. Take the Session 8 live run and list every field present.
  - Test: for each item the plan names — requests, tool calls, results, usage,
    stop reasons — point at the record that holds it, or write "missing".
  - Review: this list is the specification. Anything you add beyond it is a
    feature you chose, not a milestone you completed.

  **What the inventory found.** All five of the plan's items are present, but
  two of them are not where you would look:

  | The plan's item | Where it actually is |
  |---|---|
  | requests | **no record holds one.** `model_pending` says only "step N is about to be sent"; the messages array is re-derivable only by folding `run_started.messages` + every `model_result.message` + every `tool_outcome.message` in order, which is exactly what `rebuild()` does |
  | tool calls | `tool_intent.name` / `.arguments` / `.call_id` |
  | results | `tool_outcome.message.content`, with `ok` / `error_code` / `changed_path` as the structured verdict |
  | usage | `model_result.usage` per request; `run_finished.totals` per **process** |
  | stop reasons | two different things: `model_result.finish_reason` per request, `run_finished.stop_reason` per run |

  Three more findings that changed the design:

  - **`elapsed_s` in `totals` is per PROCESS, not per run** (D39 scoped seconds
    to the process deliberately). The Session 8 live run reports 1.014s and
    1.73s across two processes for a run that spans 44 seconds. The reader uses
    the timestamp span, which is the only number that includes the time the run
    spent dead.
  - **`replayed` does not mark a resume.** It is true only when a
    pre-generated answer was replayed for free, so it reads `false` in BOTH
    `run_finished` records of the Session 8 resume. The honest signal is that
    there are two of them - one per process.
  - **No resume marker, no run id, no agent version, no CLI flags** anywhere.
    The crash is inferable only from a dangling `tool_intent` and a 32-second
    gap in `at`.

- [x] **Step 2 — Find the three schema generations.** _(there are **six**,
  plus a seventh state. See below - the premise of this step was wrong.)_
  - Build: a script, five lines, that groups records in `runs/` by which keys
    they carry.
  - Test: confirm the pre-Session-6 and pre-Session-8 shapes are both still
    readable by `rebuild()`.
  - Review: D94. Decide whether tolerance is a design or a habit.

  **The premise of this step was wrong, which is why it was worth running.**
  Not three generations: **15 distinct record shapes across 7 event types,
  forming 6 field-level generations plus a 7th observable state.**

  | | From | What appears |
  |---|---|---|
  | G1 | baseline | — |
  | G2 | 20261113 | `run_finished.replayed` |
  | G3 | 20261114 09:48 | `tool_outcome.changed_path` |
  | G4 | 20261114 11:26 | `run_started.workspace` |
  | G5 | 20261115 12:26 | `tool_intent.before` (+ the replay variant carrying `before`/`now`/`interrupted`) |
  | G6 | 20261116 10:27 | `tool_outcome.repeat`, `run_finished.repeats`, `run_finished.context` |
  | G7 | 20261116 10:43 | the `model_retry` event type appears for the first time |

  G3 and G4 are 98 minutes apart and would have been read as one generation by
  anyone reconstructing this from the session docs. Record-level cutovers are
  clean: no old-shape record appears after its successor's first appearance.

  **Integrity: no parse failures in any of the 132 files**, no torn tails, no
  empty files. Also found:

  - **`model_retry` has no branch in `rebuild()`.** The constant is defined and
    the event is written, but the `if/elif` chain never mentions it, so it
    falls through silently. Tolerated, contributes nothing - and nobody noticed
    because only one journal on disk has ever contained one.
  - **9 files have no `run_finished`** (crashed or abandoned), and **5 hold
    more than one `run_started`** - resumed runs appending to the same journal,
    where `rebuild()` overwrites `messages`/`task` each time, so those files
    are only replayable from the last one.
  - The load-bearing names - read by `rebuild()` or `replay.py`, therefore
    never renameable - are: `t`, `messages`, `task`, `message`, `usage`,
    `changed_path`, `refused`, `call_id`, `ok`, `name`, `arguments`, `before`,
    the four `usage.*` token counts, `tool_calls`/`content`/`id` inside a
    message, `path` inside an argument string, and the six `t` values
    themselves. Everything else written today - `at`, `step`, `finish_reason`,
    `budget`, `workspace`, `error_code`, `repeat`, `interrupted`, `now`,
    `cost_usd`, `totals`, `steps`, `stop_reason`, `replayed`, `repeats`,
    `context` and every `model_retry` field - is written and never read back by
    either module. **That list is the trace, and it was already most of the
    journal.**

### Phase 1 — The record the journal is missing

- [x] **Step 3 — Latency, model id, and the menu.** _(`duration_ms`, `model`
  and `tools{names,digest}` on `model_result`; `schema` on `run_started`.)_
  - Build: D90. `duration_ms` and `response.model` are in `ModelResponse` and
    nowhere on disk; "which tools were offered" is in the `tools` argument and
    is the single biggest confounder in Session 12 (`--tools` versus not is a
    different agent).
  - Test: a journal from a scripted run shows all three, and `rebuild()` is
    unchanged by their presence.
  - Review: adding fields to an append-only format is the safe direction.
    Renaming one is not. Say out loud which fields are now load-bearing for
    `rebuild()` — those are the ones you may never rename.

### Phase 2 — The reader

- [x] **Step 4 — A timeline a human reads.** _(`python -m agent.tracing
  <run_id>`; retries and interrupted verdicts have their own markers.)_
  - Build: D97. One line per event, aligned, with the step number, the elapsed
    offset, and what happened. `model_retry` and the interrupted-call verdicts
    from Session 8 must be visible without `grep`.
  - Test: run it against a crashed journal, a resumed journal, and a journal
    that `heal()` truncated. None may raise.
  - Review: a reader that crashes on a damaged file is a reader you cannot use
    on the only file you ever urgently need to read.

- [x] **Step 5 — A row a machine reads.** _(`--json`, asserted against
  `LoopResult` for the same run.)_
  - Build: one JSON object summarising a run: ids, task, workspace, stop
    reason, steps, tool calls, tokens in/out/cached, spend, wall clock, model
    latency, changed paths, repeats (Session 9), retries, malformed ids.
  - Test: the numbers match `LoopResult` exactly for a run you also have in
    memory. D99 — if they disagree, one of them is lying and you need to know
    which before Session 11 depends on it.
  - Review: this object is the eval suite's row. Design it as a table column
    set, not as a dump.

### Phase 3 — Safe to share

- [x] **Step 6 — Redaction that is structural first.** _(nothing built but a
  test: the key cannot reach a record. See the limit below.)_
  - Build: D93. The key is already safe by construction — it lives in a header
    and no request object is logged wholesale. Keep it that way and write the
    reason down, because a deny-list regex over `sk-...` is a worse version of
    the same guarantee.
  - Test: a workspace file containing a plausible key, read by the agent, and a
    `.env` placed where `read_file` would refuse it anyway.
  - Review: the boundary is doing most of this work already. Name which part is
    the boundary's and which part is the trace's.

- [x] **Step 7 — Bound what a shared trace carries.** _(bounded by default in
  the reader, `--full` opts in, the header names the form.)_
  - Build: D92, as a flag on the reader rather than on the writer: the journal
    keeps everything (replay needs it), the shared artefact is produced by the
    reader.
  - Test: the bounded form still answers "which file, how big, did it change".
  - Review: if the bounded form cannot answer the question you most often ask a
    trace, the bound is in the wrong place.

> **Review checkpoint.** Full suite green. Then take a journal from Session 8,
> run the reader over it, and answer Session 8's self-test #8 from the output
> alone. If you cannot tell from the trace whether the edit landed, the trace is
> missing the fingerprint that the journal has had since D73.

**Done.** `python -m agent.tracing 20261115T123059-c30806`, the Session 8 live
run, killed between `os.replace` and the outcome record:

```
  5.29s  step  4     tool    replace_text({"path": "stats.py", "old": " return
                             values[-n + 1:]", ...})  before=sha256:fb9268f2b480…
  8.89s  step  4     ok   changed=stats.py  INTERRUPTED=changed (not repeated)
                             [236 chars] This call was interrupted by a crash, and
                             stats.py has changed since it started: the edit is…
  8.89s  step  5  -> POST
  9.90s  step  5     tool    read_file({"path": "stats.py"})
```

Self-test #8 asked which signals survive a kill between the tool call and the
tool result. The trace answers it in three lines without the journal being
re-read by hand: the fingerprint was recorded with the intent, the file had
moved by the time the resume looked, the edit was treated as applied and not
repeated, and the model's next act was to read the file rather than edit it
again. `changed_paths` - the signal Session 8 proved dies with the process -
is reconstructed here from `tool_outcome.changed_path`, which is the only
reason it can be.

The 3.6 seconds between the intent and its outcome are not tool time. They are
the crash and the restart, and nothing in the record says so - see the limits
below.

### Phase 4 — Retention

- [x] **Step 8 — Decide what deletes runs.** _(`scripts/prune_runs.py`: dry
  run by default, two refusals no flag can widen.)_
  - Build: D96. `runs/` grows forever and `*.jsonl` is gitignored, so this is
    invisible until a disk is.
  - Test: a prune that refuses to delete the most recent run, because that is
    the one someone is about to `--resume`.
  - Review: deleting a journal destroys the only durable record of what an
    agent did to someone's files. Default to keeping, make deletion explicit,
    and never prune automatically on startup.

  **Built as `scripts/prune_runs.py`.** Dry run is the default and `--apply`
  is the consent. Two refusals come first in an ordered branch chain and take
  no arguments, so no flag - and no flag added later - can widen past them:
  the newest run (someone is about to `--resume` it) and any run with no
  `run_finished` (that is precisely a resumable one). Two filters are an AND,
  never an OR: a run goes only if every rule the human named agrees.

  On this project's own `runs/` with `--older-than 7 --keep 60`: 132 journals,
  1 kept as newest, **9 kept as unfinished** - exactly the 9 the Phase 0 survey
  found - 4 deletable, 10.2K.

  Two details worth keeping:

  - **`run_finished` is detected by parsing each record, never by substring.**
    A journal quotes the full text of every file the agent read, so a workspace
    file containing the words `run_finished` would make a grep call an
    unfinished run finished - the one mistake this script must not make.
  - **Age comes from the journal's own `run_started.at`, not from the
    filename and never from mtime.** A resume appends and a restore rewrites
    mtime, so mtime answers "last touched", which is a different question.

  **And the finding that outlives this script: run ids are LOCAL time, not
  UTC.** `new_run_id()` calls `time.strftime()` with no timezone argument.
  Every document in this project - including the brief that commissioned this
  step - has read those ids as UTC, and on this machine they are six hours
  off. Nothing is broken by it today, because lexical order still equals
  chronological order within one timezone, and the one place it would have
  mattered (`--older-than`) reads the epoch from the record instead.

  It is latent, not harmless:

  - a DST fall-back repeats an hour, and the lexical sort inverts inside it -
    so "newest" would be wrong for one hour a year in any zone that observes
    DST (this one does not)
  - two machines in different zones produce ids that interleave wrongly, which
    independent local runs and any shared `runs/` directory would hit
  - a reader comparing an id to a UTC timestamp from anywhere else is simply
    wrong, silently

  **Deliberately not fixed here.** Switching to UTC would make new ids sort
  BEFORE six hours of existing ones, which breaks the only property the
  docstring claims (`ls runs/` is chronological) exactly when someone is
  looking for their most recent run. The fix belongs with a migration or with
  Session 13, and the decision is recorded rather than taken quietly.

---

## C. The failure table

| What happens | Expected | Notes |
|---|---|---|
| the process dies mid-record | the reader skips a torn tail | `heal()` + `events()` already do this |
| a record from an older schema | read, with absence given a meaning | D94; `replay.UNKNOWN` is the precedent |
| a trace is pasted into an issue | no key, and no more file content than D92 allows | the key is safe structurally |
| the reader meets an unknown event type | printed as unknown, never fatal | forward compatibility costs one branch |
| a journal with no `run_finished` | the reader says "unfinished", not "0 steps" | a crashed run is the normal case here |
| two runs with the same id | impossible by construction | timestamp + 6 random hex |
| a run resumed three times | one timeline, not three | the journal is appended to, not replaced |
| a chat session with 40 turns | D98 — one session, many turns | `chat.py` journals per session (D56) |
| `runs/` with 10,000 files | the reader still starts | an index, or accept the glob - drafted in section G |

---

## D. Brainstorming space

**Scope decision taken before Phase 0:** no new storage engine, no SQLite, no
OpenTelemetry. The plan says JSONL and the project already proves JSONL is
enough: append-only is what made crash recovery possible at all, and a format
you can `tail -f` and `jq` is a format you will actually look at. The moment to
revisit that is Session 12, if comparing twenty tasks × three runs turns out to
be painful with files. **That revisit is now drafted, unbuilt, in
section G** - written while this session's measurements were fresh rather than
in the middle of the experiment that needs it.

Decided during the build:

- D89 through D99, all closed above.
- **`tracing.py` keeps its name and is a reader.** The name promises a noun
  and delivers a verb, which is mildly wrong and cheaper than renaming a file
  the plan names. Written in the module docstring so the next reader is not
  misled by the filename.
- **The git sha is still not recorded, and that is now a known gap** rather
  than an open question. Session 12 must record the code version; `run_started`
  is the only record written before anything can go wrong; nothing writes it
  today. Logged in section E.

Things that surprised me:

- **The step called "find the three schema generations" found six.** G3 and G4
  are 98 minutes apart and would have read as one to anyone reconstructing the
  history from these documents. The lesson is the one Session 8 already taught
  with the fault matrix: survey the artefacts, do not reason about them. Both
  Phase 0 steps were run as read-only subagents over all 132 journals for
  exactly that reason.
- **`model_retry` is written and never read.** It has been defined in
  `journal.py` since Session 8 and `rebuild()` has no branch for it. Nobody
  noticed because only one journal on disk has ever contained one - the
  feature and its first real use were four days apart.
- **Most of the journal was already a trace.** The load-bearing set that
  `rebuild()` and `replay.py` actually read is about a dozen names. Everything
  else - `at`, `step`, `finish_reason`, `budget`, `workspace`, `cost_usd`,
  `totals`, `stop_reason`, `repeats`, `context` - is written and never read
  back by either. The split between the two obligations was already there; it
  had just never been named.
- **Run ids were never UTC, and four documents said they were.** The prune
  script needed an age, went looking for one, and found that the thing
  everyone had been reading as a UTC timestamp is `time.strftime()` with no
  timezone - local, by six hours here. Three separate passes over `runs/` in
  this session repeated the assumption before anything tested it.
- **`replayed` does not mean what its name says.** Both `run_finished` records
  of the Session 8 resume say `false`. A field whose name reads as "this was a
  resume" and whose meaning is "a pre-generated answer was replayed for free"
  is the kind of thing a reader inherits silently - and the reader very nearly
  reported it.

Questions for review:

- The journal is written before each side effect because `rebuild()` needs it.
  Does any trace field need that guarantee? If none does, that is the argument
  for two streams; if one does, it belongs in the journal. **Answered by
  building it: none of the three new fields needs it.** `duration_ms`, `model`
  and `tools` are all written in `model_result`, which already lands before any
  tool runs - they got the guarantee for free by riding an existing record,
  which is why D90 put them there rather than in a record of their own.
- A trace that is bounded for sharing and a journal that is complete for replay
  will disagree in public. Which one is "the record" when someone quotes the
  wrong one back at you?
- Session 8 made `changed_path` load-bearing for a resumed run's report. Is
  there anything else in the journal that a reader would be wrong to treat as
  cosmetic? **Yes: `before` on `tool_intent`.** It looks like a debugging aid
  and it is the whole of D72 - without it an interrupted edit is UNPROVABLE
  rather than decidable. The audit's load-bearing list is now written down in
  Phase 0 above, so the next person does not have to find out by renaming one.

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
> **A trace records what was done, not a copy of what was read.** (new, proposed)

The proposed invariant is the one that makes a trace shareable, and it is in
direct tension with the journal, which *must* keep a copy of what was read or a
resumed run cannot reconstruct the conversation. That tension is the session:
two artefacts, one complete and private, one bounded and public, derived in one
direction only — never the reverse, because you cannot un-publish a file.

---

## E. Known limits of this observability

- **Bounded is not redacted.** D92 caps what a shared trace carries; it does
  not know what is in it. A workspace file whose FIRST line is a credential is
  a credential in the bounded form. The guarantee this module actually makes
  is about the API key (structural, tested) and about VOLUME - it is the
  difference between leaking a line and leaking a repository. Treat any trace
  of a real workspace as workspace-confidential.
- **`model_retry` is written and never folded.** `rebuild()` has no branch for
  it, so a resumed run does not know how many requests the dead process sent.
  The reader counts them; crash recovery still cannot.
- **A resume leaves no marker.** Two `run_finished` records is the only signal,
  and it is inference rather than a record. Nothing stores a run id, an agent
  version, a git sha or the CLI flags - so two traces from different builds of
  this project are indistinguishable, which is a problem Session 12 owns and
  Session 11 will feel first.
- **Tool duration does not exist.** The gap between `tool_intent.at` and
  `tool_outcome.at` is the only proxy, and on the Session 8 live run it reads
  3.6s for an edit that took microseconds - the rest is the crash and the
  restart.
- **Run ids are local wall clock.** See step 8. Sortable within one machine
  and one timezone, wrong everywhere else, and silently so. One rule already
  depends on that sort: `prune_runs.py` picks the newest run - the one it must
  never delete - by lexical filename. Correct here, quiet when it is not; see
  G.6.
- **The timeline is derived, so it inherits every absence.** A generation-0
  journal prints `?ms` and no model name forever. That is the price of D89 and
  it was the right price, but it means old runs can never be compared with new
  ones on latency.

- **A trace shows actions, not reasoning.** The plan says so and it is worth
  repeating: `finish_reason` and a tool call are observable; why the model
  chose that `old` string is not, and no amount of logging will make it so.
- **Latency is measured on this side of the wire.** `duration_ms` includes
  network time, the provider's queue and your own TLS handshake, and nothing in
  it separates "the model was slow" from "your connection was".
- **Token counts are the provider's claims.** They are what you are billed on
  and they are not independently verifiable here.
- **A bounded trace cannot debug a matching failure.** The one bug class this
  project hits most — an `old` that does not match — needs the exact bytes, and
  the exact bytes are what D92 bounds away.
- **Nothing here is a metrics system.** One file per run answers "what happened
  in this run". It does not answer "has the failure rate changed this week";
  that is Session 12's report, computed from many of these files by hand.

---

## F. Self-test before moving to Session 11

1. Open a journal from a crashed run. Can you tell, from the file alone,
   whether the file on disk was edited? Which record and which field?
2. Your trace says a run used 18,422 tokens. Name every place that number could
   have come from and say which one you trusted.
3. A record arrives with an event type your reader has never seen. What
   happens, and what would have happened in the version you wrote first?
4. Which fields in the journal may you never rename, and why is the answer
   "because `rebuild()` reads them" rather than "because the reader prints
   them"?
5. Someone pastes a trace into a public issue. What is the worst thing in it,
   and which decision bounded it?
6. A run was resumed twice. How many `run_started` records does its journal
   hold, and how many `run_finished`?
7. The eval runner needs "tool calls per task". Does it count records, read a
   summary row, or ask `LoopResult`? What happens when those three disagree?

And the one Session 11 will punish a wrong answer to:

8. Session 11 runs twenty tasks three times each: sixty runs, sixty journals.
   What identifies a *task* across its three runs, what identifies a *variant*
   across sixty, and is either of those things in the file today — or are you
   about to recover them from filenames?

---

## G. Deferred: the SQLite index, and the trigger that opens it

Section D closed this session with *no new storage engine, no SQLite*, and
named the condition for reopening it: the moment comparing twenty tasks x three
runs turns out to be painful with files. This is the draft to execute at that
moment. It is written now, unbuilt, for the reason Phase 0 exists: the
measurements are on disk today, and a storage decision taken in the middle of
an experiment is taken under deadline.

**Nothing here is built. No file in `agent/` or `scripts/` is affected.**

### G.1 What `runs/` actually measures (2026-11-16)

```
132 journals        595 KB        12.2 runs/day, 55 KB/day over 11 days
46 workspaces       488 KB        regenerable: scripts/fixture.py build
                  ----------
runs/ total         1.3 MB

record          count      KB   share
model_result      359     180     30%
run_started       140     142     24%    <- 76 copies of one identical prompt
tool_outcome      285     132     22%
tool_intent       306      70     12%
run_finished      147      49      8%
model_pending     362      21      4%
model_retry         2       0      0%

gzip over all journals: 595 KB -> 182 KB (3.3x, measured)
```

At 55 KB/day this is not a disk problem and will not become one: twenty years
of it is 400 MB. **The pressure is not bytes, it is files and parsing.**
Session 11 runs twenty tasks three times - sixty journals in an afternoon,
five days of today's rate - and interactive use adds more. Every
question asked across runs is then a glob plus a full parse of every record to
answer one `GROUP BY`. `scripts/repeat_audit.py` already does exactly that
over 132 files, and it is the shape of every audit that follows it.

### G.2 The rule that makes this compatible with D89 and D95

D95 rejected an index file because it would be *a cache that can be wrong about
the only durable record of what an agent did to someone's files*. That
objection is correct and it is not an argument against a database - it is the
specification for one:

> **The log is the source of truth. The database is a view, and no decision
> that must be correct may read it.**

Three invariants carry that:

1. **No recovery path touches the DB.** `rebuild()`, `replay.plans_for()`,
   `--resume`, `heal()` and `prune_runs.py`'s two refusals keep reading files.
   The DB answers questions about runs; the journal answers what happened in
   one.
2. **`reindex` rebuilds everything from the journals.** Corrupt the DB, delete
   it, change its schema mid-session: `scripts/runs.py reindex` restores it in
   one pass and nothing is lost. That is what makes the schema cheap to get
   wrong, which matters because Session 12 will want a column this draft did
   not think of.
3. **A row is asserted against the reader, in D99's shape.** The existing test
   compares `tracing`'s row against `LoopResult` for the same run; the DB row
   joins that assertion. Three derivations of one number, one test, and if they
   disagree the test names which.

### G.3 Decisions to lock before writing code

| #    | Decision                        | Options                                                                 | Draft pick + why |
|------|---------------------------------|-------------------------------------------------------------------------|------------------|
| D158 | What the database IS            | source of truth / a rebuildable cache / the live write path              | **A rebuildable cache**, by G.2. The live write path stays append-only JSONL: one fsync per record is the simplest durable write there is, and Sessions 5, 6 and 8 - `heal()`, `crash_demo.py`, `fault_matrix.py`, the `before` fingerprint - are all built on a file that survives `kill -9` half-written. SQLite rolls the half-transaction back, and the rolled-back step is precisely the one `replay.py` needed to see. |
| D159 | What writes it, and when        | the loop at `run_finished` / `prune_runs.py` / an explicit `runs.py index` | **An explicit command, never the loop.** D89's argument one level down: nothing new goes in the fsync hot path, and an indexer that fails, locks or slows cannot take a run with it. The CLI may *offer* to index after a run; it may never require it. |
| D160 | The grain                       | runs only / runs + events / runs + events + messages                     | **`runs` now, `events` when a query needs one, `messages` never.** Messages are the private half (section E): a table of them is a second copy of every file the agent has ever read, with no `--full` boundary in front of it and no `heal()` to bound the damage. The journal already holds them, under D92's rules. |
| D161 | Where the bytes live            | journals stay as files / `journal_gz` BLOB in the DB / both, for a window | **Both for a window, then the file may be pruned.** gzip is 3.3x measured. The archive is written only after `export` reproduces the original byte-for-byte, and `prune_runs.py`'s two refusals still come first and still take no arguments. A journal that is unfinished or newest is not archivable, deletable, or anything else. |
| D162 | What identifies a run's siblings | the run id / task + variant columns / the filename                       | **Columns, written by the runner, never parsed from a filename.** This is self-test #8 of this session, due in Session 11: `task_id`, `variant`, `git_sha`, `provider`, `model`, `tools_digest` (D90 already records the last two). A suite that recovers its own grouping from filenames has made the filename a schema. |
| D163 | How time is stored              | the run id string / `run_started.at` / both                              | **The epoch from the record, plus the id as an opaque string. `started_at REAL` is the time; the id is a name. Never `ORDER BY id`.** And this is not a workaround for Step 8's finding, which matters because the two read identically in a schema and differently in six months - see below. |

**D163 is independent of the run-id bug, and that is the point.**

Step 8 found that `new_run_id()` is local wall clock. The tempting reading of
D163 is "ids are broken, so sort by something else" - a workaround, which
would mean the index inherits a dependency on someone fixing them. It does
not, and the honest statement is stronger: **ordering by an id would be wrong
even if the ids were perfect UTC.** A name is not a clock. Two machines drift,
a restored backup carries its old name, and `uuid4` decides the tie. Sorting
records by their filenames is a habit from single-process scripts, and this
project stops being one in Session 13.

So the disposition of the underlying bug, stated rather than implied:

- **The index does not need it fixed.** No decision in G depends on id
  ordering, and none may be added that does. If `ORDER BY id` ever appears in
  a query, that is the bug, not the id.
- **Whether to fix `new_run_id()` is Session 13's call**, for the reason step 8
  gave: switching to UTC makes new ids sort *before* six hours of existing
  ones, which breaks `ls runs/` being chronological exactly when someone is
  hunting for their most recent run. It needs a migration or a prefix change,
  and it should be decided by the session that actually runs on two machines.
- **This section is not a reason to do it.** An index that would be fixed by
  renaming files is an index built on filenames, which D162 already refused.

### G.4 Schema sketch

```sql
CREATE TABLE runs (              -- one row per run, from run_started + run_finished
  id            TEXT PRIMARY KEY,
  started_at    REAL,            -- D163: the epoch in the record, not the id
  schema        INT,             -- D94's generation, or NULL for generation 0
  task          TEXT,
  workspace     TEXT,
  task_id       TEXT,            -- D162: written by the runner, never parsed
  variant       TEXT,
  git_sha       TEXT,            -- section E's known gap; a column waiting for it
  provider      TEXT, model TEXT, tools_digest TEXT,
  stop_reason   TEXT, exit_code INT,
  steps INT, tool_calls INT, retries INT, resumes INT,
  tokens_in INT, tokens_out INT, tokens_cached INT, cost_usd REAL,
  wall_s REAL, model_ms REAL,
  repeats_longest INT, repetition_stopped_on TEXT,
  changed_paths TEXT,            -- JSON array; the claim lives in the journal
  finished      INT,             -- 0 = no run_finished = resumable = untouchable
  journal_gz    BLOB             -- D161, only after a verified round trip
);

CREATE TABLE events (            -- D160: only once a query needs it
  run_id TEXT, seq INT, t TEXT, step INT, at REAL,
  name TEXT, call_id TEXT, ok INT, error_code TEXT, arguments TEXT,
  PRIMARY KEY (run_id, seq)
);
CREATE INDEX events_tool ON events(t, name, error_code);
```

What that buys, in the project's own terms:

- `repeat_audit.py`'s 91-journal replay becomes one `GROUP BY name, arguments`.
  Session 9 decided its thresholds that way, and the next rule will want the
  same evidence over more runs.
- Session 12's two arms become `WHERE variant = ? GROUP BY stop_reason` instead
  of a bespoke script per comparison.
- `model_retry` - written since Session 8, read by nothing (section E) -
  becomes queryable without `rebuild()` ever learning a branch for it.

### G.5 Build order, when the trigger fires

1. `runs.py index --dry-run` prints the rows it would write, writes nothing.
2. `reindex` over every journal in `runs/`, then the D99-shaped test: the row
   from the DB equals `tracing.summary()` for the same run, for all of them.
3. Port one real consumer - `repeat_audit.py` - and keep the file-based one
   until both agree on this project's own history.
4. `events`, only when a query actually needs it.
5. `journal_gz` + `export`, verified byte-for-byte, and only then may
   `prune_runs.py` consider an archived journal deletable.

### G.6 What this does not fix

- **The 76 identical system prompts** (67 KB, 11% of all bytes) are a journal-
  side change: `system_sha` in `run_started` and the text once under
  `runs/prompts/<sha>.txt`. Cheaper than the index, independent of it, and it
  turns "which prompt was this run under?" from an eyeball diff into a field -
  which is what Session 12's comparison needs anyway.
- **`runs/workspaces/`** - 46 directories, 488 KB, 45% of the tree - is
  regenerable scratch that no database touches. Anything that reclaims space
  should start there, because those are the only bytes in `runs/` with no
  evidentiary value.
- **`prune_runs.py`'s `newest` refusal is an `ORDER BY id` in disguise**, and
  it is in shipped code rather than in this sketch. The script ranks journals
  by lexical filename to decide which run is newest and which fall inside
  `--keep N`. On one machine in a zone with no DST - this one - lexical order
  is chronological order and the rule is exactly right. It stops being right
  in two places, both of which arrive with the same sessions this section
  does: a DST fall-back repeats an hour and inverts the sort inside it, and a
  shared or copied `runs/` interleaves two machines' ids. The failure is quiet
  and lands on the one rule that must not be quiet - the newest run is the one
  someone is about to `--resume`, and protecting the wrong file protects
  nothing.

  The fix is small and is NOT taken here: the script already parses
  `run_started.at` out of every journal for `--older-than`, so ranking by that
  epoch costs one sort key and removes the dependency entirely. It is left
  alone because it changes what `--keep N` counts - the tests pin the current
  lexical ranking deliberately - and because it should land with whatever
  Session 13 decides about the ids, as one change with one test, rather than
  as a drive-by inside a deferred design note.
- **Concurrency.** Session 13's parallel runs keep writing their own files;
  the DB is written afterwards, by one process. That is the only reason SQLite
  writer contention never becomes this project's problem - and the reason to
  re-open D158 would be wanting the live path in the DB, which is a Session 13
  decision with a Session 13 test, not this one.
