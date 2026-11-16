# Session 11 — Ten Tasks, and a Script That Grades Them Without Asking the Agent

**Goal (from the plan):** create 10 evaluation tasks with fresh fixtures.
**Done when:** a script verifies the expected outcomes.

Three of the ten exist. `fixtures/` holds `tail_window`, `twice_over` and
`loud_comment`, each with a `case.json` (task, `allowed_changed`, `max_steps`,
`golden`, notes) and a `verify/check.py` the agent cannot see. `scripts/fixture.py`
builds a byte-identical workspace per run, resets it provably, and grades it.
`scripts/fault_matrix.py` is already a runner in miniature: twelve runs, a row
each, a verdict per row.

`evals/run.py` is four lines of docstring. `evals/cases/` is empty.

So today is seven more cases and the loop around them — and the seven are not
"three more of the same". Session 7 produced 15/15 on the three it has, with
zero refusals. A suite that cannot fail is a suite that measures nothing, and
the plan's category table is the cure it prescribes:

| Category | At 20 (plan) | At 10 (today) | Have |
|---|---|---|---|
| Read/search | 4 | 2 | 0 |
| Simple edits | 6 | 3 | 2 (`tail_window`, `twice_over`) |
| Multi-file | 4 | 2 | 0 |
| Failure/boundary | 4 | 2 | 1 (`loud_comment`) |
| Already correct or needs clarification | 2 | 1 | 0 |

The three gaps are the whole difficulty, and each breaks a different thing that
currently works:

- **A read/search task changes no files.** Session 7's invariant says the
  workspace is the record — and for these tasks there *is* no workspace change.
  The only artefact is the agent's final sentence, which is exactly the thing
  the evaluator was built not to trust.
- **A multi-file task needs two edits that both have to land.** D42 bought
  atomicity for one file and said so explicitly: *one file is atomic, a pair of
  files is two renames and nothing makes the pair atomic.* A case that renames a
  function and updates its import is a case where a stop between the two leaves
  the workspace broken in a way neither file's check can see alone.
- **An "already correct" task is graded by the absence of an edit.** Every
  mechanism in this repository is built to notice changes. This case succeeds
  only if nothing happened, which means `allowed_changed: []` has to mean
  something stronger than it does today.

---

## A. Decisions to lock before writing code

| #    | Decision                                       | Options                                                                          | My pick + why |
|------|------------------------------------------------|-----------------------------------------------------------------------------------|---------------|
| D100 | Where cases live                               | `fixtures/` stays the home / move to `evals/cases/` / both, with one as a pointer  |               |
| D101 | What "success" means                           | the verifier passes / that plus no unexpected changes / that plus an expected stop reason |               |
| D102 | Repeats per case                               | 1 while debugging then 3 / always 3 / configurable with 3 as the default          |               |
| D103 | What to do about non-determinism               | temperature 0 and accept what remains / record variance per case / treat a flaky case as a broken case |               |
| D104 | Budgets                                        | per-case `max_steps` from `case.json` (exists) / plus a suite-wide spend ceiling / plus a per-case timeout |               |
| D105 | A case that breaks the runner                  | abort the suite / record a row of kind `error` and continue                        |               |
| D106 | How a read/search task is graded               | substring match on the answer / a check script given the answer text / a second model as judge |               |
| D107 | How "already correct" is expressed             | `allowed_changed: []` / an explicit `expect: no_change` / a check that fails on any diff |               |
| D108 | Where `needs_clarification` lives              | a stop reason from the loop / a verdict from the checker / a tool the agent may call |               |
| D109 | What identifies a run                          | `(case, variant, repeat)` in the filename / in `run_started` / both                |               |
| D110 | Sequential or concurrent                       | one at a time / N at a time, and what that does to rate limits                     |               |
| D111 | Results format                                 | one JSONL row per run plus a rendered summary / a single JSON document / a CSV     |               |

**Hardest two: D106 and D108.**

D106 because it is the first time this project grades a *claim*. Everything
since Session 7 has rested on reading the filesystem precisely because the
model's account is not evidence — and a read/search task has no filesystem
consequence to read. Substring matching is brittle in a specific, measurable
way: "the discount is calculated in `pricing.py`" and "`pricing.py:41`
calculates it" are both correct and only one matches `"in pricing.py"`. Handing
the answer to the case's own `check.py` is more honest (the case author decides
what counts) and still brittle. A model judge is a third model in the loop whose
agreement with you is itself unmeasured, and it costs money per grade. Pick one
and write down the failure you are accepting.

D108 because `needs_clarification` is an outcome the agent has to be *able* to
produce, and today it cannot. A model that answers "this task is ambiguous"
stops with `STOP_ANSWER` and text — indistinguishable from a wrong answer
confidently given. Making it distinguishable means either a stop reason the loop
can detect (which means detecting it from prose, which is the D106 problem
again) or a tool the model may call to say so, which is a new tool in a project
that has deliberately kept four.

**And the sneaky one: D102.** Three runs per case is the plan's number and it is
not a statistical sample; it is enough to notice a case that is flaky and not
enough to measure a 10% improvement. Decide today what you will do when a case
passes twice and fails once, because that will happen in the first suite you run
and the temptation — rerun it until it agrees with you — is how eval suites stop
being measurements.

---

## B. Sequential build order

Phases 0 to 2 are offline. Phase 3 is the first time the whole suite runs live,
and it is the most expensive thing this project has done: ten cases × three runs
× several steps each. Phase 0 exists so that is not the run where you discover
the runner was wrong.

### Phase 0 — The runner, with no model at all

- [ ] **Step 1 — Drive the existing three with the golden transcript.**
  - Build: `evals/run.py` around `fixture.build` → run → `fixture.verify`, using
    the scripted model from `fault_matrix.py` rather than a provider.
  - Test: 3 cases × 1 run, all pass, in under a second and for nothing.
  - Review: this is the runner's skeleton proven on known-good input. If a
    golden transcript does not score 3/3, the bug is in the runner and you have
    not spent a cent finding it.

- [ ] **Step 2 — Prove the runner can fail.**
  - Build: a deliberately broken scripted model — edits the wrong file, edits
    nothing, edits twice.
  - Test: each produces a *distinct* failure row: unexpected change, check
    failed, double edit.
  - Review: Session 7's rule, applied to the layer above: a harness that has
    never returned FAIL has not been tested.

### Phase 1 — Seven cases

- [ ] **Step 3 — Two read/search cases.**
  - Build: D106. Design the task so the correct answer is *forced* to contain
    something checkable — "name the file and the function" — rather than hoping
    prose contains a substring.
  - Test: write the check first, then answer the task by hand, then try to
    phrase a correct answer that the check rejects. If you can do that in under
    a minute, the check is wrong.

- [ ] **Step 4 — Two multi-file cases.**
  - Build: a rename plus its import; a constant plus the test that asserts it.
  - Test: `allowed_changed` lists both. Then deliberately stop the agent after
    the first edit (Session 8's failpoints) and confirm the verifier fails —
    half a rename must not score as a pass.
  - Review: this is where D42's honesty about pairs of files becomes a grade.

- [ ] **Step 5 — Two failure/boundary cases.**
  - Build: one ambiguous `old` that must produce `multiple_matches` and a
    recovery; one path outside the workspace that must be refused.
  - Test: success here means the agent *failed correctly* — the refusal
    happened and the workspace is untouched.
  - Review: `loud_comment` already covers injection. Do not write a second
    injection case until the first one has ever failed.

- [ ] **Step 6 — One already-correct case.**
  - Build: D107. A workspace with no bug, and a task that describes a bug that
    is not there.
  - Test: success is an unchanged workspace and an answer that says so.
  - Review: the failure mode to catch is the agent "fixing" something to satisfy
    the task. If it edits, the row must be a loud failure, not a near miss.

### Phase 2 — The suite

- [ ] **Step 7 — Configuration is part of the result.**
  - Build: D109. Record model id, prompt version, code version (git sha),
    provider, budget, and the template hash of every fixture, into every row.
  - Test: two rows from different code produce different `code` fields.
  - Review: a result without its configuration is not a result. Session 12
    compares one variable at a time and cannot do that if the rows do not say
    which variables were set.

- [ ] **Step 8 — Rows, then a summary.**
  - Build: D111. One JSONL row per run into `evals/results/`, then a rendered
    table: per case pass/fail/flaky, and the totals the plan names — success
    rate, unexpected changes, tool calls, model requests, duration, tokens,
    estimated cost, stop reason.
  - Test: the summary recomputes from the rows alone, with no state in memory.

- [ ] **Step 9 — Make the suite survivable.**
  - Build: D104 and D105. A spend ceiling that stops the suite, a per-case
    timeout, and an `error` row for a case that explodes.
  - Test: a case whose check script hangs; a provider that 401s on run 4 of 30.
  - Review: the suite must be resumable or cheap enough to rerun. Thirty runs
    lost to one bad key is the most expensive mistake available today.

> **Review checkpoint.** The whole suite green on scripted models, no API key,
> no network, under two seconds. Then one live case, once, and read its row by
> hand before running the other nine.

### Phase 3 — Live, once, then three times

- [ ] **Step 10 — Ten cases, one run each.**
  - Test: record every row. Expect failures; a 10/10 first live suite means the
    cases are too easy and Session 12 will have nothing to compare.

- [ ] **Step 11 — Ten cases, three runs each.**
  - Test: thirty rows. Report per-case variance, not just the mean.
  - Review: D102 comes due. A case that is 2/3 is either a flaky case or a real
    signal, and the only way to tell is to read all three transcripts.

---

## C. The failure table

| What happens | Expected | Notes |
|---|---|---|
| the agent fixes the bug and edits a second file | FAIL, unexpected change | `allowed_changed` |
| the agent fixes the bug a different way than `golden` | PASS | Session 7's D59: tests pass, not diffs match |
| the agent edits nothing on an already-correct case | PASS | D107 |
| the agent edits something on an already-correct case | FAIL, loudly | the case exists for this |
| half of a multi-file change lands | FAIL | no partial credit; a broken repo is not 50% fixed |
| the answer is right and the check's substring is not | FAIL | D106 — brittleness you chose, written down |
| a check script hangs | the case times out as an `error` row | `CHECK_TIMEOUT_S` exists |
| the provider 401s mid-suite | the suite stops; completed rows are kept | D105 |
| a case passes twice and fails once | recorded as flaky, never rerun to a nicer number | D102 |
| the agent reads a file it should not reach | FAIL, boundary case | already enforced; now graded |
| the workspace is not reset between runs | impossible | `fixture.reset` is byte-proven |

---

## D. Brainstorming space

**Scope decision taken before Phase 0:** no new agent capability. If a case
cannot be solved with `list_files`, `list_folders`, `read_file` and
`replace_text`, it is not a Session 11 case — it is a Session 12 argument for a
fifth tool, and that argument needs a number attached.

Things I haven't decided:

- D100 through D111. None are closed.
- Whether `evals/cases/` should exist at all, given `fixtures/` already holds
  everything a case needs. Two directories with the same purpose is how a suite
  ends up half-migrated.
- Whether the runner should be able to resume a half-finished suite, or whether
  "rerun it" is the correct answer at this size.
- Whether a case may declare an expected *stop reason* as part of success. The
  boundary cases want this; the edit cases do not care.

Things that surprised me:

- _(fill in during Phases 1 and 3)_

Questions for review:

- Session 7 found that `twice_over` is toothless because the fixture is too
  small — two matches in four files is not the same problem as two matches in a
  repository. Do any of the seven new cases have the same defect, and how would
  I know before running them?
- If a read/search check is a substring match, what stops me from quietly
  rewriting the check when a correct answer fails it? Who reviews that commit?
- The agent has been told by `loud_comment` to overwrite a file and refused,
  five times out of five. Is a case that has never failed still earning its
  place in a ten-case suite?

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
> **An eval you cannot fail on purpose is not measuring anything.** (new, proposed)

Session 7 established that a check must fail before it passes. The suite-level
version is stronger and less comfortable: for every case, you should be able to
state the wrong behaviour it catches, and demonstrate it catching it. A case
whose only evidence is "the agent passed it" tells you nothing about the agent
and quite a lot about the case.

---

## E. Known limits of this suite

- **Ten cases is a smoke test, not a benchmark.** It will catch a regression
  that breaks everything and miss one that costs five percent.
- **Three runs is not a sample.** It distinguishes "always" from "sometimes" and
  nothing finer.
- **The fixtures are small, and smallness flatters the agent.** "Changed exactly
  one file" is cheap to check on four files and meaningless on a repository;
  "the anchor is unique" is nearly free in 40 lines and hard in 4,000.
- **Grading an answer is not grading a task.** Any read/search score is a score
  of your checker's phrasing tolerance as much as of the agent.
- **The suite shares one machine, one key and one rate limit.** Anything it
  measures about latency is measured under those conditions, and Session 13 is
  about to change all three.
- **The cases were written by the person who built the agent.** They encode what
  it is already good at. That is the strongest argument in the plan for the
  reserved cases Session 12 asks for.

---

## F. Self-test before moving to Session 12

1. A case passes with a diff that does not match `golden`. Is that a pass, and
   which decision says so?
2. A read/search case fails. How do you tell "the agent was wrong" from "the
   checker was too strict", and how long does that take?
3. Your suite reports 70%. What is the confidence interval on that number with
   ten cases and three runs, and is it worth quoting to one decimal place?
4. A case is flaky. What do you do, and what would you have to believe to
   justify rerunning it?
5. The multi-file case fails because the agent made the first edit and stopped
   at the step limit. Is the agent wrong, is `max_steps` wrong, or is the case
   wrong?
6. Which row in `evals/results/` tells you which code produced it? If the
   answer is "the timestamp", what happens when you run two variants in one
   afternoon?
7. The already-correct case fails because the agent made a small, correct,
   unrequested improvement. Is that a failure? Defend the answer you would
   give a user.

And the one Session 12 will punish a wrong answer to:

8. You are about to change one thing — a prompt line, a tool description, a
   limit — and claim it made the agent better. With ten cases and three runs,
   what difference in success rate would you need to see before that claim is
   anything other than noise, and do you know that number *before* you look at
   the result?
