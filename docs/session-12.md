# Session 12 — Twenty Tasks, Two Arms, and One Number You Agreed to in Advance

**Goal (from the plan):** expand to 20 tasks and compare one improvement
against a baseline.
**Done when:** a report covering success, latency, usage, and unexpected
changes.

This project has already run this session once, in miniature, and the result is
the reason to take today seriously.

Session 7, Phase 4: I claimed the model's over-wide `old` anchors were caused by
one clause in `replace_text`'s description — *"include enough surrounding lines
to make it unique"*. One variable, fixed protocol, five runs per arm. The
clause came out and **nothing changed**: byte-identical anchors, 5/5 in both
arms. The hypothesis was mine, it was plausible, and it was wrong.

Two lessons came out of that and both scale to today:

- **A null result is a result**, and it is cheap compared to shipping a change
  nobody measured.
- **A saturated baseline has no discriminating power.** 15/15 on three cases
  means an improvement *cannot* show up: there is no room above the ceiling.
  Session 11's ten cases probably have the same problem, and twenty is the
  plan's answer to it.

So today is not "make the agent better". It is: build enough cases that better
is detectable, pick one change, decide in advance what would convince you, and
then let the number be whatever it is.

---

## A. Decisions to lock before writing code

Lock these **before** a single run, and write them into the report header. A
decision made after seeing a result is not a decision, it is a conclusion
working backwards.

| #    | Decision                                     | Options                                                                              | My pick + why |
|------|----------------------------------------------|---------------------------------------------------------------------------------------|---------------|
| D112 | The variable                                 | a prompt line / a tool description / `max_steps` / adding `write_file` / retries on-off / Session 9's threshold |               |
| D113 | Design                                       | paired (same cases both arms) / independent / paired *and* interleaved run order       |               |
| D114 | The primary metric                           | task success rate / success minus unexpected changes / success per dollar              |               |
| D115 | The decision rule                            | "ship if the primary metric improves by ≥ X" / a paired test (McNemar) / effect size with an interval |               |
| D116 | Runs per case per arm                        | 3 (the plan) / 5 / more on cases near the boundary                                     |               |
| D117 | Held-out cases                               | how many of the 20 are reserved from tuning, chosen how, unsealed when                 |               |
| D118 | Flaky cases                                  | counted as failures / excluded with a stated reason / reported as a third outcome      |               |
| D119 | Cost ceiling                                 | a hard stop mid-suite / an estimate up front and a hard stop / a dry run first         |               |
| D120 | What makes the report reproducible           | model id, prompt version, git sha, provider, budget, fixture hashes, seed if any       |               |
| D121 | Publishing a null                            | always, in `evals/results/` with the hypothesis / only if interesting                  |               |
| D122 | Trade-off rule                               | success wins outright / success per token / a named ceiling on latency and spend       |               |

**Hardest two: D115 and D122.**

D115 because it is the one decision that makes the rest honest, and it is the
one you will want to revise at exactly the wrong moment. Twenty cases × three
runs is sixty observations per arm, and the paired structure is what buys the
power: the same case runs in both arms, so the comparison is "how many cases
flipped", not "two percentages that look different". With sixty paired
observations, a change that flips three cases one way and one case the other is
not evidence of anything, and the number that tells you so has to be chosen
before you have a reason to prefer a different one.

D122 because the change most likely to improve success is also the one most
likely to cost tokens — more context, more steps, more retries. The plan asks
for latency and usage in the report precisely so that trade-off is visible, but
visible is not decided. "Success at any price" is a defensible rule. So is
"nothing that doubles spend for under five points". What is not defensible is
picking whichever rule the result happens to satisfy.

**And the sneaky one: D117.** Reserved cases only work if they stay reserved.
The failure mode is not dishonesty, it is drift: you look at a failing held-out
case, you understand it, and now every prompt you write is shaped by it. Decide
how they are stored, who may read the transcripts, and whether a held-out
failure may be used to fix a bug (yes) or to tune a prompt (no).

---

## B. Sequential build order

Phase 0 is free. Phase 1 writes cases. Phase 2 is the expensive one — 120 live
runs — and Phase 3 is where most of the value is, because a report nobody can
reproduce is a blog post.

### Phase 0 — Make the suite able to show a difference

- [ ] **Step 1 — Measure the ceiling.**
  - Build: nothing. Run Session 11's ten cases three times and look at the
    baseline.
  - Test: how many cases pass 3/3? Those cases can only ever get worse; they
    carry no information about an improvement.
  - Review: if eight of ten are saturated, the suite's effective size is two.
    That number, not twenty, is what decides whether today can conclude
    anything.

- [ ] **Step 2 — Add difficulty deliberately.**
  - Build: the ten new cases, aimed at the gap. Bigger files so an anchor is
    genuinely ambiguous; a second occurrence that is *not* adjacent; a task
    whose obvious reading is wrong.
  - Test: the baseline on the new ten should be visibly below 100%.
  - Review: a case that fails for an uninteresting reason — a flaky check, a
    `max_steps` too tight — is noise wearing a difficulty costume. Fix those
    before they enter the suite.

### Phase 1 — Pre-registration

- [ ] **Step 3 — Write the hypothesis down, in the repo, first.**
  - Build: a file in `evals/results/` holding D112–D122 filled in, the date,
    the git sha, and one sentence: *what I expect to happen and why.*
  - Test: it is committed before the first run of Phase 2.
  - Review: this is the Session 7 Phase 4 protocol, written down instead of
    remembered. The experiment that produced a null result there was
    trustworthy because the protocol was fixed before the second arm ran.

- [ ] **Step 4 — Estimate the bill before you spend it.**
  - Build: D119. Twenty cases × three runs × two arms is 120 runs; multiply by
    the mean tokens per run from Session 11's rows and the configured prices.
  - Test: the runner refuses to start if the estimate exceeds the ceiling, and
    stops mid-suite if actual spend reaches it.
  - Review: the ledger and the pricing already exist (Session 5, D38). This is
    one more consumer of them, at suite scale.

### Phase 2 — The comparison

- [ ] **Step 5 — Interleave the arms.**
  - Build: D113. Run A, B, A, B per case rather than all of A then all of B.
  - Test: the run order is recorded per row.
  - Review: providers change under you — load, routing, silent model updates.
    Blocked order confounds the variable with the hour of the day.

- [ ] **Step 6 — Run it, and do not look early.**
  - Test: 120 rows in `evals/results/`.
  - Review: stopping early because arm B "looks better" after nine cases is how
    you get a result that does not reproduce. The ceiling is the budget, not
    the trend.

### Phase 3 — The report

- [ ] **Step 7 — Rows to a table.**
  - Build: per arm — success rate, unexpected changes, tool calls, model
    requests, duration, tokens, estimated cost, stop-reason distribution. Per
    case — the paired outcome: both passed, both failed, A only, B only.
  - Test: every number recomputes from the rows alone.

- [ ] **Step 8 — Apply the rule you wrote in Step 3.**
  - Build: D115, mechanically. The flip counts are the evidence; quote them
    with the test and the interval, not just the two percentages.
  - Review: write the sentence out — *"the change flipped N cases to passing and
    M to failing, which is / is not enough to ship under the rule I set"* — and
    put it at the top of the report.

- [ ] **Step 9 — Report the cost, not just the score.**
  - Build: D122. Tokens and latency per arm, and the trade-off rule applied.
  - Review: an improvement that costs 40% more tokens per task is a different
    product, not a better one, unless you said in advance that it was.

- [ ] **Step 10 — Unseal the held-out cases.**
  - Test: run both arms on the reserved cases. If the effect disappears, you
    tuned to the suite — which is a finding about your process, and the most
    valuable thing today can produce.

> **Review checkpoint.** The report reproduces from `evals/results/` with no
> state in your head: same rows, same table, same conclusion. Hand it to
> yourself in three months and see whether you could rerun it.

---

## C. The failure table

| What happens | Expected | Notes |
|---|---|---|
| arm B wins by 2 points | reported as no detectable difference | sixty paired observations do not resolve 2 points |
| arm B wins on 3 cases, loses on 3 | a null | the paired structure makes this visible; two percentages hide it |
| a case is 2/3 in both arms | reported as flaky, by D118's rule | decided before, not now |
| the provider changes mid-suite | detectable because arms are interleaved | and recorded: model id per row |
| spend hits the ceiling at run 94 | the suite stops; 94 rows survive | partial data with a stated reason beats a cliff |
| a check script was wrong all along | both arms were graded wrong | the paired design limits the damage; rerun both |
| the effect vanishes on held-out cases | the change is tuning, not improvement | the result that justifies reserving them |
| the change improves success and doubles cost | D122 decides, not the number | write the rule first |
| a case's fixture drifted between arms | impossible | byte-identical builds, Session 7 |

---

## D. Brainstorming space

**Scope decision taken before Phase 0:** one variable. The temptation at twenty
cases is to change the prompt *and* add a tool *and* raise `max_steps`, see a
big number, and keep all three. The plan says one variable and Session 7's null
result is the argument: the only reason that experiment was interpretable was
that exactly one clause moved.

Candidate variables, with what each would actually test:

- **A clause in a tool description.** Already tried once, null. Trying a
  different clause is fair; re-trying the same one is not.
- **`write_file`, from the deferred backlog.** The biggest capability change
  available, and the one with the most obvious failure mode: a tool that
  creates files makes "unexpected changes" a much richer metric.
- **Retries on versus off** (Session 8). Should change nothing about success on
  a healthy provider — which makes it an excellent *negative control*, and a
  negative control is how you find out whether your suite can tell two
  identical things apart.
- **Session 9's repetition threshold.** Directly trades a stop against a
  recovery, and the suite has cases on both sides.

Things I haven't decided:

- D112 through D122. None are closed.
- Whether the negative control runs first. It costs a suite's worth of money to
  learn that the suite has no resolution — and that is the cheapest possible
  way to learn it.
- Whether "unexpected changes" should be part of the primary metric or a
  separate gate. A change that improves success while editing one extra file
  per task is not an improvement.

Things that surprised me:

- _(fill in during Phases 2 and 3)_

Questions for review:

- If the negative control (retries on/off) shows a 5-point difference, what does
  that say about every other number in this report?
- How many of the twenty cases are variations of the same underlying skill?
  Three cases that all test "find the unique anchor" are one case with three
  spellings, and they inflate the apparent sample.
- Session 7's experiment was run by the person who proposed the hypothesis, on
  cases they wrote, and reported honestly because the protocol was fixed. What
  is the equivalent safeguard at twenty cases and 120 runs?

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
> **A comparison you did not specify in advance is a story.** (new, proposed)

Every invariant before this one constrains the agent. This one constrains you,
and it is the only one with no code to enforce it: nothing in the repository can
stop you from changing the metric after seeing the result. The enforcement is
that the pre-registration file is committed before the runs and the report cites
its sha.

---

## E. Known limits of this comparison

- **Twenty cases cannot resolve small effects.** With sixty paired observations
  per arm, anything under about ten points is indistinguishable from chance, and
  quoting a one-decimal percentage implies a precision you do not have.
- **The cases were written by the agent's author.** Reserved cases help and do
  not cure it; the whole suite still encodes one person's idea of what a coding
  task looks like.
- **One model, one provider, one week.** Nothing here generalises to another
  model, and a provider-side update can move every number without a commit on
  your side.
- **Success is binary and tasks are not.** A case that is 90% fixed scores zero,
  which is the right call for a grader and a poor description of the work.
- **The report measures the agent *and* the suite.** When a number moves, the
  suite is always a candidate explanation, and the only defence is the negative
  control.
- **Nothing here measures what a user would feel.** Latency per task and spend
  per task are proxies. Whether the agent is pleasant to work with is not on the
  table and should not be claimed.

---

## F. Self-test before moving to Session 13

1. Your primary metric improved by 4 points. How many cases flipped, in which
   directions, and does your rule ship it?
2. The negative control shows a difference. What do you do with the rest of the
   report?
3. A case failed in arm A and passed in arm B. Name three explanations that are
   not "the change worked".
4. Spend hit the ceiling at run 94 of 120. Is the partial result usable, and
   what exactly may you claim from it?
5. Someone asks you to rerun the three flaky cases "to clean up the numbers".
   What is your answer and which decision backs it?
6. Your report says the change costs 35% more tokens for 8 points of success.
   Ship it or not — and was that rule written before or after you saw those
   numbers?
7. Three months from now, can you reproduce this report from the repository
   alone? Name every input you would need and where each one is recorded.

And the one Session 13 will punish a wrong answer to:

8. Every run in this report was sequential, on one machine, with one API key,
   against one workspace at a time. Session 13 runs them concurrently with a
   workspace per request. Which of today's numbers survive that change, which
   become meaningless, and which were only ever true because nothing else was
   running?
