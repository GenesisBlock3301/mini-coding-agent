# Interlude — A Chat Session for the Agent You Already Have

**Goal:** one process, many questions, history that carries over. The Claude Code /
Codex shape.
**Done when:** a follow-up with a pronoun — *"and the other one?"* — is answered from
the history instead of met with a question; and the conversation's cost is capped
across the whole session, not per question.

**This is not a plan milestone.** None of the 14 sessions build it. Session 9 handles
the problem a long conversation *creates* (context growth) and Session 13 builds a
server for a different reason entirely (concurrent callers, isolated workspaces). The
interactive loop itself was never on the list.

It is worth ~90 minutes anyway, because sessions 7 through 9 are exploration work —
run a task, look at what the model did, adjust, run again — and a live session is a
far better instrument for that than relaunching a process per question.

**No FastAPI.** Claude Code and Codex are long-lived local processes holding history
in RAM; the only HTTP they speak is outbound to the model API. A web framework earns
its place when a second caller exists. Today there is one: your terminal.

---

## The thing to understand before writing any code

`run_loop` already supports this. It takes `messages` and returns `result.messages`:

```python
history = [{"role": "system", "content": TOOLS_SYSTEM_PROMPT}]
while True:
    history.append({"role": "user", "content": input("> ")})
    result = run_loop(history, root=root, budget=budget, journal=journal)
    print(result.text)
    history = result.messages        # the entire trick
```

That works today. `_task_of` already scans backwards for the most recent user message
(`agent/loop.py:194`), so each turn journals its own task correctly.

Everything below is about what that loop gets **wrong** once the conversation outlives
a single question.

---

## A. Decisions to lock before writing code

| #   | Decision                                   | Options                                                             | My pick + why |
|-----|--------------------------------------------|---------------------------------------------------------------------|---------------|
| D53 | Where it lives | `agent/chat.py` | **Separate module.** cli.py:main is the eval runner's entry point and must not move. |
| D54 | Per turn vs per session | **split by limit** | steps/seconds/tool-calls per turn, tokens/spend per session. A session clock would count reading and typing. |
| D55 | How the session cap reaches the loop | **derive the Budget** | `Session.turn_budget()` hands each turn what the session has LEFT. No run_loop signature change. |
| D56 | Journal scope | **one per session** | Safe since `Journal.heal()`, and required by D62. |
| D57 | What a turn prints | **both** | per-turn `report_totals` + one `[session]` line; `:cost` for detail. |
| D58 | History after a limit stop | **keep it** | Every tool_call_id in it was answered, so it stays valid; rolling back discards work already paid for. |
| D59 | When the history gets too big | **warn then refuse** | Measured by the last turn's real `prompt_tokens`, not an estimate. 80% warns, the ceiling refuses. |
| D60 | Ctrl-C | **abandon the turn** | The candidate-history pattern already makes this safe. |
| D61 | In-band commands | **`:quit :cost :reset :history :help`** | Matched before anything reaches the model. `:reset` clears history, never the bill. |
| D62 | Resuming a session | **restore, don't replay** | History and totals come back; an interrupted turn is NOT auto-finished - run_loop's `resume=` would double-count against totals rebuilt from the same file. |

### The hardest one is D54, and it has a trap in it

`_start` builds a **fresh `Ledger()` on every `run_loop` call** (`agent/loop.py:450`).
Your `Budget` object survives across turns; its counters do not. So today every limit
silently becomes per-question: `--max-spend=0.50` is fifty cents *per turn*, and an
evening of chatting has no cap at all. That is the bug this phase exists to fix.

The obvious fix — one `Ledger` for the whole session — is wrong, and wrong in a way
that is easy to ship:

```python
started_at: float = field(default_factory=time.monotonic)   # budget.py:220
elapsed_s = time.monotonic() - started_at                   # budget.py:224
```

`check_before_step` compares `elapsed_s` against `max_seconds`, which defaults to 300.
A session-scoped ledger starts its clock when the session starts — so after five
minutes of conversation, *including the time you spent reading answers and typing*,
every subsequent turn dies instantly with `time_limit`. The wall clock was never
measuring the run; its docstring says so: **"the wall clock protects this process, the
token and call counts protect the run."** Idle time at a prompt is not work.

So D54 is probably not one answer but four:

| Limit       | Natural scope | Why |
|-------------|---------------|-----|
| `max_steps` | turn | "how many model calls may one question take" |
| `max_seconds` | turn | protects responsiveness; idle time must not count |
| `max_tool_calls` | turn | a session cap would throttle a long conversation for no reason |
| `max_total_tokens`, `max_spend` | **session** | this is the money, and the money is the whole point |

Which means the mechanism is not "pass a ledger" but "carry *some* counters across and
reset the rest". Decide where that split is implemented — in `Ledger`, in the chat
loop, or in a new object that owns both — before writing it.

### D55 is a signature change, and it is the same one Session 13 needs

`run_loop` has no way to accept a ledger. Whatever you choose here is the seam a
server would use later for exactly the same reason: per-caller accounting. Worth
getting right once.

One edge to settle while you are in there: if a caller passes a ledger **and**
`resume=`, which wins? `_start` currently rehydrates the ledger from the journal
(D39). A passed-in ledger and a rebuilt one are two sources of truth for the same
number.

### And the quiet one: D56 meets the torn-line bug

A chat session with one journal file reopens that file on every turn. `Journal.append`
opens per call, so a torn final line from a crash gets glued to the next record and
**both** are dropped by `events()` — the valid one too, in silence. Reproduced:

```
after the crash      : ['run_started']
after resume appends : ['run_started']        <- model_result is gone
raw last line        : {"t": "tool_int{"t": "model_result", "at": ..., "step": 2}
```

**Fixed, 2026-11-13.** `Journal.heal()` truncates a torn final line back to the last
newline, and `append` calls it once per instance - so a session-wide journal is now
safe to reopen. `events()` also stopped swallowing bad lines in the middle of a file:
that cannot be caused by a crash, so it raises `JournalCorrupt` rather than quietly
rebuilding a history with a hole in it. See `tests/test_journal.py`.

D56 is therefore a free choice now, not a forced one.

---

## B. Sequential build order

**All seven steps implemented 2026-11-13.** `agent/chat.py` (~400 lines),
`tests/test_chat.py` (19 tests, no key and no network). Nothing in `agent/`
changed: `run_loop`, `Budget`, `Ledger`, `Journal` and the policy are called
exactly as `cli.py` calls them.

### Phase 0 — The loop that already works

- [x] **Step 1 — Eight lines, one new file.**
  - Build: D53. The loop above, a system message, `input()`, print `result.text`.
    Do not touch `cli.py:main` — one-shot mode is what Session 11's eval runner calls
    and it must stay exactly as it is.
  - Test: three turns. `list the files` → `read calculator.py` → **`what does the
    other one do?`** That third turn is the whole feature: it only works if the
    history carried.
  - Review: `import readline` (stdlib, one line, Unix) gives `input()` arrow-key
    history and line editing for free. Skip it and you will feel the difference on
    turn four.

### Phase 1 — Make the growth visible before you cap it

- [x] **Step 2 — Print the numbers every turn.**
  - Build: D57. Per turn: `prompt_tokens`, `completion_tokens`, `cache_hit_tokens`,
    `len(history)`, `cost_usd`. Plus a running session total you accumulate yourself.
  - Test: run six turns, two of them reads of real files. Watch `prompt_tokens` climb
    turn over turn — you are paying for the whole conversation on every single
    request. That number is Session 9's milestone, made visible for the price of a
    print statement.
  - Review: `cache_hit_tokens` should jump from turn 2 onward, because the prefix is
    stable and the provider caches it. If it does not, something is mutating the front
    of your history and you have just found it for free.

### Phase 2 — The session budget

- [x] **Step 3 — Decide the split, then implement it.**
  - Build: D54 and D55.
  - Test: set a tiny `MAX_SPEND`, chat until it fires. Then the trap: set
    `--max-seconds=60`, chat slowly for two minutes, confirm turn three still runs.
    A session that dies because you were *reading* is the bug this test exists for.
  - Review: whatever carries across turns must also survive a crash, or `--resume`
    hands back a session whose spend has quietly reset — the same `kill -9` hole D39
    closed for a single run.

### Phase 3 — Interrupts and commands

- [x] **Step 4 — Ctrl-C, and the half-finished turn.**
  - Build: D60 and D58. A `KeyboardInterrupt` during `run_loop` leaves a history whose
    last entries may be tool messages with no answer after them. Decide whether the
    next turn sees that, or whether you roll back to before the user's message.
  - Test: Ctrl-C during a model call. Then Ctrl-C between the tool call and the
    answer. Ask a normal question afterwards and see whether the model copes.
  - Review: rolling back is the honest option but it throws away tool results you
    paid for. Keeping them means the model sees a question it never finished
    answering, followed by a new one. Both are defensible; drifting into one by
    accident is not.

- [x] **Step 5 — In-band commands.**
  - Build: D61. At minimum `:quit` and `:cost`. `:reset` (new history, same process,
    same session budget) is the one you will use most while testing prompts.
  - Review: a command must never reach the model. Check for the prefix before
    appending to `history`, not after.

### Phase 4 — Only if you want it now rather than at Session 9

- [x] **Step 6 — The context cap.** D59. Measure first (Phase 1 already does), then
  decide what happens at the ceiling. Refusing a new turn with a clear message is a
  complete answer; dropping old turns is a decision with consequences for tool-call
  pairing — an assistant turn and its tool results must be dropped together or the
  next request is a provider 400.

### Phase 5 — Optional

- [x] **Step 7 — `--resume=<session>`.** D62. You already have `rebuild()`. A chat
  session is several runs in one file, and `rebuild` reassigns `state.messages` on
  every `run_started` — check what that does across turns before trusting it.

---

## C. What you are deliberately not building

- **Streaming.** Claude Code and Codex stream tokens over SSE; `call_model` blocks for
  the whole response. Streaming complicates usage accounting (totals arrive in a final
  event) and tool-call assembly (arguments arrive as fragments — that is what the
  `"index": 0` in your journal's tool calls is for). Not now.
- **FastAPI.** See the top of this file.
- **Compaction / summarisation.** The plan is explicit: a summary can lose file
  details, instructions, and tool-call relationships. Measure first, cap second,
  summarise much later if ever.
- **Concurrent sessions.** One process, one conversation. That is Session 13.

---

## D. What this lets you exercise today

| Works now | Not yet |
|---|---|
| Tool choice on a follow-up question | `replace_text` (Session 6, unwritten) |
| `list_files`, `read_file`, every rejection code | Any editing or diffs (7) |
| Workspace boundary, symlink refusals | Retries on timeout (8) |
| All five stop reasons and the pre-POST gate | Eval scoring (11) |
| Journal shape across many turns | |
| Prompt-cache behaviour over a growing prefix | |

Read-only, in other words. Which is an argument for doing Session 6 first — or for
building this as the instrument you use *while* doing 6 through 9.

---

## E. Self-test

1. Your session has run twelve turns. How many times has the content of
   `calculator.py` been sent to the model, and what did each one cost?
2. `--max-spend=1.00` is set and the session has spent $0.98. What happens on the next
   turn, and at which of the two gates?
3. A turn stops at the step limit. What is the last message in `history`, and what
   does the model see at the start of the next turn?
4. You press Ctrl-C while a `read_file` is running. What is in the journal, what is in
   `history`, and are those two still consistent with each other?
5. The session budget is carried across turns in memory. The process is killed. What
   does a resumed session believe it has spent?
6. Why does `max_seconds` belong to the turn when `max_spend` belongs to the session —
   and what breaks if you get that backwards?
7. At what point does this stop being a REPL and start needing Session 13 — and is
   "two terminals" the answer, or is it something else?


---

## F. Found by building it

**TOKEN_RESERVE swamps a small session cap.** `check_before_step` compares
tokens-so-far **plus the reserve** against the limit, so `MAX_SESSION_TOKENS`
at or below `TOKEN_RESERVE` (default 2000) refuses every turn before a single
request. Correct, and completely silent - it reads as "nothing happens".
`chat.py` now warns at startup. Found by a test, not by reasoning.

**`TOKEN_RESERVE=0` cannot be expressed.** `Budget.from_env` uses
`_env_int("TOKEN_RESERVE") or DEFAULT_TOKEN_RESERVE`, and `0` is falsy, so it
silently becomes 2000. The same pattern affects every `or DEFAULT` in that
method. Not fixed - it is core code and outside this interlude's scope.

**`rebuild` reassigns `state.messages` at every `run_started`.** For a
session journal that is correct by luck rather than design: each turn's
`run_started` carries the whole accumulated history, so the last one wins and
is complete. Worth knowing before anyone changes what `run_started` records.
