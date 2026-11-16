# Session 8 — Retrying Is an Action

**Goal (from the plan):** deliberately introduce failures.
**Done when:** timeouts, malformed arguments, and unknown tools do not crash the process.

Read the criterion literally and most of it already passes, which is the most
dangerous way a session can start.

`dispatch` turns an unknown tool name, unparseable JSON and a wrong-typed argument
into `ToolResult`s the model can act on — Session 4 step 8, and `tests/test_tools.py`
has held it ever since. No `httpx` exception escapes `model.py`: every failure leaves
that module as a `ModelError` with a `kind` the CLI maps to an exit code. Named
failpoints live in the real code path, not in a copy of it. `journal.heal()` repairs
a torn final record. `--resume` rebuilds the history a dead process had in RAM.

So the easy half is done. What is left is the half where *not crashing* is not the
same as *being right*:

- **Two flags with no consumer.** `ModelError` carries `retryable` and
  `safe_to_retry`, and the docstring for the second one says *"did the request
  definitely NOT reach the server?"* Nothing reads either. `agent/loop.py:250` says
  it out loud: *"A ModelError propagates. The loop does not retry (Session 8)."*
  That line comes due today.
- **`replace_text` is atomic but not idempotent.** Those are different properties
  and Session 6 only bought the first. Replaying an edit that already landed finds
  *zero* matches — so the replay returns a refusal, which looks exactly like a model
  error and is in fact a success you can no longer see. `ResumeState` already carries
  `in_flight_call_ids` with the comment *"Safe to re-run only if the tool is
  idempotent or atomic"*, written in Session 6 and never cashed.
- **Failures can finally be measured instead of demonstrated.**
  `scripts/crash_demo.py` has been *showing* you crashes since Session 5. Session 7
  built a harness that grades the filesystem independently of what the run claims.
  Point one at the other and a crash stops being a story about a terminal and starts
  being a row in a table.

And the question Session 7's self-test ended on: *when a run dies between the tool
call and the tool result, which of the four signals — verifier, `changed_paths`,
diff, final message — is still trustworthy?* Two of them die with the process. This
session is where you prove which two.

---

## A. Decisions to lock before writing code

| #   | Decision                                        | Options                                                                | My pick + why |
|-----|-------------------------------------------------|------------------------------------------------------------------------|---------------|
| D65 | Where the retry lives                           | inside `call_model` / in `_Run.take_step` / a wrapper around `model_fn` | **In the loop, policy in `agent/retry.py`.** A wrapper around `model_fn` cannot know that step 2 edited a file, and that is exactly what decides D69. The loop has the journal, the budget and `self.changed`. |
| D66 | Attempts and delay                              | fixed delay / exponential / exponential with jitter                     | **Exponential with full jitter, capped** — 0.5s doubling to 8s. Jitter buys nothing with one process; it is here for Session 13 and `jitter=False` exists so a test can read the raw curve. |
| D67 | `Retry-After` from the provider                 | ignore it / honour it / honour it up to a cap                           | **Honour it, up to 30s.** Longer than the cap is not a retry, it is an instruction to come back later, and sleeping through it burns a wall clock the user set. |
| D68 | Which `kind`s are retried at all                | `retryable` alone decides / an explicit allowlist per kind              | **`retryable` alone.** It is set where the error is translated, which is the one place that knows what the status meant. A second allowlist would be a copy that drifts. |
| D69 | A read timeout, where `safe_to_retry` is false  | never resend / resend anyway / resend only when no tool has written yet | **Resend only while nothing has been written this run.** A read timeout costs money to repeat; after a write it could cost a file. `self.changed` is already the loop's record of that. |
| D70 | What a retry costs the budget                   | nothing / a step / its own attempt budget / wall clock only             | **Wall clock only** — not a step, not a tool call — plus a ceiling of its own (`max_attempts`) and a refusal to sleep past the deadline. |
| D71 | What the journal records per attempt            | only the outcome / one event per attempt / attempt count on the result  | **One `model_retry` event per attempt, written before the sleep.** A count on the result cannot be written by a process that dies during the backoff, and the attempt that was already paid for is exactly the one you want on disk. The event carries both flags, so a trace says *why* the policy decided, not just what it decided. |
| D72 | A resumed run with an in-flight **edit**        | replay it / refuse and report / re-read the file and decide             | **Re-read the file and decide**, with *refuse and report* as the fallback when there is nothing to decide against. Replaying blind is the only option that can produce "the agent said it changed nothing and the file changed", which is the worst of the four outcomes. `agent/replay.py`. |
| D73 | How a replayed edit is recognised               | not at all / a hash in `tool_intent` / compare file bytes at resume     | **Both, because neither works alone.** A hash in `tool_intent` with nothing to compare it to is a number; bytes compared at resume with nothing recorded are bytes compared to a guess. The intent carries `before`, the resume re-reads the file, and the rule is one line: same bytes → replay, different bytes → do not. |
| D74 | What a hallucinated tool name costs             | a step / a tool call / nothing but tokens                               | **A tool call** - which it already did, since `_dispatch` counts before it dispatches. Made deliberate rather than incidental: the slot was spent whether or not the tool existed, and a mistake that costs nothing is a mistake a model can make forever inside a budget that never notices. |
| D75 | The same malformed call arriving twice          | answer it twice / stop the run / hand it to Session 9's counter         | **Answer it twice, count it, and say so.** Stopping on repetition is a loop detector, and a loop detector built as a side effect of error handling is one nobody can configure. `repeated_failures` is the measurement that gives Session 9 something to be designed against. |
| D76 | Exit code and report when retries are exhausted | reuse the existing `kind` code / a new "gave up" code                   | **Reuse the `kind` code, add a sentence.** A 500 that failed three times is still a 500 to anything scripting this; what changed is how many requests were sent, and that belongs in prose, not in a number every caller would have to learn. |

**Hardest two: D69 and D72.**

D69 because a read timeout is the one failure where you genuinely cannot know what
happened. The connection opened, the request went out, and the answer never came
back. The server may have ignored it, may have served it and lost the reply, or may
still be working. `safe_to_retry=False` is not pessimism, it is an accurate
statement about what you know — and the honest responses to it are "fail the run"
or "resend and accept that you may pay twice and act twice". Choosing to resend
is defensible. Choosing it *without writing down what you are risking* is not.

D72 because resume was designed when every tool was a read. Replaying a `read_file`
costs nothing. Replaying a `replace_text` either does nothing (the edit landed, the
anchor is gone, you get a zero-match refusal that reads as failure) or does it
again (the anchor is still there, because the first attempt died before writing).
The journal cannot currently tell those apart — that was Session 6's step 9 and the
answer was "record a hash or make it unnecessary". Today it becomes load-bearing.

**And the sneaky one: D70.** A three-attempt backoff with exponential delay can
spend thirty seconds making no progress at all. The wall clock is the only limit
that notices, and Session 5's D39 deliberately scoped seconds to *this process*
rather than the run. So a retry storm can eat a time limit that a `--resume` then
resets — and the run looks, in the trace, like it simply stopped. Decide whether an
attempt is a step, a tool call, a thing with its own ceiling, or free.

---

## B. Sequential build order

Phases 0 through 3 need **no API key and no network** — the injected `model_fn`
from Session 4 is what makes that true, and this session leans on it harder than
any before. Phase 4 is live, and by then the only new thing is the real provider's
sense of timing.

### Phase 0 — A model that fails on command

- [x] **Step 1 — An injectable clock.** _(`sleep=` on `run_loop`; `Clock` in `tests/test_retry.py` records instead of waiting.)_
  - Build: the backoff has to sleep somewhere. Make that a parameter, not a call to
    `time.sleep` buried in a function.
  - Test: a three-attempt backoff completes instantly and records the delays it
    *would* have taken.
  - Review: a test suite that actually waits is one you will stop running. This is
    also the first thing you will want again in Session 13, where a timeout is a
    property of a request rather than of a process.

- [x] **Step 2 — The fault menu.** _(`Flaky` raises prepared `ModelError`s in order, then answers.)_
  - Build: a `model_fn` that raises any `ModelError` you name, on any attempt
    number you name — fail twice then succeed, fail forever, fail once with
    `safe_to_retry=False`.
  - Test: the menu itself. A fake that cannot reproduce "succeeds on attempt 3"
    cannot test the thing you are about to write.
  - Review: list the kinds you can produce against the ones `model.py` can actually
    raise. A fault that the real adapter never emits is a test of your imagination.

### Phase 1 — The backoff, offline

- [x] **Step 3 — Delay as a pure function.** _(`retry.delay_for`, with `rng` injected.)_
  - Build: D66. Attempt number in, delay out. No sleeping, no errors, no I/O.
  - Test: the sequence for attempts 1..N, the cap, and the jitter bounds if you
    added jitter.
  - Review: jitter exists because synchronised clients retry in lockstep and rebuild
    the spike that rate-limited them. With one process it does nothing — write down
    that you are adding it for Session 13's sake, or leave it out honestly.

- [x] **Step 4 — Which failures get a second chance.** _(`retry.plan_retry`, five checks in a fixed order.)_
  - Build: D65, D68 and D69. The two axes are already on every `ModelError`.
  - Test: a rate limit retries; a 500 retries; an auth failure does not; a
    `bad_request` does not; a read timeout does whatever D69 says, every time.
  - Review: an auth error that retries three times turns one clear message into
    thirty seconds of silence followed by the same message. Retrying the
    unretryable is not caution, it is latency with extra steps.

- [x] **Step 5 — What an attempt costs.** _(not a step, not a tool call; `_seconds_left` refuses a backoff that would not fit.)_
  - Build: D70. Decide, then make the budget enforce it rather than the backoff.
  - Test: a run whose retries exhaust the wall clock stops with the limit's stop
    reason, not with the provider's error.
  - Review: whichever you chose, the report has to name it. "Stopped after 30s" and
    "stopped after 30s, 27 of them waiting to retry" are different facts about your
    agent, and only the second one tells you what to fix.

- [x] **Step 6 — What the trace says about attempts.** _(`model_retry` event, written before the sleep, on every attempt including the last.)_
  - Build: D71. `MODEL_PENDING` is already written before the POST so a death can
    name the step that may have been billed. An attempt is the same shape of risk,
    one level down.
  - Test: kill a run between attempt 2 and attempt 3; read the journal; say how many
    requests the provider may have billed.
  - Review: if the answer is "between one and three", decide whether that range is
    acceptable or whether the journal owes you a number.

### Phase 2 — The half-done edit

- [x] **Step 7 — Resume meets a write.** _(`agent/replay.py`; `before` on every
  writing `tool_intent`; `_Run._interrupted`; `crash_demo.py --edit` now resumes.)_
  - Build: D72 and D73. `crash_demo.py` already has `--edit before` and
    `--edit after`, built in Session 6 precisely because they are indistinguishable
    on paper. Make them distinguishable, or make the distinction unnecessary.
  - **Made distinguishable.** The intent record now carries a fingerprint of the
    file taken a moment before dispatch is entered, so the two journals differ in
    the one place that matters: one describes a file that still matches, one does
    not. `--edit` ends both runs with a resume, and both end the same way:
    `file fixed = True, run reported it = True`.
  - Test: both crash points, then `--resume`, then check the file **and** the
    report. Four outcomes to enumerate: edit landed and is reported, edit landed and
    is not, edit did not land and is retried, edit did not land and is lost.
  - **The four, and what produces each:** *landed and reported* ← crash after the
    write, resumed (`tests/test_replay.py::test_the_edit_landed_and_is_reported`);
    *did not land, retried* ← crash before the write, resumed; *landed and NOT
    reported* ← a naive replay, kept as a test of the TOOL rather than the agent
    (`test_outcome_two_is_what_a_naive_replay_produces`); *did not land and lost* ←
    no journal at all, which is Session 5's starting point.
  - Review: "the agent said it changed nothing and the file changed" is the worst of
    the four, and it is the one a naive replay produces.
  - **A fifth thing the review found.** `changed_paths` was a list in one process,
    so a resumed run reported "changed nothing" about a file its first process had
    edited - the same false claim by a different route. It is now rebuilt from the
    `changed_path` already recorded on every `tool_outcome`. That also repaired
    D69: a resumed run used to think no tool had written, and would have resent a
    request that is not safe to send twice.

- [x] **Step 8 — Prove the non-idempotence.** _(`crash_demo.py --replay`;
  `tests/test_replay.py::test_a_zero_match_refusal_cannot_name_its_own_cause`.)_
  - Build: nothing. Apply an edit, then replay the identical call.
  - Test: the second call returns the zero-match refusal.
  - **Proven, and worse than expected.** The replayed edit and an anchor the model
    simply invented produce **byte-identical** results - same code, same sentence -
    so no amount of reading the tool result can separate them. And when `new`
    contains `old` there is no refusal at all: `FEE = 10` replayed once becomes
    `FEE = 10 + 5 + 5`, a success that is wrong with no error anywhere.
  - Review: write the sentence out. *A zero-match refusal after a crash means either
    the model was wrong or the edit already succeeded, and the tool cannot tell
    you which.* Anything you build in D73 exists to break that ambiguity.
  - **Why the fix could not live in the tool.** The difference between those two
    worlds is in the past, not in the file, so no amount of looking at the file
    afterwards recovers it. Only a record made *before* the call can - which is why
    D73 is a journal decision and why Session 6 was right to leave it open.

### Phase 3 — The failures that already work, and what they cost

- [x] **Step 9 — The tool layer, under load.** _(`LoopResult.tool_errors`,
  `repeated_failures`; the tally in `report_totals`.)_
  - Build: D74 and D75. The results are already correct; what is undecided is the
    accounting.
  - Test: a response containing one good call and one call to a tool that does not
    exist. Then five consecutive malformed calls.
  - **Measured:** the unknown tool costs one tool call and is answered with the
    menu; five malformed calls cost five tool calls, produce five correct refusals,
    and end the run **not at all**. `repeated_failures` reports 4 of the 5 as
    byte-identical resends. With `max_tool_calls=3` the run stops at 3 - the
    tool-call cap is the only thing in the agent that bounds this.
  - Review: if a hallucinated tool name costs nothing, a model can loop on it until
    the token budget dies. If it costs a step, five mistakes end a five-step run.
    Session 9 has the proper answer; this step is where you notice you need one.
  - **Noticed.** A batch of five mistakes costs five tool calls and one step, so the
    step limit does not see it at all. The honest statement of today's position is
    printed rather than enforced: *nothing stops that except the tool-call limit*.

- [x] **Step 10 — The provider's own malformed output.** _(Nothing built, as
  predicted; four tests added.)_
  - Build: nothing, probably. `LoopResult.malformed_call_ids` already collects calls
    with a missing or duplicated id, and `report_totals` warns about them.
  - Test: a response with two calls sharing an id, and one with no id at all.
  - Review: confirm the warning still fires when the run *also* hits a limit. A
    diagnostic that only prints on the happy path is a diagnostic you do not have.
  - **Confirmed under a step limit**, which is the stop that matters: both calls are
    answered, the warning prints, and the tool-error tally prints with it
    (`test_the_malformed_warning_fires_on_a_limit_stop`). A clean run prints
    neither, so the line means something when it appears.

> **Review checkpoint.** Full suite green, no API key, no network. Then one real
> `crash_demo` run end to end, because the whole point of a failpoint in the real
> code path is that the real code path is what runs.
>
> **Done:** 455 tests, no key, no network, 2.1s. `crash_demo --edit` runs both
> crash points as real subprocesses that really `os._exit(137)`, and `--replay`
> runs the non-idempotence proof through the real `dispatch`.

### Phase 4 — Measured failure

- [x] **Step 11 — Faults against the fixtures.** _(`scripts/fault_matrix.py`: 3
  cases × 4 crash points = 12 runs, scripted model, no network.)_
  - Build: nothing new. Session 7's harness builds a workspace, Session 8's
    failpoints kill the run, `scripts/fixture.py verify` grades what is left.
  - Test: for each fixture case, kill the run before the edit, during the edit and
    after the edit, then verify. Record the verdict and whether the *report* agreed
    with it.

```
case          crash          fixed?  run said  replay   resume said     fixed?
tail_window   none           PASS    changed   -        stats.py        PASS
tail_window   before_write   FAIL    -         replay   stats.py        PASS
tail_window   after_write    PASS    -         changed  stats.py        PASS
tail_window   after_outcome  PASS    -         -        stats.py        PASS
```

  (`loud_comment` and `twice_over` produce the same four rows.)

  - **The gap, in one number: 6 of 12 runs were killed AFTER fixing the bug, and
    not one of them said so.** The verifier graded every one as fixed. "run said"
    is a dash on every killed row because there is nothing to put there - a dead
    process prints no totals and returns no `LoopResult`.
  - Review: the verifier's job here is not to pass. It is to be *right* — a killed
    run that fixed the bug should verify as fixed, and the gap between that and what
    the run told you is the finding.
  - **Deliberate deviation:** the matrix scripts the model. Nothing being measured
    is the model's judgment, and a scripted model makes the crash point the only
    variable - the Session 7 method, applied to a different question. The live half
    is below, because one thing genuinely needs a real provider.

**The live run.** A real model, a real fixture, killed between `os.replace`
returning and the outcome record landing — the window no scripted test can
make more real, but whose *aftermath* only a real model can be measured on:

```
  step 1-3: list_folders, list_files, 3 × read_file
  step 4:   replace_text  >>> plug pulled, edit on disk, no outcome record <<<

  $ python -m agent.cli --tools --workspace=... --resume=20261115T123059-c30806
    in flight: replace_text on stats.py -> already applied, not repeated
    → ok | This call was interrupted by a crash, and stats.py has changed since
         | it started: the edit is treated as applied and was NOT repeated.
    step 5: read_file stats.py          <- it checked, instead of editing again
    step 6: "The fix is in place. ... changed `return values[-n + 1:]` to
             `return values[-n:]`"
  changed 1 file(s): stats.py
  $ python scripts/fixture.py verify tail_window --workspace=...   ->  PASS
```

  The model did the one thing the message was written to make it do: it read the
  file rather than re-editing it, then reported the change as its own. The
  verifier agrees with the report. That sentence was not true of any killed run
  before this session.

- [x] **Step 12 — Answer self-test #8 with evidence.** _(Printed by
  `fault_matrix.py` from the rows it just produced, not from memory.)_
  - Build: nothing.
  - Test: for each of the four signals, say whether it survives a kill between the
    tool call and the tool result.

| Signal | Survives a kill? | Why |
|---|---|---|
| the verifier's verdict | **yes** | it reads the filesystem and was never told the run existed |
| the diff against the template | **yes** | same bytes, same reason |
| `changed_paths` | **no** | a list in a dataclass in a process that no longer exists — recoverable *only* because every `tool_outcome` records `changed_path`, which is what a resume now rebuilds it from |
| the final message | **never existed** | the model was never asked for it; there is nothing to recover |

  - Review: `changed_paths` lives in a dataclass in a dead process. The final
    message was never generated. The diff and the verifier read the disk, which is
    the only thing that outlived the run — which is the argument for why the
    evaluator was built to read the filesystem in the first place.
  - **The two that survive are the two that never belonged to the agent.** That is
    not a coincidence and it is the whole case for Session 7's design: any signal
    the agent produces about itself is a claim, and a claim needs a live process to
    make it. The disk needs nobody.

---

## C. The failure table

| What happens | Expected | Notes |
|---|---|---|
| 429 with `Retry-After: 2` | wait, retry, succeed | D67 — honour it or say why not |
| 500, then success | one retry, run continues | the easy case |
| 500 forever | bounded attempts, then an explicit failure | the plan's words: *explicit failure if exhausted* |
| connect timeout | retried; nothing reached the server | `safe_to_retry=True` |
| read timeout after an edit landed | whatever D69 says, the same way every time | the one you cannot know |
| 401 / `bad_request` | no retry, exit immediately | retrying the unretryable is latency |
| retries exhaust the wall clock | a limit stop, naming the retries | D70 |
| crash between `tool_intent` and `tool_outcome` on an edit | file is entirely old or entirely new | holds; `half-written = False` at both crash points |
| `--resume` meets an in-flight edit | D72 — and the report says which it chose | `in flight: replace_text on stats.py -> already applied, not repeated` |
| replayed edit finds zero matches | distinguishable from a model mistake | **not from the result** — byte-identical to an invented anchor. Distinguished by the fingerprint, before the fact |
| unknown tool name | `unknown_tool`, the menu named | true; D74 — it costs one tool call |
| arguments are not JSON | `invalid_arguments`, nothing opened | already true |
| two calls sharing one id | both answered in order, warning printed | `malformed_call_ids` |
| killed run that had already fixed the bug | verifier says fixed, report said nothing | the gap worth measuring |

---

## D. Brainstorming space

**Scope decision taken before Phase 0:** no new tool, and no new *tool* behaviour.
Retrying is a property of the transport and the loop, not of `replace_text`. If the
answer to D72 seems to need a flag on the tool, that is a sign the decision belongs
in the journal instead.

Things I haven't decided:

- ~~D65 through D76.~~ All twelve are closed and implemented.
- ~~Whether `crash_demo.py` grows a fixture mode or Phase 4 gets its own driver.~~
  **Its own driver.** `scripts/fault_matrix.py` prints rows; `crash_demo.py` keeps
  printing prose. They are different arguments to different readers, and merging
  them would have made both worse — the demo exists to be understood once, the
  matrix to be re-run and compared.
- Whether "attempts" belong in `Budget.describe()`, which is currently the one
  printable statement of every rule a run is under. **Still open**, and the banner
  prints `retry:` on its own line as a holding answer.

Things that surprised me:

- **The two refusals are byte-identical.** I expected a replayed edit to be
  *recognisable* from the result with enough care — a different sentence, a
  different code, something. There is nothing: `no_match` after your own successful
  edit and `no_match` from an anchor the model invented are the same object. That
  closed the "be smarter about reading the error" route completely and is what sent
  the decision to the journal.
- **The dangerous case is the one that does NOT error.** A replay whose `new`
  contains its own `old` succeeds twice and the file is quietly wrong. The
  refusal is the *safe* failure; the success is the dangerous one.
- **D52 was quietly half-broken.** `changed_paths` was per-process, so a resumed run
  reported "changed nothing" about a file its own first process had edited. Phase 4
  found it by pointing the verifier at the report — the gap it was built to measure
  turned out to include one of mine.
- **That bug reached into D69.** A resumed run believed no tool had written, so the
  retry policy would have resent a request that is not safe to send twice. Two
  sessions' decisions, one shared fact, and the fact was only recorded in RAM.
- **The live model did the right thing on the first try.** Told its edit had already
  applied, it read the file to check rather than editing again, then reported the
  change as its own. I had expected to have to tune that sentence.

Questions for review:

- `safe_to_retry` is set at the point the exception is translated. Is it ever wrong
  in the unsafe direction — a case marked safe where the request *did* arrive?
- If the backoff lives inside `call_model`, the loop cannot see attempts at all and
  the journal records one event for three requests. If it lives in the loop, every
  caller of `call_model` has to implement it. Which cost is the one I want?
- Does `agent/chat.py` inherit the retry, and should an interactive session wait
  thirty seconds without saying anything? **Still no, deliberately.** `chat.py`
  builds its own `run_loop` call and passes no policy, so it gets
  `RetryPolicy.off()` — one attempt, an error on screen, and a human sitting there
  who can press enter again. A thirty-second silent pause in an interactive session
  is indistinguishable from a hang. The counter-argument is real (the human now
  pays for a failure the CLI would have absorbed) and the honest fix is a one-line
  "retrying, attempt 2 of 3" callback, which is Session 10's shape, not this one's.

---

## E1. The invariants, carried forward

> **No rejection may depend on whether the target exists.** (Session 2)
> **No file's contents may change what the agent is permitted to do.** (Session 3)
> **The prompt is not a boundary.** (Session 4)
> **Every tool call gets an answer — including the ones you refuse to run.** (Session 5)
> **A tool that does not succeed changes nothing on disk.** (Session 6)
> **The model's account of what it did is a claim; the workspace is the record.** (Session 7)
> **A retry is an action, not a correction.** (new)

Every invariant before this one is about a single attempt: this call, this file,
this response. The new one is about the second attempt, and it says that sending a
request again is a decision to act on the world again — not a way of asking what
happened the first time.

That is why `safe_to_retry` is a separate axis from `retryable`, and why the two
are so easy to collapse into one. *Should I try again* is a question about the
error. *May I try again* is a question about what the first attempt already did,
and only the second one can cost a user their file.

---

## E. Known limits of this failure handling

- **A read timeout is permanently ambiguous.** No backoff resolves it. The best
  available outcome is at-most-once *or* at-least-once, chosen deliberately and
  written down, never "exactly once" — the provider offers no idempotency key and
  nothing you build here can manufacture one.
- **Retries multiply spend.** Three attempts at a long prompt is three times the
  input tokens for one answer, and the budget only learns about the ones that
  returned.
- **Backoff cannot fix a rate limit you are causing.** One process backing off is
  polite; Session 13's concurrent runs will need a shared limiter, which is a
  different mechanism entirely.
- **Failpoints rehearse the deaths you thought of.** `os._exit` models a SIGKILL
  well. It does not model a disk that accepts a write and loses it, a clock that
  jumps, or a container killed between `write` and `fsync` on a filesystem with
  different ordering guarantees than yours.
- **The fixture harness detects a double edit only because fixtures are small.**
  "Changed exactly one file" is cheap to check on four files and meaningless on a
  repository.
- **The fingerprint covers one file, named by one argument.** It is taken from the
  `path` of a tool in `WRITES`, so a future tool that writes two files, or names its
  target something else, is outside this protection. `_check_registries()` refuses
  a writing tool with no `path` at import time, which converts that gap into a
  startup error rather than a silent wrong answer — but a two-file tool would pass
  that check and still be unprotected on its second file.
- **"Changed" is not "we changed it".** Another process editing the workspace while
  a run is dead produces the same verdict. Both readings have the same answer, so
  the agent behaves correctly either way, but the sentence it tells the model
  ("the edit is treated as applied") is a reasonable inference, not a proof.
- **There is still a window.** The fingerprint is taken just before `dispatch` is
  entered, not atomically with the write. A change landing in that window is
  attributed to the edit. The window went from "the whole call" to "a few
  microseconds of function-call overhead", which is a narrowing, not a closing —
  the same honesty D44 already owes about the read-to-rename race.
- **A resumed run inherits the dead run's steps, and the default is five.** The
  live run above stopped at `step_limit` on its first resume, having spent four
  steps before the crash. That is D39 working as designed, and it is also a
  usability edge: the obvious reaction is `--max-steps=8`, and nothing on screen
  says so.

---

## F. Self-test before moving to Session 9

1. A read timeout fires on a step whose previous step edited a file. What does your
   agent do, and which of the two flags on `ModelError` made that decision?
2. Your journal shows `model_pending` at step 3 and nothing after it. How many
   requests may the provider have billed, and what in the trace bounds that number?
3. A replayed `replace_text` comes back with a zero-match refusal. Name the two
   situations that produce that result and say how your code tells them apart.
4. An auth error arrives. How long does the user wait before they see it, and is
   that number a decision or an accident?
5. A run spends 27 of its 30 seconds in backoff and stops. Which stop reason does
   the user see, and does the report mention the retries at all?
6. You kill a run immediately after the write completes but before `tool_outcome` is
   journaled, then resume. What does the user end up believing about their file?
7. Five consecutive malformed tool calls arrive. What ends the run — and if the
   answer is "nothing", which budget are you relying on and is it the right one?

And the one Session 9 will punish a wrong answer to:

8. A backoff retrying three times and a model re-sending the same failing tool call
   three times are the same shape from the outside: three identical requests, no
   progress. Which counter stops which, and what happens when a run does both at
   once?
