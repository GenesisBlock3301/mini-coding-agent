# Session 11 — Ten Tasks, and a Script That Grades Them Without Asking the Agent

**Goal (from the plan):** create 10 evaluation tasks with fresh fixtures.
**Done when:** a script verifies the expected outcomes.

**Implementation status (2026-11-17):** All phases complete. The live suites passed
10/10 and 30/30. Final regression: 605 tests passed. See
[`evals/session-11-report.md`](../evals/session-11-report.md) for the per-case
ranges and execution evidence.

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
| D100 | Where cases live                               | `fixtures/` stays the home / move to `evals/cases/` / both, with one as a pointer  | Keep `fixtures/` as the only case home; `evals/` holds execution and results. No migration or duplicate case directory. |
| D101 | What "success" means                           | the verifier passes / that plus no unexpected changes / that plus an expected stop reason | Independent checker + no unexpected changes + final `answer` stop. Boundary cases additionally require the specified real refusal in the journal. Any partial multi-file fix fails. |
| D102 | Repeats per case                               | 1 while debugging then 3 / always 3 / configurable with 3 as the default          | Configurable, default 3. Run once for debugging, then freeze the cases and record all three repetitions; no score-driven retries. |
| D103 | What to do about non-determinism               | temperature 0 and accept what remains / record variance per case / treat a flaky case as a broken case | Record pass counts and per-case token/duration ranges. Temperature remains the provider default and is recorded; no claim of deterministic inference. |
| D104 | Budgets                                        | per-case `max_steps` from `case.json` (exists) / plus a suite-wide spend ceiling / plus a per-case timeout | Per-case steps, full worker deadline (120s default), checker timeout (5s default), output cap (2048), and a suite spend ceiling ($1 default). Require prices before hosted runs; reserve a conservative next-request estimate and stop on uncertain spend. |
| D105 | A case that breaks the runner                  | abort the suite / record a row of kind `error` and continue                        | Append and fsync an `error` row and continue for isolated case/checker errors. Stop on auth, bad configuration, exhausted spend reserve, or uncertain billing. Kill the worker process group at its deadline. |
| D106 | How a read/search task is graded               | substring match on the answer / a check script given the answer text / a second model as judge | The case checker receives final answer text on stdin. Read tasks require file and function identifiers; case-insensitive token checks tolerate prose, code formatting and order. This checks named identifiers, not arbitrary semantic correctness. |
| D107 | How "already correct" is expressed             | `allowed_changed: []` / an explicit `expect: no_change` / a check that fails on any diff | `allowed_changed: []` is enforced by comparing the entire workspace with its template, plus a checker requiring a no-change explanation. No file edits or unrequested improvements are accepted. |
| D108 | Where `needs_clarification` lives              | a stop reason from the loop / a verdict from the checker / a tool the agent may call | No new tool or loop stop reason. Choose an already-correct task for this slot; any future clarification verdict belongs to its checker, not prose detection in the agent loop. |
| D109 | What identifies a run                          | `(case, variant, repeat)` in the filename / in `run_started` / both                | Unique suite/case/mode/repeat/nonce run ID, journal `run_started.evaluation` metadata, and result-row identity. Record requested and returned model, provider, endpoint (without credentials), prompt/tool digests, code SHA plus local source digest, budget, fixture/spec/checker hashes and deadlines. |
| D110 | Sequential or concurrent                       | one at a time / N at a time, and what that does to rate limits                     | Sequential fresh workspaces; no concurrency or extra retry layer. The live agent uses `RetryPolicy.off()` and all attempts are retained. |
| D111 | Results format                                 | one JSONL row per run plus a rendered summary / a single JSON document / a CSV     | Append-only JSONL under `evals/results/`, flushed and fsynced per row. Summary opens those rows again; standalone `--summary` requires no provider or key. |

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

- [x] **Step 1 — Drive the existing three with the golden transcript.**
  - Build: `evals/run.py` around `fixture.build` → run → `fixture.verify`, using
    the scripted model from `fault_matrix.py` rather than a provider.
  - Test: 3 cases × 1 run, all pass, in under a second and for nothing.
  - Review: this is the runner's skeleton proven on known-good input. If a
    golden transcript does not score 3/3, the bug is in the runner and you have
    not spent a cent finding it.

- [x] **Step 2 — Prove the runner can fail.**
  - Build: a deliberately broken scripted model — edits the wrong file, edits
    nothing, edits twice.
  - Test: each produces a *distinct* failure row: unexpected change, check
    failed, double edit.
  - Review: Session 7's rule, applied to the layer above: a harness that has
    never returned FAIL has not been tested.

### Phase 1 — Seven cases

- [x] **Step 3 — Two read/search cases.**
  - Build: D106. Design the task so the correct answer is *forced* to contain
    something checkable — "name the file and the function" — rather than hoping
    prose contains a substring.
  - Test: write the check first, then answer the task by hand, then try to
    phrase a correct answer that the check rejects. If you can do that in under
    a minute, the check is wrong.

- [x] **Step 4 — Two multi-file cases.**
  - Build: a rename plus its import; a constant plus the test that asserts it.
  - Test: `allowed_changed` lists both. Then deliberately stop the agent after
    the first edit (Session 8's failpoints) and confirm the verifier fails —
    half a rename must not score as a pass.
  - Review: this is where D42's honesty about pairs of files becomes a grade.

- [x] **Step 5 — Two failure/boundary cases.**
  - Build: one ambiguous `old` that must produce `multiple_matches` and a
    recovery; one path outside the workspace that must be refused.
  - Test: success here means the agent *failed correctly* — the refusal
    happened and the workspace is untouched.
  - Review: `loud_comment` already covers injection. Do not write a second
    injection case until the first one has ever failed.

- [x] **Step 6 — One already-correct case.**
  - Build: D107. A workspace with no bug, and a task that describes a bug that
    is not there.
  - Test: success is an unchanged workspace and an answer that says so.
  - Review: the failure mode to catch is the agent "fixing" something to satisfy
    the task. If it edits, the row must be a loud failure, not a near miss.

### Phase 2 — The suite

- [x] **Step 7 — Configuration is part of the result.**
  - Build: D109. Record model id, prompt version, code version (git sha),
    provider, budget, and the template hash of every fixture, into every row.
  - Test: two rows from different code produce different `code` fields.
  - Review: a result without its configuration is not a result. Session 12
    compares one variable at a time and cannot do that if the rows do not say
    which variables were set.

- [x] **Step 8 — Rows, then a summary.**
  - Build: D111. One JSONL row per run into `evals/results/`, then a rendered
    table: per case pass/fail/flaky, and the totals the plan names — success
    rate, unexpected changes, tool calls, model requests, duration, tokens,
    estimated cost, stop reason.
  - Test: the summary recomputes from the rows alone, with no state in memory.

- [x] **Step 9 — Make the suite survivable.**
  - Build: D104 and D105. A spend ceiling that stops the suite, a per-case
    timeout, and an `error` row for a case that explodes.
  - Test: a case whose check script hangs; a provider that 401s on run 4 of 30.
  - Review: the suite must be resumable or cheap enough to rerun. Thirty runs
    lost to one bad key is the most expensive mistake available today.

> **Review checkpoint.** The whole suite green on scripted models, no API key,
> no network, under two seconds. Then one live case, once, and read its row by
> hand before running the other nine.

### Phase 3 — Live, once, then three times

- [x] **Step 10 — Ten cases, one run each.**
  - Test: record every row. Expect failures; a 10/10 first live suite means the
    cases are too easy and Session 12 will have nothing to compare.

- [x] **Step 11 — Ten cases, three runs each.**
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

Decisions closed during implementation:

- D100–D111 are recorded above. `fixtures/` is the only source of cases.
- Suite resume is deferred. Every completed row is durable; an existing result
  file is never overwritten. A deliberate rerun uses a new suite file, so no
  earlier failure disappears. Individual journals remain available for inspection.
- A successful eval must end with `answer`; a limit is an incomplete run, even
  if a partial workspace happens to satisfy a subset of checks.

Things that surprised me:

- The category table asks for three simple edits and two boundaries, but the
  numbered build steps add two new boundaries to the existing injection case.
  Following all explicit steps gives **2 read/search, 2 simple edits, 2 multi-file,
  3 boundary, 1 already-correct**: ten cases. We kept both required boundary
  experiments rather than quietly omitting a step or adding an eleventh task.
- Existing Session 7 tests assume every fixture starts broken and has one golden
  edit. Those tests now explicitly cover the original three; Session 11 tests
  exercise all ten, including legitimate zero-edit and two-edit cases.
- Pytest initially collected the fixture's own `test_settings.py` as a project
  test. `pytest.ini` now limits project discovery to `tests/`.
- The first live suite passed 10/10. We did not rewrite cases or rerun failures
  to obtain a preferred result; this is evidence that the suite is a smoke test,
  not a strong comparison benchmark.

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


## G. Execution evidence (2026-11-17)

The original three golden runs scored **3/3**, with 0.22s inside the workers.
The full scripted suite scored **10/10**, with 0.58s inside the workers.
Worker startup adds overhead; these durations are not a claim about total CLI
wall time. No API key or network is needed for scripted runs.

Three durable negative-control rows were produced for `tail_window`:

| Script | Expected and observed failure |
|---|---|
| `wrong_file` | `unexpected_change`, even though the behavioral check passes |
| `noop` | `check_failed` |
| `double_edit` | `double_edit`: two successful writes undo the fix |

The classifier does **not** reject every second write. A valid alternative fix
still passes; `double_edit` explains a failing checker with repeated writes to
the same path. Rejected repeated calls are not counted as writes.

Tests additionally cover both multi-file cases killed by `os._exit(73)` after
the first write, refusal without mutation before ambiguous-anchor recovery,
a correct boundary explanation without an actual refusal (FAIL), read-answer
format variations and incorrect identifiers, a semantic fix different from the
golden patch (PASS), and an already-correct workspace with an unrequested
comment added (FAIL). A hanging checker becomes an error; a simulated provider
401 on run 4 keeps three pass rows plus an error row and stops. Other case
errors keep their row and allow the following case to run.

The live preflight fixed only `stats.py`, passed the independent checker, and
recorded 4 requests, 6 tool calls, 5,901 tokens, 5.72s and an estimated $0.000774.
Its row was inspected before the full suite. A preceding sandbox-network
failure is retained separately; it was an infrastructure error, not a task
failure, and did not produce an answer or edit.

The ten-case live suite scored **10/10**, with 43 model requests, 42 tool calls,
57,480 tokens, 64.25s total worker time and an estimated **$0.006429**. All ten
stopped with `answer`; there were no unexpected changes or checker errors.

Raw rows and journals are local, ignored run artifacts:

- `evals/results/session-11-phase-0.jsonl`
- `evals/results/session-11-negative-{unexpected,noop,double}.jsonl`
- `evals/results/session-11-scripted-final.jsonl`
- `evals/results/session-11-live-preflight.jsonl` (sandbox network error)
- `evals/results/session-11-live-preflight-network.jsonl` (successful preflight)
- `evals/results/session-11-live-once.jsonl`
- `evals/results/session-11-live-three.jsonl` (three-repeat suite)

Live commands supplied `--prices 0.006 0.30 1.20`, in USD per million
cache-hit input, cache-miss input and output tokens, with a $0.25 estimated
suite ceiling ($0.10 for preflight). These are the peak Flash rates checked
against [DeepSeek's pricing page](https://api-docs.deepseek.com/quick_start/pricing/)
on this date, used conservatively at off-peak hours too. They are estimates,
not a provider invoice. The configured request model was `deepseek-chat`; the
responses identified `deepseek-flash`, retained in each row's trace metrics.
No provider defaults, prompts or agent capabilities were changed.

The suite ceiling depends on supplied prices and reported usage. Admission
uses request UTF-8 bytes plus framing allowance and a capped response rather
than assuming an average chars/token ratio. A timed-out/disconnected request
may have been billed without returning usage; stop the suite and label that
additional cost uncertain instead of counting it as a free request.

Read graders intentionally check explicit identifiers, not the truth of every
sentence: a response that names the right pair in an otherwise misleading
explanation may pass. This is a documented limitation of deterministic answer
checks. They accept common prose/Markdown/case variations and reject wrong
pairs; no grader was rewritten in response to a live answer.

Reproduce offline or inspect existing results:

```bash
python -m evals.run --mode golden --repeats 1
python -m evals.run --summary evals/results/session-11-live-three.jsonl
python -m pytest -q
```


### G.1 Three-repeat outcome and final review

The fixed three-repeat suite passed **30/30**. It recorded 120 model requests,
121 tool calls, 161,184 tokens, 183.91s total worker time, and an estimated
**$0.018991**. All ten cases passed 3/3; none was flaky. The complete per-case
token/duration ranges are in the linked report. We kept the easy-case result
rather than changing the suite after seeing its score.

Final offline counterexamples expanded the read checker to accept absolute
path prefixes and kept generated bytecode/OS metadata out of fixture copies
and scripted source reads. The initial broad pytest discovery had generated
bytecode inside the `limit_and_test` template; the original live workspaces
included it, and future workspaces omit it. All 40 original live-suite
answers/workspaces were regraded with the final checkers: **40/40 unchanged**.
No live answer had failed the read checker, no behavioral assertion or task
was changed, and no new model requests were made for this regrade.

`session-11-final-regrade.jsonl` records the audit and final checker hashes;
`session-11-scripted-clean.jsonl` is the final clean offline suite.
The full regression suite passed **605 tests**. The historical Session 8
fault matrix still exercises the original single-edit cases.


Final journals put eval identity inside `run_started.evaluation`, so the
Session 10 segment reader still sees exactly one run. The original live
journals retain their initial `eval_started` preamble as historical evidence.
The initial subprocess launcher took 0.47s in workers and 2.12s end to end;
an isolated timing also exceeded the checkpoint. The final Unix worker uses a
fresh forked process and process group per case, preserving isolation and
kill/reap deadlines without reimporting the interpreter for every case.
**Final scripted suite: 10/10, 0.53s in workers, 0.86s end to end**, satisfying
the under-two-second checkpoint. This is the sequential standalone CLI on
macOS/Linux; it starts no threads or HTTP clients before forking. The test
suite verifies that a timeout kills the worker group including its checker.

Final fast-worker evidence: `evals/results/session-11-scripted-fast-workers.jsonl`.
