# Session 6 — One Edit, Exactly One Match

**Goal (from the plan):** add `replace_text`; require exactly one match.
**Done when:** zero or multiple matches return an error without changing the file.

Read the completion criterion literally and it is four lines of code: count the
occurrences, refuse unless the count is 1, write the result. You could have it
passing in twenty minutes.

Everything that makes this session hard is in the words *without changing the file*.

Until now every tool was read-only. A wrong `list_files` cost tokens. A wrong
`read_file` cost tokens. Every invariant you wrote was cheap to hold because nothing
you did could be observed after the process exited. From this session on, a wrong
answer costs the user's file — and three pieces of machinery you already built were
designed on the assumption that no tool has side effects:

- **`dispatch` validates exactly one argument, and it is called `path`.** Look at
  the `unexpected = set(parsed) - {"path"}` line and the `parsed.get("path")` below
  it in `agent/tools.py`. `replace_text` needs three arguments. Decide how the
  validator generalises *before* you write the tool, or you will write it twice.
- **`ModelError.safe_to_retry` has been free so far.** Re-reading a file after a
  timeout costs nothing. Re-applying an edit after a timeout is a second edit.
  Session 1 gave you two axes for exactly this moment.
- **A `tool_intent` with no `tool_outcome` in the journal currently means "a read
  may not have happened".** After this session it means "a file may be half
  written" — unless you make that impossible.

And one question you have already written down and not yet answered: Session 5's
self-test ends with *"if a batch contains two edits to the same file and the second
is refused by a budget, what state is that file in — and does my trace say so?"*
That question is now due.

---

## A. Decisions to lock before writing code

| #   | Decision                                   | Options                                                           | My pick + why |
|-----|--------------------------------------------|-------------------------------------------------------------------|---------------|
| D41 | How `dispatch` validates arguments         | keep the hardcoded `path` / a per-tool spec / **derive from the schema** | **Derive from `TOOL_SCHEMAS`.** The schemas already carry `required` and `additionalProperties: False`, so a separate validator would be copying information that is already written down — and the day the two disagree, the tool rejects an argument the model was *told* to send. Cost is a small schema walker over the subset I actually use (`type: object`, `properties`, `required`, string types); anything outside that subset fails at import, not at call time. |
| D42 | How the new bytes land on disk             | truncate in place / temp file + atomic rename / write beside, then swap |               |
| D43 | What `old` has to match                    | exact bytes / whitespace-insensitive / fuzzy with a threshold      |               |
| D44 | Staleness: must the model have read it first | no check / re-read at write time / model sends a hash it must match |               |
| D45 | How occurrences are counted                | `str.count` (non-overlapping) / scan for overlapping / first-only  |               |
| D46 | A file larger than `MAX_READ_BYTES`        | edit it anyway / refuse the edit / refuse unless the match is inside what was shown |               |
| D47 | What a successful result tells the model   | "ok" / the match count / the changed line / a diff                 |               |
| D48 | `old` empty, or `old == new`               | reject both / allow the no-op / reject only the empty one          |               |
| D49 | May `replace_text` create a file           | yes, if missing / no, edits only                                   |               |
| D50 | May it edit the same file twice in one batch | yes / refuse the second / yes but report it                        | **Still open — but no longer a feature.** Session 5's `run_pending` already executes every call in a response, so two `replace_text` calls to one file arrive together whether or not I build anything. What is left to decide is ordering and reporting, not capability. Leaning: run them in order, each matched against the file *as it stands*, both ids answered, and D52 names the file once. |
| D51 | What the journal records about a write     | same fields as a read / + byte counts / + a hash before and after  |               |
| D52 | What the CLI prints after a run that edited | nothing new / the files touched / the files touched on every stop  |               |

**Hardest two: D42 and D44.**

D42 because "it worked when I ran it" and "it is correct" diverge here for the first
time. Truncating a file and writing new bytes into it is two operations with a
window between them, and a crash in that window leaves a file that is neither the
old one nor the new one. The temp-file-plus-rename shape closes the window because
`os.replace` is atomic — but only *within one filesystem*, which is why the temp
file has to be created in the target's own directory and not in `/tmp`. Get this
wrong and it still passes every test you are likely to write, because your tests do
not crash mid-write. Session 5's `crash_demo.py` is the tool you already own for
proving it.

D44 because the agent's edit is built on a file it read some seconds ago, and
nothing has held that file still in the meantime. The plan is explicit: *verify
expected content or a file hash before applying an edit; reject stale edits rather
than applying them blindly.* The cheap version of this is almost free — you are
re-reading the file at write time anyway, so the question is only whether you compare
it to anything. The expensive version asks the model to carry a hash. Decide which,
and write down what each one does **not** protect you from.

**And the sneaky one: D46.** `read_file` truncates at 64KB and tells the model so.
If the model then sends an edit for a 200KB file, it is editing a file it has only
seen the first third of. The match may be unique in what it saw and ambiguous in
what it did not — and your count, run over the whole file, will disagree with the
model's belief for a reason the model cannot see. There is no wrong answer here, but
there is a wrong non-answer.

---

## B. Sequential build order

Phases 0 through 3 are testable with **no API key and no network**. Nothing in this
session needs a live model until the last step.

### Phase 0 — The argument gate

- [ ] **Step 1 — Make `dispatch` take more than one argument.**
  - Build: D41. A tool's arguments are described in exactly one place today — the
    JSON Schema in `TOOL_SCHEMAS`. Decide whether that place is now the source of
    truth for validation too, or whether the schema (for the model) and the
    validator (for you) stay separate on purpose.
  - Test: `list_files` and `read_file` behave *identically* to before — same error
    codes, same messages. Then: a `replace_text` call missing `new`, one with `old`
    as a number, one with an argument you never defined.
  - Review: whichever you choose, the error message must name the argument that is
    wrong. "Invalid arguments" sends the model guessing; "replace_text needs a
    string 'old'" is a correction it can act on. That was Session 4's rule for
    `unknown_tool` and it has not changed.

### Phase 1 — The match rule, with no filesystem at all

- [ ] **Step 2 — Counting, as a pure function.**
  - Build: text in, `old` in, `new` in; out comes either the new text or a refusal
    and a count. No `open()` anywhere in this step.
  - Test: zero matches, one match, three matches. Assert the *count* is in the
    refusal message for the three-match case.
  - Review: the zero-match message and the multi-match message are different
    instructions. Zero means "you are looking at text that is not there — read the
    file again". Multiple means "your anchor is too short — include the surrounding
    lines". A model given the same sentence for both will retry the same way twice.

- [ ] **Step 3 — The degenerate inputs.**
  - Build: D45 and D48.
  - Test: `old` empty. `old == new`. `old` longer than the file. And the one that
    decides D45 for you: `old="aa"` against a file containing `"aaa"`. Is that one
    match or two? `str.count` says one, a scan for overlapping occurrences says two,
    and your users will have opinions.
  - Review: an empty `old` matches at every position in every file. Whatever
    reasoning you use to reject it, make sure it rejects *before* anything is read
    from disk — the same ordering rule as Session 2 step 3.

### Phase 2 — The write

- [ ] **Step 4 — Read, match, write — reusing the boundary unchanged.**
  - Build: resolve the path with `resolve_in_workspace`, exactly as `read_file`
    does. Nothing new in `policy.py`.
  - Test: the Session 2 boundary cases all over again, against the new tool —
    outside the workspace, absolute, through a symlink, null byte.
  - Review: if you needed even one new line in `policy.py` to make a *write* safe,
    then Session 2's boundary was in the wrong place. This is the same check Session
    3 passed, under more load: a path does not care which tool asked, but a symlink
    you merely read is a different proposition from one you write through.

- [ ] **Step 5 — The write must not half-land.**
  - Build: D42.
  - Test: the ordinary path first — bytes on disk afterwards are exactly what you
    computed. Then the interesting one: force a failure between writing the new
    bytes and making them visible, and assert the original file is byte-identical.
  - Review: name the window you closed and the window you did not. Atomic rename
    protects the file's *contents*; the directory entry itself is only durable after
    the directory is synced. State the bound you are claiming — Session 5 taught you
    to say "at most one response" rather than "never", and this is the same habit.

- [ ] **Step 6 — Bytes in, bytes out.**
  - Build: carry D19 forward. `read_file` opens in binary and decodes by hand
    precisely so a CRLF file is not silently rewritten. The write side has to be the
    mirror image, or the first file your agent edits on a Windows-authored repo
    comes back with every line ending changed and a diff nobody can review.
  - Test: a CRLF fixture, a file with no trailing newline, a file with a BOM, and a
    UTF-8 file with non-ASCII text. In each case the bytes outside the replaced
    region must be unchanged.
  - Review: diff the *bytes*, not the decoded strings. A test that compares decoded
    text cannot see the bug this step exists to prevent.

- [ ] **Step 7 — Stale edits.**
  - Build: D44.
  - Test: read a file, change it on disk behind the agent's back, then apply the
    edit. Assert whatever you decided — a refusal, or a documented decision to
    overwrite.
  - Review: write down the race you are still exposed to. Re-reading at write time
    narrows the window to microseconds; it does not close it, and the plan already
    warns that path checks are not a sandbox against hostile concurrent changes.
    A narrowed window you can describe beats a closed one you cannot.

### Phase 3 — The batch, the journal, and the report

- [ ] **Step 8 — Two edits in one batch.**
  - Build: D50. Nothing in `loop.py` should need to change — `run_pending` already
    runs calls in order and answers every id. Confirm that, do not assume it.
  - Test: two edits to the same file in one response, where the second depends on
    the first having landed. Then the same pair in the opposite order.
  - Review: Session 5 step 3 said ordering was "academic for read-only tools, write
    it down anyway". This is the step that collects on it.

- [ ] **Step 9 — What the journal now means.**
  - Build: D51. `_dispatch` writes `tool_intent` before the call and `tool_outcome`
    after. The record shape does not change; what it *means* does.
  - Test: kill a run between intent and outcome on an edit (crash_demo already does
    this for model calls — extend it), then read the journal and answer: did the
    file change?
  - Review: if the answer is "I cannot tell from the journal", you have found either
    a missing field or a missing atomicity guarantee. Decide which one you are
    fixing. Recording a hash makes the question answerable; atomicity makes it
    unnecessary. Both is defensible, neither is not.

- [ ] **Step 10 — Report what was changed.**
  - Build: D52. The plan's rule for limits is *"stop explicitly when a limit is
    reached, and report changes already made"*. Until today there were never any
    changes to report.
  - Test: a run that edits one file and then hits the tool-call cap. The output must
    name the edited file, on a limit stop as clearly as on a successful one.
  - Review: this is the answer to the Session 5 self-test question. A user who
    cannot tell which files a stopped agent touched will not run it a second time.

> **Review checkpoint.** Everything above is green under `pytest`, with no API key
> and no network. Before Phase 4: check out a clean `playground/`, run the whole
> suite, then `git status` it. A test that leaves an edit behind is a test that was
> writing somewhere you did not intend.

### Phase 4 — Live

- [ ] **Step 11 — One real edit.**
  - Build: nothing. Add `replace_text` to `TOOL_SCHEMAS` and `TOOLS` if you have not
    already, and run the plan's own example task against `playground/calculator.py`:
    *read it, fix the subtraction bug, explain the change.*
  - Test: by hand. `git diff` the playground.
  - Review: read the model's explanation against the diff. They can disagree — a
    model that edited one line and described two is the exact reason Session 11's
    evaluator checks the workspace instead of the transcript.

---

## C. The failure table

Every row must produce a `ToolResult` the model can act on, and — in all but the
last two rows — a workspace that is byte-identical afterwards.

| What happens | Expected | Notes |
|---|---|---|
| `old` appears exactly once | the file is changed, result says so | the happy path |
| `old` appears zero times | refusal, file untouched, message says to re-read | the milestone |
| `old` appears three times | refusal naming the count, file untouched | ask for more context |
| `old` is empty | `invalid_arguments`, nothing opened | D48, rejected syntactically |
| `old == new` | your call — but the same every time | D48 |
| `new` argument missing | `invalid_arguments` naming `new` | D41 |
| the file does not exist | `not_found`, nothing created | D49 |
| the path is a directory | `not_a_file` | same as `read_file` |
| the file is binary | `binary_file`, no write attempted | D14 carried forward |
| the file is larger than the read cap | whatever D46 says — stated, not accidental | the sneaky one |
| the path leaves the workspace | `outside_workspace`, unchanged from Session 2 | the boundary does not care that this is a write |
| the path goes through a symlink | `symlink` | D5 |
| the file is read-only on disk | `permission_denied`, original intact | no partial write |
| the disk is full | a refusal, original intact | the temp-file shape gets this for free |
| the file changed since the model read it | whatever D44 says | the staleness race |
| crash between the match and the write | file is entirely old | D42 |
| crash during the write | file is entirely old or entirely new, never half | D42 — the whole point |
| two edits in one batch, the second refused by the budget | the first edit stands, both ids answered, the report names the file | Session 5 D33 + D52 |

---

## D. Brainstorming space

**Scope decision taken before Phase 0:** this session builds a **single-edit** tool —
one `old`/`new` pair per call, exactly one match. The multi-edit shape (several pairs
applied to one file atomically) is deliberately deferred until after all 14 sessions.
Adding it now would hand the model an escape hatch from a one-match refusal, and
widening its anchor instead *is* the behaviour Session 11 is supposed to measure.
Note this is about the tool, not the response: the loop can still receive two
`replace_text` calls in one turn — see D50.

Things I haven't decided:

- **D50's ordering and reporting rule.** Not whether to allow two edits to one file
  in a batch — `run_pending` already does that — but what the second one is matched
  against and how the report names a file touched twice.
- **Which registry owns "does this tool exist".** `TOOLS` and `TOOL_SCHEMAS` are two
  tables keyed by the same names, and `dispatch` checks `TOOLS` first — so a tool
  with a schema and no implementation answers `unknown_tool`. Merge them, or keep
  them apart and test that they agree? D41 made the schema authoritative for
  *arguments* and left *existence* where it was.
- **Which error wins when a call is wrong in two ways at once.** An object can carry
  an argument I never defined *and* omit a required one. `tests/test_list_folders.py:181`
  sends exactly that (`{"recursive": true}`) and currently expects `recursive` to be
  named. Whatever I pick has to be deterministic — same input, same message, every
  run. Smallest instance of self-test #8.
- Everything from D42 onward. D41 is the only decision closed so far.

Things that surprised me:

- **The real Claude Code edit tool is this exact shape.** Exact string match, must be
  unique, refuses otherwise — no line numbers, no diff format, no fuzzy fallback.
  Session 6's milestone is not a simplified teaching version of the real thing.
- **"Editing several places at once" is not a tool feature.** It is N `tool_use`
  blocks in one assistant message — the same protocol Session 5 already implements.
  Multi-file is the same thing with different `path`s. Nothing in the *tool* knows.
- **The model writes every block before it sees a single result.** It cannot look at
  the file between edit 1 and edit 2, so it has to predict the intermediate state.
  That is why non-overlapping anchors matter, and it is a model-behaviour property,
  not something the tool enforces.
- **A Session 2 decision had a second half nobody had written down.** `_scan`
  counted a symlink as a file so the model would not try to *list* it - and by
  leaving it unmarked, invited the model to *read* it instead. Same wasted
  call, same `symlink` refusal, one tool along. Asked "give me the files
  here", the agent truthfully listed two directories, because nothing in the
  listing said otherwise. Symlinks now carry an `@` mark (`ls -F`'s), with a
  note that appears only when one is present. Revisits session-02 D5.
- **The schemas already contained the validator.** `required` and
  `additionalProperties: False` were written down in Session 4 for the model's
  benefit; D41 was less a new design than noticing I had already specified it once.

Questions for review:

- If `dispatch` calls tools as `TOOLS[name](**parsed, root=root)`, the Python
  parameter names silently become part of the contract. Is a test that walks `TOOLS`
  and compares signatures against schemas enough, or does that want to be structural?
- Does a schema-driven validator stay small, or is this the first step of writing a
  JSON Schema implementation by accident? What is the trigger to stop?
- When Step 1 is green, have `list_files`, `list_folders` and `read_file` really come
  through unchanged — or only unchanged in the ways my existing tests happen to assert?

---

## E1. The invariants, carried forward

> **No rejection may depend on whether the target exists.** (Session 2)
> **No file's contents may change what the agent is permitted to do.** (Session 3)
> **The prompt is not a boundary.** (Session 4)
> **Every tool call gets an answer — including the ones you refuse to run.** (Session 5)
> **A tool that does not succeed changes nothing on disk.** (new)

The new one is the first invariant in this project that the *filesystem* has to
help you keep. The previous four are enforced by code you control from top to
bottom: an ordering, a comparison, a message. This one depends on how an operating
system makes bytes visible, which is why the shape of the write is a design decision
and not a detail.

It also has a quiet second half. "Changes nothing" means nothing *the user can
observe*: not a truncated file, not a stray `.tmp` left in their source tree after a
failure, not a changed mtime on a file you decided not to edit. A refusal that
leaves litter is a refusal the user has to clean up, and they will not trust the next
one.

---

## E. Known limits of this edit tool

- **Exact matching will fail on whitespace the model hallucinated.** That is a
  prompt and context problem, and Session 11 will measure how often it happens.
  Fuzzy matching would hide the measurement and introduce a failure mode — an edit
  applied to text the model never saw — that is strictly worse than a refusal.
- **Atomic rename is not a transaction.** One file is replaced atomically. Two files
  are two renames, and nothing makes the pair atomic. The plan's multi-file category
  (rename a function, update its imports) is several calls with no group guarantee,
  and Session 12 will have to score partial completions honestly.
- **No locking.** Your editor, a watcher process, or a second agent can write the
  same file between your read and your rename. D44 narrows that window; nothing here
  closes it.
- **No undo.** The journal records what was asked for, not what was overwritten
  (unless D51 says otherwise). `git` in the playground is your undo, and the fixtures
  are disposable on purpose.
- **One edit per call, by choice.** There is no way to apply several replacements in
  one atomic operation. A model wanting three changes to one file sends three calls,
  and nothing makes that group all-or-nothing — if the third is refused, the first two
  stand. The multi-pair shape that would close this is a post-course extension, and
  until it exists D52's report is what keeps a partial application honest.
- **One match is a rule, not a guarantee of correctness.** A unique match in the
  wrong function is still a unique match. The tool guarantees that the edit is
  unambiguous, never that it is right — which is the whole reason Session 11's
  evaluator exists.

---

## F. Self-test before moving to Session 7

1. A timeout fires while `replace_text` is running. Is it safe to retry, and which
   of the two flags on `ModelError` is the one that answers that?
2. Your journal holds a `tool_intent` for an edit with no `tool_outcome`. What state
   is the file in — and can you prove it from the journal alone, without looking at
   the file?
3. The model sends `old` with four spaces where the file has a tab. What does your
   tool say back, and why is "fix it for them" the wrong answer?
4. A 200KB file was truncated at 64KB when the model read it. The model's `old`
   matches once in what it saw and twice in the whole file. What does your tool do?
5. Two edits to the same file arrive in one batch and the second is refused by the
   budget. What is on disk, what does the run report, and what does a `--resume`
   do next?
6. Why can `replace_text` not create a file, when `read_file` could perfectly well
   have returned "not found" and let the model carry on?
7. Which of Session 2's five rejection codes can a write operation reach that a read
   could not — and if the answer is "none", what does that tell you about where you
   put the boundary?

And the one Session 7 will punish a wrong answer to:

8. You run the same edit task twice against a fresh fixture and get two different
   diffs. Which parts of your run are allowed to differ, and which parts would make
   the evaluation in Session 11 meaningless?
