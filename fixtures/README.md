# Fixture projects (Session 7+)

One directory per case. The agent sees `workspace/` and nothing else.
Templates stay here; each evaluation copies one into
`.runtime/eval-workspaces/`. Execution journals live separately in
`.runtime/journals/`.

    fixtures/<case>/
        case.json          the task, the allowed changes, the golden fix
        workspace/         copied fresh per run - this IS the agent's world
        verify/check.py    the independent check - never copied in (D60)

The split is the point. `check.py` lives outside `workspace/`, so the agent
cannot read the tests, cannot edit them, and cannot make a failing check pass
by changing what it asserts. Making a test pass by editing the test is a
correct solution to the problem you posed; the fix is to pose a different
problem.

Build, run, verify:

    python scripts/fixture.py build tail_window
    python -m agent.cli --tools --workspace=<the path it printed> "<the task>"
    python scripts/fixture.py verify tail_window --workspace=<that path>

`case.json` carries the `golden` fix - the one-line answer, written down
before any model saw the fixture. A fixture whose correct fix you cannot state
in advance is a fixture you cannot grade.


Session 11 left eleven cases here, including the added subtraction case. Single-edit Session 7 cases retain their
`golden` object. New cases use an `edits` array (possibly empty), an `answer`
for the offline golden transcript, a `category`, optional `probes` for scripted
tool failures, and `required_errors` that the eval grader verifies in the
journal. Live inference never receives the answer key, probes or checker.

`allowed_changed: []` means **nothing may change**, including additions and
removals. Read/search and already-correct cases additionally grade the final
answer, passed to `check.py` on stdin. Checkers remain separate subprocesses
with bytecode disabled and a deadline. `fixture.verify(..., answer=text)` uses
this protocol; the standalone fixture command remains useful for edit cases.

| Case | Category | Incorrect behavior it catches |
|---|---|---|
| `tail_window` | Simple edit | Off-by-one slice remains wrong |
| `subtraction` | Simple edit | Adding instead of subtracting, reversing operands, or breaking addition |
| `twice_over` | Simple edit | Editing the wrong matching branch |
| `loud_comment` | Boundary | Obeying injected file instructions |
| `find_discount` | Read/search | Naming display code instead of pricing math |
| `find_retry` | Read/search | Naming request dispatch instead of capped backoff |
| `rename_import` | Multi-file | Renaming a definition without its caller/import |
| `limit_and_test` | Multi-file | Updating a constant but leaving a stale/missing assertion |
| `ambiguous_recovery` | Boundary | Skipping the required ambiguity refusal or changing both functions |
| `outside_refusal` | Boundary | Claiming refusal without a rejected outside read, or changing files |
| `already_correct` | Already correct | Making unrequested edits or failing to explain no change |

Run all cases with `python -m evals.run --mode golden --repeats 1` (offline), or
select `--mode live` to use the configured provider. The Session 8 fault matrix
still targets the original single-golden-edit cases; Session 11 tests kill
both multi-file cases after their first write separately.

Session 12 expands the suite to **20 cases**. The ten additions relative to
Session 11's measured ten are `subtraction` plus nine behavioral cases:

| Case | Behavior checked |
|---|---|
| `zero_window` | Zero/negative counts, oversized windows, input preservation |
| `falsy_setting` | Missing keys versus explicit falsy values; unchanged display fallback |
| `stable_dedupe` | Stable order and equality for unhashable values |
| `adjacent_ranges` | Half-open intervals, empty/reversed ranges and symmetry |
| `nonadjacent_anchor` | Three separated matching anchors in a larger file |
| `atomic_transfer` | Validation before mutation, balances and self-transfer |
| `copy_merge` | Overrides without mutating either input |
| `literal_suffix` | Literal suffix versus character-set stripping |
| `pagination_total` | Total before slicing, zero limit and exhausted pages |

The last four in the pre-registration's `heldout` array are reserved from
**live** development inspection. Fixture authors and offline harness tests
necessarily inspect their specifications; this is a process holdout, not an
independently authored blind benchmark. All nine new pristine workspaces fail
their independent checkers; their golden edits pass.
