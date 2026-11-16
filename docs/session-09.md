# Session 9 — The Same Thing Again, and a Conversation That Only Grows

**Goal (from the plan):** handle repeated actions and context growth.
**Done when:** repeated failures trigger a stop; large outputs are bounded.

Half of this is already measured and none of it is enforced, which is the most
honest place a session has started yet.

Session 8 ended by printing a sentence it could not act on:

```
  WARNING: 4 failing tool call(s) were sent again byte-identically.
           Nothing stops that except the tool-call limit.
```

`LoopResult.repeated_failures` is that number. It exists because D75 decided
that building a loop detector as a side effect of error handling produces one
nobody can configure — so the measurement was taken and the stopping was left
to today. The other half is bounded already: `read_file` caps at
`MAX_READ_BYTES` (64KB), `list_files` at `MAX_BYTES` (8KB), and every truncated
result carries `[truncated: this is not the whole result]` so a model that saw
half a file knows it saw half a file.

So what is actually left:

- **Nothing stops a run that is going nowhere.** Five identical failing calls
  cost five tool calls and *one step*, because D27 made a step a model call and
  a batch of five arrives in one of those. The step limit — the oldest limit in
  the project — cannot see this at all.
- **The history only grows.** D30 says history keeps everything and truncation
  happens in `tools.py`. That was right, and it means a long run's conversation
  grows without bound until the provider refuses it, and on Ollama does
  something worse than refuse: `context_warning` exists because overflow there
  silently drops the oldest messages, including an assistant turn whose
  tool_call ids are still being answered.
- **Two things look identical from outside and need different answers.** That
  is Session 8's self-test #8 and today is where it comes due: a backoff
  sending three identical requests and a model sending three identical tool
  calls are both "three requests, no progress". One is the transport being
  patient. One is the agent being stuck.

And the trap sitting in the middle of it: **the live run at the end of Session 8
read a file it had already read.** It had been told its edit already applied, so
it re-read `stats.py` to check before reporting. That is the single most correct
thing an agent did in this entire project, and the most obvious formulation of
today's rule — *stop when a call repeats* — kills it.

---

## A. Decisions to lock before writing code

| #   | Decision                                        | Options                                                                        | My pick + why |
|-----|-------------------------------------------------|--------------------------------------------------------------------------------|---------------|
| D77 | What "the same action" means                     | identical `(name, arguments)` bytes / normalised arguments / same tool + same path / semantic | **Tool name + arguments, with `path` resolved through `policy.resolve_in_workspace`.** Not a per-tool normalisation flag - it is the one canonicalisation the workspace boundary already performs on every path that reaches a tool, so `a.py`, `./a.py` and `sub/../a.py` collapse without `tools.py` exposing anything new. Key order is normalised, non-path arguments are bytes, and unparseable arguments compare as the raw string (three identical `invalid_arguments` IS the commonest stuck loop). A path the boundary refuses keeps its raw spelling: it never reaches a tool, so there is no canonical form to resolve it to. |
| D78 | Whether a repeated **success** counts            | only failures repeat / successes count too / successes count at a higher threshold | **Both count, at different thresholds.** A repeated failure is a model that has not understood the error; a repeated success is usually verification. The plan's table says "repeated identical tool call -> stop" without distinguishing them, and the evidence that it is wrong is in this project's own journal - see the audit below. |
| D79 | The threshold                                    | 2 / 3 / configurable, and with what default                                     | **3 failures, 5 successes**, both from `.env` (`MAX_REPEATED_FAILURES`, `MAX_REPEATED_CALLS`). Starting points for Session 12 to argue with, not results. |
| D80 | Consecutive or cumulative                        | N in a row / N anywhere in the run / N within a sliding window                  | **Cumulative per signature.** Consecutive is cheaper and blind to the exact shape this session exists to catch - the agent that re-reads the same file between every edit, where a different call always sits in between. Cumulative also catches the A/B/A/B two-cycle for free without this becoming a cycle detector: A's third occurrence is A's third occurrence however B is spaced. |
| D81 | What the stop is called                          | a new `STOP_REPETITION` / reuse `STOP_TOOL_LIMIT` / not a stop at all           | **A new `STOP_REPETITION`** (and `STOP_CONTEXT_LIMIT` for D86). Reusing `STOP_TOOL_LIMIT` would have been free and lies in a specific way: "went in circles" and "ran out of tool calls" are the two outcomes Session 12 exists to tell apart. |
| D82 | Whether the model is warned first                | stop silently / one warning in a tool result, then stop / warn every time       | **One warning in the tool message, then the stop.** Appended by `tool_message`, NOT written into the `ToolResult` - that is frozen so a trace cannot be edited after the fact. The loop may add its own sentence to the conversation; it may not change what the tool reported. `Tally.should_warn` enforces the "once" so it is not a promise the loop keeps by hand. |
| D83 | Retries versus resends (Session 8's #8)          | one counter for both / `model_retry` counts attempts, repetition counts calls / neither counts the other | **Neither counts the other.** A retry never reaches the tally (no tool ran, nothing was answered); a resend never touches the retry policy (the request succeeded). They are separately countable by eye in one journal: `model_retry` events vs repeated `tool_intent` records. |
| D84 | Where the counter lives                          | `budget.py` (a rule) / `loop.py` (a fact about this run) / its own module       | **Thresholds on `Budget`, counts in the loop.** The same split as `Budget`/`Ledger`: the rule is frozen, printable and recorded with the result; the reading is mutable and belongs to the run. This also answers the brainstorm question below - repetition IS a budget, and `describe()` prints it. |
| D85 | How conversation size is measured                | characters / a local tokenizer / the provider's `prompt_tokens` from the last response | **A local estimate (`chars/4 + 4/message`), calibrated against `prompt_tokens` and reported as a ratio.** The exact figure is one step stale and does not exist before the first response; the step that overflows is the one it has no number for. The ratio is reported, never applied: scaling the estimate by a correction measured on the previous request is a second estimator with no error bar. |
| D86 | What happens as the context fills                | stop cleanly / drop oldest / summarise (deferred by the plan) / refuse to start a step | **Refuse to start a step that would not fit**, as a `check_before_step` branch rather than a second gate - the question is identical (may the loop make another model call?) and two gates is how one gets forgotten at one call site. Never drop-oldest: dropping a turn can orphan a `tool_call` id. |
| D87 | Whether the output caps move                     | keep 64KB/8KB / per-tool / per-run budget of total tool bytes                   | **Keep 64KB/8KB. No new byte budget.** The run-level bound is the context check, measured in the unit that actually matters - what the next request carries - rather than in bytes of tool output. A byte budget would be a third limit that fires later than the one already there and explains less when it does. |
| D88 | Whether a repetition stop is a failure           | an exit code like the other limits / a normal answer with a stop reason         | **A limit stop, with its own exit code** (repetition 16, context 17). A run that stopped repeating itself has not finished the task, and reporting it as a normal answer is the "claimed success" failure Session 7 built a verifier to catch. |

**Hardest two: D77 and D78.**

D77 because every definition is wrong somewhere. Byte-identical arguments is
the only one you can implement without judgement — and it misses
`{"path": "a.py"}` versus `{"path": "./a.py"}`, which the model will produce
within three steps of being told to try again. Normalising arguments means
`tools.py` has to expose what normalisation means per tool, which is the "flag
on the tool" smell Session 8 refused. Semantic sameness is not available to
anything in this repository and pretending otherwise is how you ship a stop
rule that fires on two different reads of two different files.

D78 because of the `stats.py` re-read. A repeated *failure* is a model that has
not understood the error. A repeated *success* is often verification, which is
the behaviour the whole project has been trying to produce — and the plan's own
failure table says "repeated identical tool call → stop at the configured
repetition threshold" without distinguishing them. The plan is wrong here, or
at least underspecified, and the evidence for that is a run this project already
has in its journal.

**And the sneaky one: D85.** Counting tokens honestly requires the provider's
tokenizer, which is not installed and is not the same across providers.
`prompt_tokens` from the last response is exact, free and *one step stale* — it
describes the history before the turn you are about to send. Characters are
available immediately and wrong by a factor that varies with the content. The
decision is not "which is accurate", it is "which error do you want": a stale
number that was exact, or a fresh number that never was.

---

## B. Sequential build order

Phases 0 through 3 need no API key and no network. Phase 4 is live, and Phase 5
is the one that matters: the Session 11 suite does not exist yet, but
`scripts/fault_matrix.py` and `fixtures/` do, and a stop rule that has never
been run against a correct run is a stop rule you have not tested.

### Phase 0 — Count before you limit

- [x] **Step 1 — A repetition record, not a rule.** _(`agent/repetition.py`:
  `Tally`, counting both outcomes per canonical signature.)_
  - Build: extend what `_Run.failed_before` already does into a full record of
    every call in the run — `(name, arguments)` and whether it succeeded — and
    report the longest repeat. Nothing stops.
  - Test: five identical failures report 5; the Session 8 live transcript
    (read, edit, read) reports 2 for `read_file`.
  - Review: decide D77 by looking at real transcripts, not at the abstraction.
    If `{"path": "stats.py"}` and `{"path": "./stats.py"}` both appear in your
    own journals, byte-identity has already failed.

- [x] **Step 2 — Measure the conversation.** _(`agent/context.py`; printed by
  the CLI once past half the window, with the est/actual ratio beside it.)_

  **The error bar, measured against a real run** (`20261116T102728-d4263f`,
  DeepSeek, two requests). The review said to record the ratio and treat it as
  the error bar for the rest of the session. It took three versions to get a
  number worth writing down:

```
  version                                  step 1   step 2
  history only, 4 chars/token               0.45     0.41    under-reads 2.4x
  + the tool schemas, 4 chars/token         0.90     0.91    under-reads 10%
  + the tool schemas, 3.5 chars/token       1.02     1.04    over-reads 3%
```

  The first number is the interesting one. The estimator measured the
  HISTORY, and a request is not a history: `TOOL_SCHEMAS` is 2,565 characters
  that travel on every single request and appear in no message. A 2.4x
  under-read in the direction where the check passes and the provider refuses.

  3.5 rather than 4 is then a calibrated constant - chosen once from these
  traces, written into the module, and still reported live so it can be seen
  to drift. It is NOT the dynamic correction D85 refuses: scaling the estimate
  by a ratio measured on the previous request is a second estimator with no
  error bar of its own.
  - Build: a size for `self.history` — whatever D85 decides — printed per step
    next to the token line that already prints.
  - Test: a run whose history crosses 50% of the configured context prints
    something a human notices.
  - Review: compare your measure against `prompt_tokens` from the same step.
    Record the ratio. That number is your error bar for the rest of the session.

### Phase 1 — The sameness rule, with no loop at all

- [x] **Step 3 — A pure function.** _(`signature`, `is_repeat`, and a
  `Repetition` value. Nothing in the module touches the loop.)_
  - Build: `is_repeat(previous_calls, call) -> bool` or a small
    `Repetition` value, in the shape `retry.py` and `replay.py` already use:
    policy that returns a verdict and touches nothing.
  - Test: identical calls, same tool different path, same path different tool,
    the A/B/A/B alternation (a two-cycle is a loop and byte-identity misses it),
    and a batch that contains the same call twice *in one response*.
  - Review: the alternating case is the one that decides whether you are
    building a repeat detector or a cycle detector. They are different programs.

- [x] **Step 4 — The threshold, and what it means.** _(3 failures / 5
  successes, cumulative per signature.)_
  - Build: D79 and D80. Consecutive is cheap and forgiving; cumulative catches
    the agent that reads the same file between every edit.
  - Test: a run that repeats twice, recovers, and repeats twice again. Under
    "consecutive" it never stops. Decide whether that is correct.

### Phase 2 — The stop

- [x] **Step 5 — Wire it in, without breaking D33.** _(`_dispatch` returns a
  stop reason; the rest of the batch is refused and answered.)_
  - Build: D81 and D82. The batch is still answered in full — a stop rule that
    skips the rest of a batch produces the corrupted conversation Session 5
    spent a phase proving you must not produce.
  - Test: repetition detected at call 2 of 3; calls 2 and 3 still get tool
    messages; the run stops after the batch.
  - Review: `LIMIT_STOPS`, `LIMIT_EXIT_CODES` and `explain_limit` all need the
    new reason, or the run stops with a word the CLI cannot explain.

- [x] **Step 6 — Tell the model once.** _(`Repetition.notice`, appended by
  `tool_message` - never to the frozen `ToolResult`.)_
  - Build: D82. A tool result saying "this is the third identical call; it will
    not be answered again" is an instruction the model can act on. A silent stop
    is not.
  - Test: the warning appears in the history exactly once, and the stop still
    happens if the model ignores it.
  - Review: this is the same reasoning as `unknown_tool` naming the menu
    (Session 4) — a model told only "no" guesses again.

### Phase 3 — Context growth

- [x] **Step 7 — Refuse to start a step you cannot finish.** _(a branch in
  `check_before_step`, above the clock because it is deterministic.)_
  - Build: D86, as a `check_before_step` sibling. The budget already refuses a
    step whose response would exceed the token limit via `token_reserve`; this
    is the same shape against the context window rather than the wallet.
  - Test: a history engineered just under and just over the line.
  - Review: `context_warning` in `model.py` already knows the Ollama failure
    mode. Make sure the new check fires *before* the provider silently drops
    the system prompt, because after that the trace blames the wrong thing.

- [x] **Step 8 — Bound the outputs, deliberately this time.** _(nothing new:
  D87 says the context check IS the run-level bound.)_
  - Build: D87. Probably nothing: 64KB and 8KB are already enforced and already
    announced. What may be missing is a run-level total — ten reads of 64KB is
    640KB of history that no per-call cap noticed.
  - Test: ten large reads in one run.
  - Review: if you add a run-level byte budget, it is a new limit and it owes
    the same things every other limit owes: a name, a stop reason, an exit code
    and a sentence in `Budget.describe()`.

> **Review checkpoint.** Full suite green, no API key, no network. Then read
> your own Session 8 journals back through the new rule and count how many
> correct runs it would have stopped. If the answer is more than zero, the rule
> is wrong and the measurement just saved you a live session.

**Done — `scripts/repeat_audit.py`, 91 journals, 230 answered calls.**

```
rule                     runs stopped   of those, runs that had ANSWERED
3 failures / 5 successes       0                      0
2 failures / 2 successes      14                     14   <- before the fix below
2 failures / 2 successes       2                      2   <- after
```

At the chosen thresholds the rule is silent on every run this project has
ever completed. At the strict threshold the plan's own wording implies -
repetition is an offence, stop at 2 - it bans exactly two runs, and they are:

```
20261114T094830-b4a09c   read_file({"path": "calculator.py"})   Session 7
20261115T123059-c30806   read_file({"path": "stats.py"})        Session 8, live
```

The second one is **the run this session was designed around**: the crashed
edit, the resume, the re-read before reporting. The strong version of the rule
kills it. That is no longer an argument, it is a row in a table.

**And the audit found a bug before any model did.** The first run of it
reported 242 calls where the journals hold 230. A resumed run that re-ran an
interrupted call writes a SECOND `tool_intent` under the same call id, and
`rebuild` was counting both - so `kill -9` could push a run toward a
repetition stop. The mirror image of the bug D39 fixed for tokens, where a
crash could refund a budget. One id is one call; `journal.rebuild` now keeps
the last intent per id. Twelve phantom calls across 91 runs, every one a crash
artefact, and the strict-rule casualty list fell from 14 to 2 once they were
gone.

### Phase 4 — Live, and against the fixtures

**Done, 2026-11-16, DeepSeek (`deepseek-flash`), five live runs.** Phases 0-3
were green with no key and no network; the review checkpoint above is the
offline half of Step 9's question, answered against 91 real journals. These
are the live half.

- [x] **Step 9 — One real run that should never trip it.**
  - Test: `tail_window` end to end with repetition enabled. The read → edit →
    re-read pattern must survive.
  - **Result: it survives.** `20261116T104016-fcd19a` fixed the bug in 4 steps
    / 6 tool calls with no repeat at all. The second run
    (`20261116T104049-0de826`) asked for the verification explicitly - read
    `stats.py`, edit it, read it again - and reported
    `repeats: 0 failing, 1 succeeding, longest run 2`, under the threshold of
    5, exit 0, `fixture.py verify` PASS. The `stats.py` re-read this session
    was designed to protect is now a live trace, not an argument.

- [x] **Step 10 — One real run that should.**
  - Test: the run stops for repetition rather than for the step limit, and the
    report says which call it was.
  - **Result: stop=repetition, exit 16, after 3 identical calls.**
    `20261116T104203-0f22ff`:
    `the same call was made 3 times without making progress: replace_text(...)`.
  - **D77 earned its keep on the wire.** The model sent the SAME call with a
    different JSON key order - `{"path", "old", "new"}` on attempt 1 and
    `{"new", "old", "path"}` on attempts 2 and 3. Byte-identity would have
    seen three different calls and never stopped. The normalised signature saw
    one call three times.
  - **An honest negative first.** A merely impossible task does NOT loop this
    model. Told to fix a `rolling_median` that does not exist, it read the
    file, found the real line and fixed that instead; told to replace a line
    that exists nowhere, it read the file, reported the absence and refused to
    retry (`20261116T104136-7fad9a`, 3 steps, stop=answer). The loop had to be
    instructed - "never read, never vary the arguments, resend on error" -
    which is the `fault_matrix.py` precedent: the model's judgment is not what
    is being measured, the loop's stop is.
  - Review, same prompt with `--no-repeat-stop`
    (`20261116T104242-680963`):

```
                    steps   tool calls   total tokens   stop
repetition on         3          3           4,135      repetition (exit 16)
repetition off        7          6          11,616      the model gave up
```

    Half the tool calls and a third of the tokens, and the run that stopped
    said WHY. That difference is what this session bought.

- [x] **Step 11 — Answer Session 8's #8 with a trace.**
  - Test: force a run that does both at once — a flaky provider *and* a stuck
    model — and read the journal. `model_retry` events and repeated
    `tool_intent`s must be separately countable, by eye, in one file.
  - **Result: `20261116T104330-035cca`**, run through a local pass-through
    proxy that answered every other request `429 Retry-After: 1` while the
    model was under the stuck-loop instruction. One file, two numbers:

```
model_retry   step=1 attempt=1 kind=rate_limit delay=1.0s reason=retry_after
tool_intent   step=1 replace_text({"path": ..., "old": ..., "new": ...})
tool_outcome  ok=False error=no_match repeat=1
model_retry   step=2 attempt=1 kind=rate_limit delay=1.0s reason=retry_after
tool_intent   step=2 replace_text({"new": ..., "old": ..., "path": ...})
tool_outcome  ok=False error=no_match repeat=2
tool_intent   step=3 replace_text({"new": ..., "old": ..., "path": ...})
tool_outcome  ok=False error=no_match repeat=3
run_finished  stop_reason=repetition repeats={failures: 2, longest: 3}
```

    2 retries, 3 resends, `repeat:` counting on the outcome record and nothing
    on either side touching the other. D83 holds in the wild: the transport
    being patient and the agent being stuck are two counters, and a human can
    tell them apart without a tool.

- [ ] **Follow-up found by Step 10 — `explain_limit` prints the wrong limit.**
  The stop above was the FAILURE threshold (3) and the sentence ended
  `(limit 5)`. `cli.py`'s `explain_limit` picks `max_repeated_calls` whenever
  `repetition_stopped_on and result.longest_repeat` are set, which is true for
  every repetition stop - so a failure stop always reports the success limit.
  It needs to know which threshold fired, not whether the fields are
  populated. Cosmetic, in the one sentence a human reads when a run stops.

---

## C. The failure table

| What happens | Expected | Notes |
|---|---|---|
| five identical failing calls | stop, naming the call | today: nothing stops |
| the same call succeeding five times | D78 — and the same way every time | the `stats.py` re-read |
| A, B, A, B, A, B | D77 decides whether this is even visible | byte-identity sees two calls, each twice |
| the same call twice in ONE response | both answered (D33), counted once or twice | decide and write it down |
| a repeat after a successful intervening call | D80 | consecutive says no, cumulative says yes |
| history exceeds the context window | a clean stop before the request | not a provider 400 |
| Ollama with 4096 context | refuse to start, loudly | silent truncation corrupts tool-call pairing |
| one tool result larger than the cap | truncated, announced | already true |
| ten results under the cap | D87 — possibly nothing | the cap nobody has |
| a retry storm and a stuck model together | two numbers, both visible | Session 8, #8 |
| repetition stop with edits already made | the changed files are still reported | D52 applies to every stop |

---

## D. Brainstorming space

**Scope decision taken before Phase 0:** no summarisation, no compaction. The
plan defers it explicitly and for a reason this project can now state precisely:
a summary that drops a `tool_call` id produces a history the provider rejects,
and a summary that drops a file's exact bytes produces an `old` that no longer
matches. Session 12's report is what should decide whether compaction is worth
its risk, and Session 12 does not exist yet.

Decided during the build:

- D77 through D88, all closed above.
- **Repetition is a budget, not an invariant.** The thresholds sit on `Budget`
  and print in `describe()` (`repeat<=3/5 ctx<=65536`); the counts sit in the
  loop, like `Ledger`. The deciding argument was Session 12: an arm whose rule
  is not in the recorded configuration is an arm you cannot compare.
- **The counter survives `--resume`**, for the reason the question contained -
  a budget you can reset with `kill -9` is not a budget. Nothing new is
  journalled for it: `tool_intent` already carries the name and arguments and
  `tool_outcome` already carries `ok`, so the counts are rebuilt by reading
  what is already on disk.

Things that surprised me:

- **The frozen `ToolResult` refused the obvious implementation.** D82's warning
  wants to be appended to the tool's output; `ToolResult` is frozen precisely
  so the loop cannot rewrite what a tool reported, and Session 10 is the reason
  it is. The note went into the MESSAGE instead, which is better and was not
  the first idea: the model reads one string, the trace keeps the tool's own
  words and the loop's own words separately attributable.
- **The rule had to be applied AFTER dispatch**, which felt wrong and is right.
  The outcome is half the signature's identity (D78), and nothing knows it
  before the tool runs. The call that trips the rule has therefore already run
  - it is answered in full, and the stop lands after the batch.
- **The audit found a crash-accounting bug** that no test would have: twelve
  phantom calls produced by resumed runs re-running an interrupted call. See
  the review checkpoint above.
- **A request is not a history, and the estimator did not know that.** See
  Step 2's table: the first version under-read a real prompt by 2.4x because
  the tool catalogue is sent with every request and lives in no message. The
  offline tests could not have caught it - they all pass a `tools=` list the
  estimator was ignoring, and nothing but the provider's own number says what
  a request actually weighs.
- **`canonical_arguments` had to keep unparseable arguments as-is.** The first
  version dropped them as "not a real call". Three identical `invalid_arguments`
  in a row is the most common stuck loop there is, and dropping it would have
  dropped the case the session exists for.

Questions for review:

- If the model repeats a call because the *tool result was truncated* and it is
  trying to see the rest, is that repetition or is that my output cap making a
  loop? Which of the two should change? **Still open, and the honest answer is
  that today's rule blames the model for the cap's behaviour.** `read_file`
  offers no way to ask for the rest of a file, so a model that wants the tail
  of a 2MB file can only send the same call again. The rule stops it on the
  third try and the report says "repetition", which is true and names the wrong
  component. The fix is an offset argument on `read_file`, not a change here -
  logged for the post-course backlog.
- A batch of five identical calls arrives in one response. Is that one mistake
  or five? **Five.** The tool-call budget counts five, each one was dispatched,
  and a model that gets N free attempts by batching them has found a hole in
  the rule rather than a better way to work. A human reading the transcript
  still sees one mistake, and the report's `longest run N` is there so both
  readings are available.
- Does the repetition counter belong in the journal? **It is re-derived from
  it**, which is better than being written to it: no new event type, and the
  counts reconstruct from records that have existed since Session 6.

---

## E1. The invariants, carried forward

> **No rejection may depend on whether the target exists.** (Session 2)
> **No file's contents may change what the agent is permitted to do.** (Session 3)
> **The prompt is not a boundary.** (Session 4)
> **Every tool call gets an answer — including the ones you refuse to run.** (Session 5)
> **A tool that does not succeed changes nothing on disk.** (Session 6)
> **The model's account of what it did is a claim; the workspace is the record.** (Session 7)
> **A retry is an action, not a correction.** (Session 8)
> **Doing the same thing twice is evidence, not an offence.** (new, **adopted**)

It survives, and it is now measured rather than argued: at the chosen
thresholds the rule stops none of the 91 runs this project has completed, and
at the offence-grade threshold it stops the two that were being careful. The
weaker wording is the one the evidence supports.

The proposed one is deliberately weaker than the plan's wording, and this
session is where you find out whether it survives. "Repeated identical tool
call → stop" treats repetition as misbehaviour. But an agent that re-reads a
file before reporting on it is repeating a call *because it is being careful*,
and the only run in this project that did so is the one that recovered
correctly from a crash.

The strong version — repetition is an offence — is simpler to implement and
would have stopped that run. If you adopt it anyway, adopt it knowingly: write
down which correct behaviour you are banning in order to catch the stuck case,
and make sure Session 11's suite contains a task that needs the banned pattern.

---

## E. Known limits of whatever you build today

- **A loop detector cannot tell "stuck" from "slow".** Three identical reads
  separated by three different edits is a different program from three
  identical reads in a row, and no counter distinguishes intent.
- **Byte-identity is a spelling test.** The model can defeat it without
  learning anything, by changing `"a.py"` to `"./a.py"` — and will, because the
  error message that triggered the repeat is still in its context.
- **Context measurement is per-provider.** Any number you compute locally is an
  estimate against DeepSeek and a different estimate against Ollama, and the
  only exact figure arrives one step too late.
- **This project now measures context two different ways, and both are
  right.** `chat.py`'s D59 uses the last turn's real `prompt_tokens` and
  refuses a new TURN at the ceiling; D85 here uses a local estimate and
  refuses a new STEP. The difference is position, not disagreement: between
  turns a human is waiting and the exact number is one turn old, which is
  fresh enough; inside a run the history grows by a 30KB tool result between
  one check and the next, and a number from before that result is a number
  about a different conversation. Two measures of one quantity is a smell
  worth re-reading in Session 13, where a server process has both.
- **The context window is a configured default, not a measurement.** 65536
  for DeepSeek, 32768 for Ollama - the figure we tell people to serve, not
  the one `ollama serve` actually hands out (4096). `model.context_warning`
  shouts about the gap, which is a tripwire rather than a guarantee: a cold
  server reports nothing. A run against a 4096-token server with
  `CONTEXT_WINDOW` unset still believes it has 32768.
- **Bounding outputs does not bound the history.** The cap is per call. The
  conversation is the sum, and nothing in the loop currently adds it up.
- **A repetition stop is itself a budget you can tune into uselessness.** Set it
  to 10 and it never fires; set it to 2 and it fires on correct verification.
  Session 12's comparison is the only honest way to pick, and it is three
  sessions away.

---

## F. Self-test before moving to Session 10

1. A model sends the same `read_file` call three times, with one successful
   `replace_text` between the second and the third. Does your rule fire, and is
   that the answer you want?
2. Five identical failing calls arrive in one response. How many "repeats" is
   that, how many tool calls does it cost, and how many steps?
3. Your context check says the next request fits. The provider disagrees and
   returns a 400. Name the two places that number could have come from and
   which one you trusted.
4. A run stops for repetition after making two edits. What does the user see on
   stdout, on stderr, and in the exit code?
5. The model is told "this call will not be answered again" and sends it again
   anyway. What happens, and is that a stop or a refusal?
6. You resume a run that had already repeated twice. Does the counter continue
   or restart, and which answer makes `kill -9` useful to a stuck agent?
7. A tool result is truncated, the model asks for the same file again, and your
   rule stops the run. Which component was actually wrong?

And the one Session 10 will punish a wrong answer to:

8. Everything you counted today — repeats, history size, the longest cycle —
   exists in a dataclass in RAM. Session 10 has to make a run inspectable after
   it is over, and Session 8 already proved which signals survive a process.
   Which of today's numbers are in the journal, which are only on screen, and
   which would you have to re-derive by re-reading the whole conversation?
