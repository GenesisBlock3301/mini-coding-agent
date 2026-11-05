# Session 4 — The Loop: Tool Call → Execution → Result → Model

**Goal (from the plan):** implement model tool call → execution → tool result → model.
**Done when:** the agent independently reads a file to answer a task.

This is §8's first milestone: a task typed at the CLI, the model choosing
`list_files` and `read_file` by itself, an answer grounded in a real file, and a
stop within five steps.

Sessions 2 and 3 built tools that cannot be talked out of their boundary. Nothing
has used them yet. This session hands the model a menu and lets it order — and the
new failure mode is that the *caller* is now a language model, which emits plausible
text rather than valid arguments.

What already exists: `ModelResponse.tool_calls` is parsed and defaults to `[]`
(`agent/model.py:29`). What does not: `call_model` has no `tools` parameter yet, and
`agent/loop.py` is an empty docstring.

---

## A. Decisions to lock before writing code

| #   | Decision                              | Options                                                   | My pick + why |
|-----|---------------------------------------|-----------------------------------------------------------|---------------|
| D21 | Where tool schemas live               | beside each tool / one registry module / inline in `loop.py` |               |
| D22 | Name → function dispatch              | dict registry / `if/elif` / `getattr`                      |               |
| D23 | Unknown tool name                     | error result to the model / raise                          |               |
| D24 | Who validates arguments               | the loop / each tool / a schema library                    |               |
| D25 | Where `root` comes from               | loop argument / module constant                            |               |
| D26 | What the tool message carries         | `content` only / `content` + `ok` + `error_code`           |               |
| D27 | What a "step" counts                  | model calls / tool calls / both                            |               |
| D28 | Hitting the step limit                | partial answer / explicit "incomplete" / raise             |               |
| D29 | Does the system prompt state the rules | yes, as guidance / no, code enforces                       |               |
| D30 | History growth                        | keep everything / trim old tool outputs                    |               |

**Hardest two:** D26 and D29.

D26 because the tool message is the model's entire view of what happened. Send only
`content` and a refusal reads like data; send the error code too and the model can
tell "I was refused" from "the file says this." You decided in Session 2 that
`content` is the model-facing field — this is where that decision gets tested.

D29 because it is the one people get backwards. Describing the workspace rule in the
prompt is useful — it makes the model cooperate and waste fewer steps. It is not
enforcement. If deleting that paragraph from the prompt changes what the agent *can*
do, rather than how often it tries, the boundary moved into the prompt.

---

## B. Sequential build order

Phase 1 is testable with **no API key and no network**. Don't make a real request
until step 8.

### Phase 0 — A model you can test against

- [ ] **Step 0 — The scripted model.**
  - Build: a fake `call_model` that returns `ModelResponse` objects from a list you
    wrote by hand — one with a tool call, one with a final answer.
  - Test: it returns them in order and raises when the script runs out.
  - Review: a loop that can only be tested with a live API is a loop you will test
    twice and then stop testing. Everything through step 7 depends on this.

### Phase 1 — The pieces, offline

- [ ] **Step 1 — Tool schemas.**
  - Build: the JSON description of `list_files` and `read_file` the provider expects.
  - Test: the schema names match the functions the registry can actually dispatch.
  - Review: your docstrings already explain these tools to a human. The schema
    explains them to the model — in particular, that paths are workspace-relative.
    Say it here and the model wastes fewer steps discovering it by rejection.

- [ ] **Step 2 — The registry and dispatch.**
  - Build: name → callable. One place.
  - Test: both names dispatch; the registry and the schema list agree.
  - Review: can a tool be in the schema list but missing from the registry? If those
    are two hand-maintained lists, they will drift in Session 6.

- [ ] **Step 3 — Argument validation.**
  - Build: turn the model's `arguments` string into checked keyword arguments.
  - Test: `arguments` that isn't valid JSON; `{}` with no `path`; `{"path": 5}`;
    `{"path": "src", "depth": 2}` (an argument you never defined).
  - Review: this is Session 8 arriving early, same as step 7 of Session 2. The model
    emits tokens, not function calls. Every one of those four must come back as a
    `ToolResult`, never an exception.

- [ ] **Step 4 — Unknown tools.**
  - Build: D23.
  - Test: the model asks for `delete_everything`.
  - Review: the message should name the tools that *do* exist. A model told only
    "unknown tool" will guess again; a model shown the menu will pick from it.

- [ ] **Step 5 — The tool result message.**
  - Build: `ToolResult` → the provider's tool-result message shape, carrying the
    `tool_call.id` it answers.
  - Test: the id on the result equals the id on the call, exactly.
  - Review: write down what breaks if two calls in one turn get each other's ids.
    Session 5 is built on this and will not forgive a guess here.

- [ ] **Step 6 — The loop.**
  - Build: §4's pseudocode, with your tools, against the scripted model.
  - Test: a two-step script — tool call, then final answer — produces the answer and
    a history containing assistant → tool → assistant in that order.
  - Review: does `loop.py` import `tools.py`, or does it know what `list_files`
    *does*? The loop should be able to run a tool it has never heard of.

- [ ] **Step 7 — Stopping.**
  - Build: the three exits — final answer, step limit, `ModelError`.
  - Test: a script that never stops asking for tools hits the limit at exactly
    `max_steps` model calls.
  - Review: what does the user see on a step-limit stop? "Incomplete, here is what I
    did" is a result. A silent partial answer is a lie with good manners.

> **Review checkpoint 1.** The whole loop runs, with stop reasons, under `pytest`,
> with no API key and no network. If it doesn't, Session 11's evals cannot either.

### Phase 2 — Live

- [ ] **Step 8 — Teach the adapter about tools.**
  - Build: a `tools=` parameter on `call_model`, passed into the payload.
  - Test: the payload contains the schemas; absent when `tools` is None.
  - Review: `model.py` must still know nothing about `list_files`. It serializes
    whatever it is handed.

- [ ] **Step 9 — The first real run.**
  - Build: wire `cli.py` to the loop.
  - Test (by hand): *"What does discount() in src/utils.py do?"*
  - Review: read the transcript, not just the answer. How many steps? Did it list
    before reading? Did it guess a path and get rejected — and recover?

- [ ] **Step 10 — The milestone.**
  - Build: nothing.
  - Test: run three tasks, including one whose file does not exist.
  - Done when: you can answer §8's five questions without looking anything up.

> **Review checkpoint 2.** Paste `loop.py` and the tool schemas. Review order:
> argument validation first, then id handling, then stopping — a loop that crashes on
> bad arguments never reaches the interesting failures.

---

## C. The failure table

Everything here must produce a *result*, never a traceback.

| What the model does | Expected | Notes |
|---|---|---|
| asks for `list_files("src")` | runs, result appended | the happy path |
| asks for `read_file("..")` | `outside_workspace` result | Sessions 2–3, unchanged |
| asks for a tool that doesn't exist | error result naming the real tools | D23 |
| sends `arguments` that aren't JSON | `invalid_arguments` result | it emits text, not calls |
| omits `path` | `invalid_arguments` result | |
| sends `{"path": 5}` | `invalid_arguments` result | wrong type, not missing |
| sends an argument you never defined | ignore or reject — decide, then test it | |
| returns text *and* tool calls | execute the calls; keep the text in history | both are legal |
| asks for the same thing five times | runs five times, stops at the limit | Session 9 bounds this |
| never stops asking for tools | `step_limit` stop, work so far reported | D28 |
| answers with no tool call at all | return it; it may still be wrong | §4 |
| the API times out mid-loop | `ModelError` surfaces with history intact | Session 8 retries |

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
> **The prompt is not a boundary.** (new)

The third one is this session's. The system prompt may *describe* the workspace rule
so the model cooperates; deleting that description must not change what the agent is
able to do, only how many steps it wastes finding out.

There is a second half, easy to miss: a file's text reaches the model as a tool
result, which means hostile content from `injection.md` is now sitting inside the
conversation, in a message the model treats as its own observation. Session 3 proved
it cannot widen the boundary. It can still waste your steps and your tokens, and
that is Sessions 8 and 9.

---

## E. Known limits of this loop

- **"Done" is not "correct."** A final response means the model stopped asking for
  tools. §4 is explicit about this, and it is why Session 11 evaluates outcomes
  independently rather than trusting the last message.
- **No retries.** One `ModelError` ends the run. Session 8 adds bounded retries, and
  with them the question of whether a retried *tool* call is safe to repeat.
- **History only grows.** Every tool result stays in the conversation and is re-sent
  every step. Your Session 2 and 3 caps are what make that survivable; Session 9
  measures it.
- **Sequential execution only.** Deliberate: predictable ordering matters once
  Session 6 can write files.

---

## F. Self-test before moving to Session 5

The five from §8, which you should now be able to answer from your own code:

1. Why must the tool result be sent back to the model?
2. Why must tool-call IDs match their results?
3. How does conversation history grow on each iteration?
4. Which rules are enforced by code rather than by the prompt?
5. Why is an agent saying "done" different from verified success?

And one more, because it is the bug this session invites:

6. If I delete the workspace paragraph from my system prompt, what changes?
