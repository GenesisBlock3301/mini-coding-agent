# Session 5 — Many Calls in One Turn, and Every Way a Run Can Stop

**Goal (from the plan):** handle multiple tool calls per response and stopping rules.
**Done when:** every result matches its tool-call ID; the agent stops at its step limit.

Read the completion criteria against the code you already have and the first half is
done. `agent/loop.py` iterates `response.tool_calls` in `_execute_calls`, addresses
each result with `call.get("id")`, and `STOP_STEP_LIMIT` is an explicit failure the
CLI turns into exit code 11. Session 4 overshot its own milestone.

So this session is the half that is missing, and it is the more interesting half.

A step limit is one limit. The plan's **Limits to Implement** list asks for four:
model steps, tool calls, output size, and *duration* — plus token usage and an
estimated spend checked **before** the next request, "because the next response may
consume tokens before its actual usage is known." Today `run_loop` knows about
`max_steps` and nothing else. Five steps can mean fifty tool calls. Five steps
against a 60s read timeout is a five-minute run nobody capped. `LoopResult` already
sums `total_tokens` across the run and no code anywhere reads it.

And a budget that can fire *mid-batch* is what finally makes the multi-call path
dangerous, because stopping in the middle of a batch of tool calls is how you
corrupt a conversation.

---

## A. Decisions to lock before writing code

| #   | Decision                                   | Options                                                      | My pick + why |
|-----|--------------------------------------------|--------------------------------------------------------------|---------------|
| D31 | Where the limits live                      | kwargs on `run_loop` / one `Budget` object / module constants |               |
| D32 | What the tool-call cap counts              | per step / per run                                            |               |
| D33 | A batch where the budget dies at call 2of3 | abort the rest / run them all / answer without running        |               |
| D34 | Two calls in one batch with the same id    | reject the batch / answer both / de-duplicate                 |               |
| D35 | When the budget is checked                 | after the response / before the request / both                |               |
| D36 | Headroom for the unknown next response     | none / a fixed reserve / estimate from history                |               |
| D37 | Where token prices come from               | hard-coded / env / the provider's API                         |               |
| D38 | A spend limit with no pricing configured   | ignore it / treat as unlimited / usage error                  |               |
| D39 | Does the wall clock survive a `--resume`   | yes, one deadline per run / no, per process                   |               |
| D40 | What the CLI exits with per limit          | one code for "stopped" / one per stop reason                  |               |

**Hardest two:** D33 and D35.

D33 because the provider's contract is not negotiable: *every* `tool_call.id` in an
assistant turn must be answered by a `role="tool"` message before the next request,
or the POST comes back 400. So "abort the rest of the batch" is not a stopping rule,
it is a corrupted conversation — and the corruption surfaces one step later, in a
provider error that says nothing about budgets. The only safe shape is: answer every
call in the batch, then stop. A refusal *is* an answer.

D35 because checking afterwards is a bill you have already paid. You learn a
response's token usage from the response, which means the only moment you can refuse
to spend is before the POST — using what you have spent so far plus a reserve for
what this next call might cost. That reserve is D36, and getting it wrong in the safe
direction means stopping slightly early. Getting it wrong in the other direction
means the limit is decoration.

---

## B. Sequential build order

All of Phase 1 and Phase 2 is testable with **no API key and no network** — the
scripted `model_fn` from Session 4 step 0 is what makes that true. Live runs are
Phase 3, and by then there is nothing left to discover.

### Phase 1 — The batch, properly

- [ ] **Step 1 — A batch of three, one of each.**
  - Build: nothing yet. A scripted response carrying three tool calls — one valid,
    one with `arguments` that are not JSON, one naming a tool that does not exist.
  - Test: history ends with exactly three `role="tool"` messages, whose
    `tool_call_id`s equal the three call ids, **in order**.
  - Review: count the tool messages before you read them. A loop that answers two
    calls out of three passes every assertion you wrote about *content*.

- [ ] **Step 2 — The duplicate id.**
  - Build: D34.
  - Test: a batch with the same id twice, and a batch with a call carrying no id.
  - Review: you cannot invent an id — the provider matches on the one it sent. So
    this is not a bug you can fix, only one you can make visible. Decide what the
    trace says, and make sure the run does not silently drop a call.

- [ ] **Step 3 — Ordering is a promise.**
  - Test: a batch of three valid reads produces tool messages in call order.
  - Review: read-only tools make this feel academic. Write it down now anyway —
    Session 6 adds `replace_text`, and two edits applied in the wrong order is a
    file that no test you have written will catch.

### Phase 2 — Stopping rules

- [ ] **Step 4 — One budget object.**
  - Build: D31. Steps, tool calls, seconds, tokens, spend, headroom — one frozen
    value, constructed from flags and env, defaulted so existing callers keep working.
  - Test: the defaults match today's behaviour exactly.
  - Review: every field is a number you must be able to justify in Session 12 when
    you compare a change against a baseline. A limit you cannot name the units of is
    a limit you will tune by vibes.

- [ ] **Step 5 — A ledger.**
  - Build: the running totals — steps taken, tool calls made, tokens in and out,
    elapsed seconds, estimated spend.
  - Test: totals after a scripted two-step run are exactly what the script spent.
  - Review: keep it separate from the Budget. One is the rule, the other is the
    reading. Session 11 reports the reading; Session 12 changes the rule.

- [ ] **Step 6 — The gate, before the POST.**
  - Build: D35 + D36. One function: given budget and ledger, return the stop reason
    that applies *now*, or `None`.
  - Test: a token budget of 500 with 480 spent and a 100-token reserve stops
    **without making a model call**. Assert on the call count of your fake model —
    that is the only assertion that proves it.
  - Review: order the checks and write down why. Steps before tokens? Time before
    spend? When two limits are blown at once, the one you report is the one that
    shows up in Session 12's report as the reason the agent failed.

- [ ] **Step 7 — The mid-batch stop.**
  - Build: D33. When the tool-call cap or the deadline falls in the middle of a
    batch, every remaining call still gets a `role="tool"` answer — a refusal
    carrying its error code — and the run stops after the batch, not inside it.
  - Test: a 3-call batch with 1 tool call left in the budget produces 1 real result
    and 2 refusals, three ids, and a `tool_limit` stop.
  - Review: the refusal text goes to a model that may never read it (the run is
    ending). Write it as if it will, because on a `--resume` it is exactly what the
    model wakes up to.

- [ ] **Step 8 — Pricing.**
  - Build: D37. `.env.example` already reserves `PRICE_INPUT_CACHE_HIT_PER_M`,
    `PRICE_INPUT_CACHE_MISS_PER_M`, `PRICE_OUTPUT_PER_M`. Nothing hard-coded.
  - Test: cache-hit and cache-miss input are priced differently and both are counted;
    unset prices produce `None`, not `0.0`.
  - Review: `0.0` and "I don't know" are different numbers. One of them lets a spend
    limit pass silently forever — which is D38.

- [ ] **Step 9 — Budgets survive a crash.**
  - Build: the journal already records `usage` on every `model_result`. Sum it on
    `rebuild`, and start the resumed run's ledger from there.
  - Test: a resumed run with the token budget already spent stops immediately,
    without a model call.
  - Review: D39. A run's token budget belongs to the *run*; restarting the process
    must not hand it a fresh wallet. The wall clock is the deliberate exception —
    say why in one line.

> **Review checkpoint.** Everything above is green under `pytest`, with no API key
> and no network. Count the model calls in every budget test: a limit that stops the
> run *after* paying is the bug this whole session exists to prevent.

### Phase 3 — Live

- [ ] **Step 10 — Surface it.**
  - Build: D40. A flag per limit, an exit code per stop reason, and totals that
    include tool calls, elapsed seconds and estimated cost. No limit stop writes to
    stdout — D28's rule, extended to every new way of stopping.
  - Test: a real task with `--max-tool-calls=1`, and one with a spend limit set
    below the cost of a single call.
  - Review: can a caller tell "the model answered" from "the agent ran out of money"
    without parsing English? Session 11's runner has to.

---

## C. The failure table

Everything here must produce a *result* with a named stop reason, never a traceback
and never a 400 on the next request.

| What happens | Expected | Notes |
|---|---|---|
| one response asks for three tools | three results, three matching ids, in order | the happy path |
| call 2 of 3 is malformed JSON | call 3 still runs, all three answered | D33 |
| call 2 of 3 names an unknown tool | call 3 still runs, all three answered | Session 4's `unknown_tool` |
| the tool-call cap falls at call 2 of 3 | calls 2 and 3 answered with a refusal, stop `tool_limit` | never an unanswered id |
| the same id appears twice in one batch | both answered, both visible in the trace | D34 |
| a call arrives with no id | answered with `""`, loudly | provider's bug, your trace |
| the deadline passes between two calls | the batch is finished or refused, stop `time_limit` | a running tool is never interrupted |
| tokens spent + reserve exceeds the budget | stop `token_limit` **before** the POST | assert zero model calls |
| estimated spend exceeds `--max-spend` | stop `spend_limit` before the POST | D35 |
| `--max-spend` set, no prices configured | usage error at startup | D38 |
| the step limit is reached | stop `step_limit`, unchanged | Session 4, D28 |
| a resumed run was already over budget | stops immediately, no model call | D39 |
| any limit stop | nothing on stdout, named reason + exit code on stderr | D40 |

---

## D. Brainstorming space

Things I haven't decided:

-

Things that surprised me:

-

Questions for review:

-

---

## E1. The invariants, carried forward

> **No rejection may depend on whether the target exists.** (Session 2)
> **No file's contents may change what the agent is permitted to do.** (Session 3)
> **The prompt is not a boundary.** (Session 4)
> **Every tool call gets an answer — including the ones you refuse to run.** (new)

The new one is a protocol fact before it is a design preference. An assistant turn
with three `tool_calls` is a promise of three `role="tool"` messages; break the
promise and the next POST is a 400 that mentions nothing about your budget. It also
survives the thing it looks like it contradicts: *stopping* is still allowed at any
time — what is not allowed is stopping with an id left hanging.

Which is why a refusal is written as a tool result and not as an absence. The
absence is unrecoverable; the refusal is just another observation the model can read
on resume.

---

## E. Known limits of this stopping logic

- **The reserve is a guess.** D36 is a fixed number, not a prediction. A single step
  that blows past it still overshoots the budget — by at most one response, which is
  the honest bound, and is why the check is *before* the call rather than instead of
  it.
- **Spend is an estimate, not a bill.** It is `usage × your configured prices`. If
  the price list changes and `.env` does not, the number is confidently wrong.
- **Nothing here stops a loop that is cheap and useless.** Reading the same file
  five times costs almost nothing and achieves nothing; repetition detection is
  Session 9.
- **Output size is capped per tool, not per conversation.** `MAX_BYTES` and
  `MAX_READ_BYTES` bound one result. Forty results still grow the history forty
  times — Session 9 measures that.
- **Sequential execution only.** Still deliberate (Session 4 E), and now also the
  reason the mid-batch stop is simple: there is never a tool running when the
  deadline fires.

---

## F. Self-test before moving to Session 6

1. Why can't the loop stop in the middle of a batch of tool calls?
2. Which limit can only be enforced *before* a request, and what does that cost me?
3. What is the maximum amount I can overshoot a token budget by, and why is it
   bounded at all?
4. Two calls in one batch carry the same id. What does my agent do, and what *can't*
   it do?
5. When two limits are exceeded at once, which one gets reported, and who downstream
   depends on that answer?
6. A run is resumed after a crash. Which of my budgets reset, and can I defend each
   one?

And the one Session 6 will punish a wrong answer to:

7. If a batch contains two edits to the same file and the second is refused by a
   budget, what state is that file in — and does my trace say so?
