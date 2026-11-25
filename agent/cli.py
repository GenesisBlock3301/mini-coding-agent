"""Entry point: accept the user's task from the command line and print the result.

Owns: argument parsing, loading the API key, printing the trace and the final
answer, and turning a ModelError into one actionable line. Owns no agent
logic - the loop lives in agent/loop.py.

    python -m agent.cli "say hello in five words"
    python -m agent.cli --tools "what's in the awkward folder?"
    python -m agent.cli --tools --max-steps=3 "read src/utils.py and summarise it"

Can also be run straight from an IDE with no arguments: it then falls back to
the DEBUG_TASK preset at the bottom of this file.

With --tools the model is offered the file tools and the loop runs:
tool call -> execution -> tool result -> model, until the model answers
without asking for a tool, or max_steps model calls have been made.

stdout gets the answer and nothing else. The trace, the metrics and the errors
go to stderr, so Session 11's eval runner can read the answer without parsing
around them. A step-limit stop writes NOTHING to stdout: a partial answer is
not an answer (D28).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

# Running this file directly (PyCharm's green arrow) puts agent/ on sys.path
# instead of the project root, so `import agent.model` fails before main() is
# ever reached. Launching with `python -m agent.cli` sets __package__ and this
# is a no-op.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import load_config

from agent.budget import (
    DEFAULT_MAX_STEPS,
    STOP_CONTEXT_LIMIT,
    STOP_REPETITION,
    STOP_SPEND_LIMIT,
    STOP_STEP_LIMIT,
    STOP_TIME_LIMIT,
    STOP_TOKEN_LIMIT,
    STOP_TOOL_LIMIT,
    Budget,
    Pricing,
)
from agent.context import ContextSize
from agent.journal import (
    Journal,
    JournalCorrupt,
    ResumeState,
    new_run_id,
    rebuild,
)
from agent.loop import LoopResult, run_loop
from agent.model import ModelConfig, ModelError, ModelResponse, active_provider, context_warning
from agent.approval import approving_dispatcher
from agent.trust import ensure_trusted
from agent.storage import JOURNALS_DIR, workspace_root, recorded_workspace
from agent.replay import plans_for
from agent.retry import REASON_TEXT, RetryPolicy
from agent.tools import TOOL_SCHEMAS, ToolResult

SYSTEM_PROMPT = "You are a terse assistant."

# Separate prompt for --tools. It DESCRIBES the workspace rule so the model
# wastes fewer turns discovering it by rejection. It does not enforce it:
# delete this paragraph and the agent can do exactly the same set of things,
# just less gracefully. See docs/session-04.md E1.
#
# The optimistic-path sentences ("treat that name as a workspace-relative
# path...") are guess-then-recover. Without them the model spends its first
# call on list_files('.') to find out where things live. See session-04 E2.
#
# Session 6. Two rules about editing live here and NOT in replace_text's
# schema, because they are about SEQUENCING TOOLS - no single schema owns
# "read before you edit". Everything true of replace_text alone (exactly one
# match, copy verbatim, cannot create a file) stays in its schema
# description, where it travels with the tool and cannot go stale when the
# next one is added. This prompt already went stale once: it claimed two
# tools after list_folders made it three.
#
# The test for any sentence added here: delete it, and does the agent become
# UNABLE to do something, or merely slower? Slower belongs here. Unable means
# a boundary was written in prose, and prose is not a boundary.
#
# The "every file" sentence is CONDITIONAL on purpose. An unconditional "always
# descend" would cost six tool calls for every listing question, and against a
# 5000-entry directory it would spend the budget to produce a knowingly partial
# answer. The model asking "which folder?" is the right default; this gives it
# a way to recognise when the user already said "all of them".
#
# It NAMES THE PHRASES rather than describing the category. The first version
# said "when a question is about every file rather than one directory", and it
# never fired: asked for "all files not folders" the model read that as "the
# entries here, minus the folders" - a fair reading, and not the category the
# rule described. A rule the model has to agree with your taxonomy to apply is
# a hint, not a rule. Quote the words the user will actually type.
#
# The truncation half is not politeness. list_files caps at MAX_ENTRIES/
# MAX_BYTES and footers the fact, and a model that reads the footer and then
# presents 200 of 5000 names as the answer is worse than one that refused -
# the user cannot see the cap, only the list.
TOOLS_SYSTEM_PROMPT = (
    "You are a terse coding assistant with six tools: list_files, "
    "list_folders, read_file, replace_text, write_file and run_python. File "
    "tool paths are relative to the workspace root - never absolute or outside it. "
    "Use run_python only when the user explicitly asks to run or test a Python script. "
    "It executes code with the current user's permissions; run only trusted workspace code. "
    "Call a tool when the "
    "answer depends on what is actually on disk. Before each call, check that "
    "the requested result cannot already be provided from the conversation or "
    "from an earlier tool result. Choose only tools needed for the user's "
    "specific request; do not inspect files when the user asks only about "
    "folders, or inspect folders when the user asks only about a named file. "
    "After a tool result answers the request, stop calling tools and respond. "
    "When the user names a file or folder, treat that name as a "
    "workspace-relative path and call the tool on it directly - do not list "
    "the root first to go looking for it. Only list a parent directory if "
    "the path you tried is reported missing. "
    "Always read a file before you edit it, and copy the text for 'old' "
    "verbatim from what read_file showed you, including indentation. "
    "Make one change per replace_text call. "
    "Use write_file only to create a new file; it never overwrites an existing file. "
    "\"All files\", \"every file\" or \"the files\" with no directory named "
    "means files ANYWHERE in the workspace, not only the root one. Answer "
    "those by calling list_folders, then list_files on every subdirectory it "
    "returns, all in a single batch - never from the root listing alone. If "
    "a listing says it was truncated, say so in your answer instead of "
    "presenting a partial list as complete. "
    "When you have finished, state exactly which files you changed and what "
    "you changed in each."
)

# Named once so the parser, the usage line and the debug presets below cannot
# drift apart.
RUNS_DIR = JOURNALS_DIR

TOOLS_FLAG = "--tools"
RAW_FLAG = "--raw"
MAX_STEPS_FLAG = "--max-steps="
MAX_TOOL_CALLS_FLAG = "--max-tool-calls="
MAX_SECONDS_FLAG = "--max-seconds="
MAX_TOKENS_FLAG = "--max-tokens="
MAX_SPEND_FLAG = "--max-spend="
RESUME_FLAG = "--resume="
WORKSPACE_FLAG = "--workspace="
# Session 9. The off switch, not a threshold knob: the thresholds belong in
# .env with every other limit (MAX_REPEATED_FAILURES, MAX_REPEATED_CALLS),
# and what a command line needs is the ability to run the SAME task with the
# rule and without it. That is Session 12's control arm, and a comparison you
# cannot run from one shell line is a comparison that does not get run.
NO_REPEAT_STOP_FLAG = "--no-repeat-stop"
USAGE = (
    f'usage: mini-agent [{WORKSPACE_FLAG}DIR] "your task" '
    f"[{RAW_FLAG}] [{MAX_STEPS_FLAG}N]\n"
    f"       [{MAX_TOOL_CALLS_FLAG}N] [{MAX_SECONDS_FLAG}S] "
    f"[{MAX_TOKENS_FLAG}N] [{MAX_SPEND_FLAG}USD]\n"
    f"       [{NO_REPEAT_STOP_FLAG}]\n"
    f"       mini-agent {RESUME_FLAG}<run_id>      # continue a dead run\n"
    f"\nLimits default from .env (MAX_STEPS, MAX_TOOL_CALLS, MAX_RUN_SECONDS,\n"
    f"MAX_TOTAL_TOKENS, MAX_SPEND, TOKEN_RESERVE); flags override them.\n"
    f"{MAX_SPEND_FLAG}USD needs PRICE_* configured in .env - an unpriced spend\n"
    f"limit cannot be enforced, so it is refused rather than ignored.\n"
    f"{WORKSPACE_FLAG}DIR selects the project to edit. By default, a fresh\n"
    f"run uses the current directory; WORKSPACE_DIR also sets the default.\n"
    f"{NO_REPEAT_STOP_FLAG} counts identical calls but never stops for them\n"
    f"(MAX_REPEATED_FAILURES / MAX_REPEATED_CALLS / CONTEXT_WINDOW set the rules)."
)

# Exit codes let a caller classify a failure without parsing text.
EXIT_CODES = {
    "auth": 3,
    "billing": 4,
    "bad_request": 5,
    "rate_limit": 6,
    "server": 7,
    "timeout": 8,
    "network": 9,
    "bad_response": 10,
}
EXIT_USAGE = 2
EXIT_UNKNOWN = 1
EXIT_STEP_LIMIT = 11

# D40: one code per stop reason, not one code for "stopped". Session 11's
# runner has to tell "the model answered" from "the agent ran out of money"
# without parsing English, and so does any shell wrapper you write.
LIMIT_EXIT_CODES = {
    STOP_STEP_LIMIT: EXIT_STEP_LIMIT,
    STOP_TOOL_LIMIT: 12,
    STOP_TIME_LIMIT: 13,
    STOP_TOKEN_LIMIT: 14,
    STOP_SPEND_LIMIT: 15,
    # Session 9, D88. Codes of their own, for the reason D40 gave: Session
    # 11's runner has to tell "went in circles" from "ran out of steps"
    # without parsing English, and those two are the pair it will see most.
    STOP_REPETITION: 16,
    STOP_CONTEXT_LIMIT: 17,
}


@dataclass(frozen=True)
class Options:
    """One run's settings, already validated. Nothing downstream reads argv."""

    task: str
    use_tools: bool
    show_raw: bool
    # None means "not given on the command line" - .env or the Budget default
    # then decides. 0 is a real value and must not collapse into "unset".
    max_steps: int | None = None
    max_tool_calls: int | None = None
    max_seconds: float | None = None
    max_tokens: int | None = None
    max_spend: float | None = None
    resume_run_id: str | None = None
    # A path as the user typed it. Resolving is policy.workspace_root()'s
    # job, not argv parsing's - one place decides what a path means.
    workspace: str | None = None
    # Session 9. Counting continues; only the stopping is switched off.
    repeat_stop: bool = True

    def budget(
        self, pricing: Pricing | None, *, context_window: int | None = None
    ) -> Budget:
        """Flags over .env over defaults. Built here so nothing downstream
        reads argv, and so the whole rule set is one printable value."""
        base = Budget.from_env(pricing=pricing)
        return replace(
            base,
            max_steps=self.max_steps if self.max_steps is not None else base.max_steps,
            max_tool_calls=(
                self.max_tool_calls
                if self.max_tool_calls is not None
                else base.max_tool_calls
            ),
            max_seconds=(
                self.max_seconds if self.max_seconds is not None else base.max_seconds
            ),
            max_total_tokens=(
                self.max_tokens
                if self.max_tokens is not None
                else base.max_total_tokens
            ),
            max_spend=(
                self.max_spend if self.max_spend is not None else base.max_spend
            ),
            # None on both halves is what "count but never stop" means to
            # Tally - the Session 8 behaviour, kept reachable on purpose.
            max_repeated_failures=(
                base.max_repeated_failures if self.repeat_stop else None
            ),
            max_repeated_calls=(
                base.max_repeated_calls if self.repeat_stop else None
            ),
            # The provider's own figure, unless .env named one. An env
            # setting is a statement about THIS deployment and outranks a
            # table compiled when the adapter was written.
            context_window=(
                base.context_window
                if os.getenv("CONTEXT_WINDOW")
                else (context_window or base.context_window)
            ),
        )

    @property
    def system_prompt(self) -> str:
        return TOOLS_SYSTEM_PROMPT if self.use_tools else SYSTEM_PROMPT

    @property
    def tool_schemas(self) -> list[dict] | None:
        return TOOL_SCHEMAS if self.use_tools else None


def _positive_number(value: str, kind: str) -> float | None:
    """A limit of zero or a limit of 'abc' is a typo, not a configuration.

    Returning None (rejecting the whole command line) rather than clamping:
    a silently corrected limit is a limit the user believes in and does not
    have.
    """
    try:
        number = int(value) if kind == "int" else float(value)
    except ValueError:
        return None
    return None if number <= 0 else float(number)


def _as_int(value: float | None) -> int | None:
    return None if value is None else int(value)


# Paired with _parse_limits. A tuple rather than five inline branches: adding
# a limit in a later session is one line here, not another `if` in a parser
# that SonarQube already flagged once.
LIMIT_FLAGS = (
    (MAX_STEPS_FLAG, "int"),
    (MAX_TOOL_CALLS_FLAG, "int"),
    (MAX_TOKENS_FLAG, "int"),
    (MAX_SECONDS_FLAG, "float"),
    (MAX_SPEND_FLAG, "float"),
)


def _flag_value(flags: set[str], prefix: str) -> str | None:
    """The text after `prefix`, or None when the flag was not given.

    None and "" are different answers and both matter: absent means "use the
    default", empty means the user typed `--workspace=` and meant something.
    """
    return next((f[len(prefix):] for f in flags if f.startswith(prefix)), None)


def _parse_limits(flags: set[str]) -> dict[str, float] | None:
    """Every numeric limit on the command line, or None if any one is junk.

    One bad limit rejects the WHOLE command line rather than the single flag.
    Running with four of the five limits the user asked for is the failure
    mode `_positive_number` exists to prevent, one level up.
    """
    numbers: dict[str, float] = {}
    for prefix, kind in LIMIT_FLAGS:
        raw = _flag_value(flags, prefix)
        if raw is None:
            continue
        value = _positive_number(raw, kind)
        if value is None:
            return None
        numbers[prefix] = value
    return numbers


def _has_task(positional: list[str], resume_run_id: str | None) -> bool:
    """Is there something for this run to do?

    Two sources, never both: a resumed run takes its task from the journal,
    so the id is what has to be present; a fresh run needs a non-blank
    positional argument.
    """
    if resume_run_id:
        return bool(resume_run_id.strip())
    return bool(positional) and bool(positional[0].strip())


def parse_argv(argv: list[str]) -> Options | None:
    """Options, or None when the task is missing, blank or a limit is junk.

    Flags are order-free and may sit on either side of the task. Anything that
    is not a flag is positional; only the first positional is used.

    Reads as four questions, each answered by one helper: what was asked for,
    is the workspace sane, is there a task, are the limits numbers. Keeping
    the validation flat here is deliberate - a parser whose branches nest is
    one where a rejected command line quietly becomes an accepted one.
    """
    flags = {a for a in argv[1:] if a.startswith("--")}
    positional = [a for a in argv[1:] if not a.startswith("--")]
    exact_flags = {TOOLS_FLAG, RAW_FLAG, NO_REPEAT_STOP_FLAG}
    value_flags = (RESUME_FLAG, WORKSPACE_FLAG, *(prefix for prefix, _ in LIMIT_FLAGS))
    if any(flag not in exact_flags and not flag.startswith(value_flags) for flag in flags):
        return None

    resume_run_id = _flag_value(flags, RESUME_FLAG)
    workspace = _flag_value(flags, WORKSPACE_FLAG)
    if len(positional) > 1 or (resume_run_id is not None and positional):
        return None

    # An empty value is a typo, never a request for the default. Same rule as
    # a malformed limit: a silently corrected setting is one the user believes
    # in and does not check.
    if workspace is not None and not workspace.strip():
        return None
    if not _has_task(positional, resume_run_id):
        return None

    numbers = _parse_limits(flags)
    if numbers is None:
        return None

    return Options(
        task=positional[0] if positional else "",
        use_tools=True,
        show_raw=RAW_FLAG in flags,
        max_steps=_as_int(numbers.get(MAX_STEPS_FLAG)),
        max_tool_calls=_as_int(numbers.get(MAX_TOOL_CALLS_FLAG)),
        max_seconds=numbers.get(MAX_SECONDS_FLAG),
        max_tokens=_as_int(numbers.get(MAX_TOKENS_FLAG)),
        max_spend=numbers.get(MAX_SPEND_FLAG),
        resume_run_id=resume_run_id,
        workspace=workspace,
        repeat_stop=NO_REPEAT_STOP_FLAG not in flags,
    )


def _warn_about_context(provider) -> None:
    """A silently truncating server is worse than one that refuses."""
    try:
        warning = context_warning(ModelConfig.from_env())
    except ModelError:
        return
    if warning:
        print(f"warning: {warning}", file=sys.stderr)


def report_model_error(err: ModelError) -> int:
    """Print one actionable line about a failed call; return its exit code."""
    print(f"error [{err.kind}]: {err.user_message}", file=sys.stderr)
    if err.detail:
        print(f"  detail: {err.detail}", file=sys.stderr)
    print(
        f"  retryable={err.retryable} safe_to_retry={err.safe_to_retry}",
        file=sys.stderr,
    )
    # D76: the exit code stays the failure's own kind. "Gave up after three
    # attempts" is the same outcome as "failed once" to a shell wrapper, and
    # a new code would make every caller learn a word for a detail they
    # already have in the text.
    attempts = getattr(err, "attempts", 1)
    if attempts > 1:
        why = REASON_TEXT.get(getattr(err, "gave_up_because", ""), "")
        print(
            f"  sent {attempts} times" + (f"; stopped because {why}" if why else ""),
            file=sys.stderr,
        )
    return EXIT_CODES.get(err.kind, EXIT_UNKNOWN)


def _failpoint(name: str, step: int) -> None:
    """Pull the plug if this run asked for it. Off unless the env var is set.

        CRASH_AFTER_STEP=1 python -m agent.cli --tools "..."
        CRASH_AFTER_TOOL=1 python -m agent.cli --tools "..."

    os._exit, not sys.exit: sys.exit raises, which unwinds the stack and runs
    `finally` blocks and atexit handlers. A SIGKILL, an OOM kill and a power
    cut do none of that, so neither does this.

    A named failpoint in the real code path is how production systems rehearse
    this (etcd failpoints, the `fail` crate). A copy of the loop in a demo
    script rehearses the demo script.
    """
    wanted = os.getenv(name)
    if wanted and wanted.isdigit() and int(wanted) == step:
        print(
            f"\n>>> failpoint {name}={step}: power cut. Nothing was persisted,"
            " because there is nowhere to persist it. <<<",
            file=sys.stderr,
        )
        sys.stderr.flush()
        os._exit(137)


def print_response(step: int, response: ModelResponse) -> None:
    """One line per model call: what it cost and what it decided."""
    print(
        f"\nstep {step} [{response.model}] finish={response.finish_reason} "
        f"{response.duration_ms:.0f}ms "
        f"in={response.prompt_tokens} "
        f"(hit={response.prompt_cache_hit_tokens} "
        f"miss={response.prompt_cache_miss_tokens}) "
        f"out={response.completion_tokens}",
        file=sys.stderr,
    )
    if response.text:
        print(f"  said: {response.text}", file=sys.stderr)
    _failpoint("CRASH_AFTER_STEP", step)


def print_context(step: int, size: ContextSize) -> None:
    """One line per step, before the request, when it is worth reading.

    Quiet below half the window on purpose. A number printed every step from
    the first is a number nobody reads by the fifth, and this one only means
    anything as it climbs. `est/actual` is the error bar (D85): the estimate
    is characters/4 and the exact figure arrives one step late, so the ratio
    is how wrong the limit currently is.
    """
    if size.loud:
        print(f"  {size.line()}", file=sys.stderr)


def print_tool(step: int, call: dict, result: ToolResult) -> None:
    """One block per tool call. Goes to stderr: this is the model's scratch
    work, not the answer, and must never be mistaken for it on stdout."""
    function = call.get("function") or {}
    status = "ok" if result.ok else f"ERROR {result.error_code}"
    if result.truncated:
        status += " (truncated)"
    print(
        f"  → {function.get('name', '<missing>')}"
        f"({function.get('arguments', '')})  id={call.get('id')}",
        file=sys.stderr,
    )
    print(f"    {status}", file=sys.stderr)
    for line in result.content.splitlines():
        print(f"    | {line}", file=sys.stderr)
    # Crashing HERE is the interesting one: the tool already ran. Harmless
    # while tools are read-only; in Session 6 the file is already edited.
    _failpoint("CRASH_AFTER_TOOL", step)


def report_resume(state: ResumeState, root: Path) -> None:
    """What the journal says the dead process got through."""
    print(f"  replayed {state.steps_done} model call(s) and "
          f"{state.replayed_tools} tool result(s) from disk - cost: nothing",
          file=sys.stderr)
    if state.answer_ready:
        print("  the final answer was already generated and paid for. "
              "No API call needed.", file=sys.stderr)
    if state.unanswered_calls:
        names = ", ".join(
            (c.get("function") or {}).get("name", "?") for c in state.unanswered_calls
        )
        print(f"  unanswered tool call(s): {names}", file=sys.stderr)
    # Session 8, D72. Until this session the line here said redoing was safe
    # "because list_files and read_file do not mutate anything" - true when it
    # was written and false since Session 6. Now each interrupted call is
    # decided against the workspace and named individually, because "N tools
    # were entered" is not something a user can act on and "this edit already
    # applied, I am not repeating it" is.
    for plan in plans_for(state, root=root).values():
        print(f"  in flight: {plan.line()}", file=sys.stderr)
    if state.model_call_was_in_flight:
        print("  WARNING: a model call was in flight when the process died. It may "
              "have been billed; the response is unrecoverable. Re-calling.",
              file=sys.stderr)


def report_totals(result: LoopResult) -> None:
    """The whole run, which is what you actually pay for.

    Token totals come from the ledger, so a resumed run reports what the RUN
    spent, not what this process spent. `model=` is latency; `wall=` includes
    every tool call and is what the time limit actually measures.
    """
    cost = result.cost_usd
    cost_text = "unpriced" if cost is None else f"${cost:.6f}"
    print(
        f"\n[total] stop={result.stop_reason} steps={result.steps}/"
        f"{result.budget.max_steps} tools={result.tool_calls}"
        + (f"/{result.budget.max_tool_calls}"
           if result.budget.max_tool_calls is not None else "")
        + f" model={result.duration_ms:.0f}ms wall={result.elapsed_s:.1f}s "
        f"tokens in={result.prompt_tokens} (cache hit={result.cache_hit_tokens}) "
        f"out={result.completion_tokens} total={result.total_tokens} "
        f"est={cost_text}"
        + (f" (resumed, {result.replayed_tools} tool result(s) replayed)"
           if result.resumed else ""),
        file=sys.stderr,
    )
    if result.malformed_call_ids:
        # Not fatal, and not fixable here: the id is the provider's. Printed
        # because two tool messages sharing an id is a 400 you want to have
        # predicted rather than debugged.
        print(
            f"  WARNING: {len(result.malformed_call_ids)} tool call(s) arrived with "
            "a missing or duplicated id. They were answered in order anyway.",
            file=sys.stderr,
        )

    # Session 8, D74/D75. The results were always correct; what was missing
    # was the bill. A model that keeps calling a tool that does not exist pays
    # one tool call each time and gets a correct answer each time, and without
    # this line the run just looks slow.
    if result.tool_errors:
        counts: dict[str, int] = {}
        for code in result.tool_errors:
            counts[code] = counts.get(code, 0) + 1
        print(
            "  tool errors: "
            + ", ".join(f"{code} x{n}" for code, n in counts.items()),
            file=sys.stderr,
        )
    # Session 9. Session 8 printed this as a warning that nothing acted on.
    # It is now the measurement behind a rule, so it reports both outcomes -
    # a repeated success is the case D78 is about and was invisible here -
    # and it says whether the rule is even armed. A count with no rule and a
    # count under a rule look identical on screen otherwise, which is exactly
    # the confusion Session 12's control arm would inherit.
    if result.repeated_failures or result.repeated_calls:
        armed = (
            result.budget.max_repeated_failures is not None
            or result.budget.max_repeated_calls is not None
        )
        print(
            f"  repeats: {result.repeated_failures} failing, "
            f"{result.repeated_calls} succeeding, longest run "
            f"{result.longest_repeat}"
            + ("" if armed else " (rule off: nothing stops for this)"),
            file=sys.stderr,
        )

    # Session 6, D52. Printed from report_totals, which runs on EVERY stop -
    # a limit, a crash-stop, or a clean answer. A user who cannot tell which
    # files a stopped agent touched will not run it a second time, and the
    # stop they most need this for is the one where the agent did not get to
    # explain itself.
    #
    # Ordered by first touch, de-duplicated for reading, with the repeat count
    # kept: "twice" is information, because the second edit was matched
    # against a file the first had already changed.
    if result.changed_paths:
        counts: dict[str, int] = {}
        for changed in result.changed_paths:
            counts[changed] = counts.get(changed, 0) + 1
        print(
            f"  changed {len(counts)} file(s): "
            + ", ".join(
                name + (f" (x{n})" if n > 1 else "") for name, n in counts.items()
            ),
            file=sys.stderr,
        )


def explain_limit(result: LoopResult) -> str:
    """Why this run stopped, in the units the user set the limit in."""
    budget = result.budget
    reason = result.stop_reason
    if reason == STOP_STEP_LIMIT:
        return f"{budget.max_steps} steps used without a final answer"
    if reason == STOP_TOOL_LIMIT:
        return (
            f"the run's tool-call budget ({budget.max_tool_calls}) is spent; "
            f"{result.tool_calls} call(s) ran"
        )
    if reason == STOP_TIME_LIMIT:
        return (
            f"the run passed its time limit ({budget.max_seconds:g}s) "
            f"after {result.elapsed_s:.1f}s"
        )
    if reason == STOP_TOKEN_LIMIT:
        return (
            f"{result.total_tokens} tokens used of {budget.max_total_tokens}; "
            f"another call needs {budget.token_reserve} in reserve, so it was "
            "never made"
        )
    if reason == STOP_REPETITION:
        limit = (
            budget.max_repeated_calls
            if result.repetition_stopped_on and result.longest_repeat
            else budget.max_repeated_failures
        )
        return (
            f"the same call was made {result.longest_repeat} times without "
            f"making progress: {result.repetition_stopped_on}"
            + (f" (limit {limit})" if limit else "")
        )
    if reason == STOP_CONTEXT_LIMIT:
        size = result.context
        estimated = size.estimated_tokens if size else 0
        return (
            f"the conversation is ~{estimated} tokens and would not fit the "
            f"{budget.context_window}-token context window with "
            f"{budget.token_reserve} held for the reply; the next call was "
            "never made"
        )
    if reason == STOP_SPEND_LIMIT:
        spent = result.cost_usd or 0.0
        return (
            f"estimated spend ${spent:.6f} plus the reserve exceeds "
            f"${budget.max_spend:.6f}; the next call was never made"
        )
    return reason


def report_raw(response: ModelResponse) -> None:
    print("--- raw provider response (last step) ---", file=sys.stderr)
    print(json.dumps(response.raw, indent=2), file=sys.stderr)


def _open_resumed_run(run_id: str, root: Path) -> tuple[Journal, ResumeState] | int:
    """Reopen a dead run's journal and replay it, or return an exit code.

    Split out of main so the two failure modes a resume has - no journal, and
    a journal that cannot be parsed - do not nest two try blocks inside the
    branch that only exists to choose between resuming and starting.
    """
    try:
        journal = Journal.open(RUNS_DIR, run_id)
    except FileNotFoundError as err:
        print(f"error [no_journal]: {err}", file=sys.stderr)
        return EXIT_USAGE
    print(f"resuming run {run_id}", file=sys.stderr)

    # Explicit, not left to the first append: a repair that discards bytes is
    # a fact about the crash and belongs on screen, not in silence.
    discarded = journal.heal()
    if discarded:
        print(
            f"  repaired: dropped {discarded} bytes of a torn final record "
            "(the event it described never completed)",
            file=sys.stderr,
        )

    try:
        resume = rebuild(journal.events())
    except JournalCorrupt as err:
        print(f"error [journal_corrupt]: {err}", file=sys.stderr)
        return EXIT_USAGE

    report_resume(resume, root)
    return journal, resume


def _start_new_run(
    options, budget: Budget, provider, retry: RetryPolicy
) -> tuple[Journal, list[dict]]:
    """A fresh journal and the opening two messages."""
    run_id = new_run_id()
    journal = Journal.create(RUNS_DIR, run_id)
    # Printed FIRST: after a crash this line is the only way back in.
    print(f"run {run_id}  (resume with --resume={run_id})", file=sys.stderr)
    print(f"budget: {budget.describe()}", file=sys.stderr)
    print(f"retry:  {retry.describe()}", file=sys.stderr)
    _warn_about_context(provider)
    return journal, [
        {"role": "system", "content": options.system_prompt},
        {"role": "user", "content": options.task},
    ]


def main(argv: list[str]) -> int:
    load_config()

    if "--help" in argv[1:] or "-h" in argv[1:]:
        print(USAGE)
        return 0

    options = parse_argv(argv)
    if options is None:
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE

    # Resolved once, before anything is spent. A workspace that does not
    # exist is not a boundary problem - every path inside it would come back
    # not_found, which reads as a broken agent rather than a mistyped flag.
    if options.resume_run_id:
        try:
            recorded = recorded_workspace(RUNS_DIR, options.resume_run_id)
        except FileNotFoundError as err:
            print(f"error [no_journal]: {err}", file=sys.stderr)
            return EXIT_USAGE
        except (JournalCorrupt, ValueError, OSError) as err:
            print(f"error [resume]: {err}", file=sys.stderr)
            return EXIT_USAGE
        try:
            if options.workspace is not None and workspace_root(options.workspace) != recorded:
                print("error [workspace]: --workspace conflicts with the journal", file=sys.stderr)
                return EXIT_USAGE
            root = workspace_root(recorded)
        except ValueError as err:
            print(f"error [workspace]: {err}", file=sys.stderr)
            return EXIT_USAGE
    else:
        try:
            root = workspace_root(options.workspace)
        except ValueError as err:
            print(f"error [workspace]: {err}", file=sys.stderr)
            return EXIT_USAGE
    if not root.is_dir():
        print(
            f"error [usage]: workspace {root} does not exist or is not a "
            "directory. Create it or choose an existing project directory.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if not ensure_trusted(root, explicit=options.workspace is not None or bool(options.resume_run_id)):
        return EXIT_USAGE
    print(f"workspace: {root}", file=sys.stderr)

    # A local model is free, and "free" is a fact about the provider, not a
    # price list the environment forgot to set. See Pricing.free().
    try:
        provider = active_provider()
    except ModelError as err:
        return report_model_error(err)
    pricing = Pricing.free() if provider.free else Pricing.from_env()

    budget = options.budget(pricing, context_window=provider.context_window)
    # D38. A spend limit with no price list is not "unlimited" - it is a
    # number on screen that nothing enforces, which is worse than no number.
    if budget.needs_pricing:
        print(
            f"error [usage]: {MAX_SPEND_FLAG} needs prices. Set "
            "PRICE_INPUT_CACHE_HIT_PER_M, PRICE_INPUT_CACHE_MISS_PER_M and "
            "PRICE_OUTPUT_PER_M in .env from the current price list.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    # Read from .env like every other rule set, and printed with them. A
    # retry policy nobody can see is latency with no explanation.
    retry = RetryPolicy.from_env()

    messages: list[dict] = []
    resume: ResumeState | None = None

    if options.resume_run_id:
        opened = _open_resumed_run(options.resume_run_id, root)
        if isinstance(opened, int):
            return opened
        journal, resume = opened
    else:
        journal, messages = _start_new_run(options, budget, provider, retry)

    try:
        result = run_loop(
            messages,
            root=root,
            tools=options.tool_schemas,
            budget=budget,
            on_response=print_response,
            on_tool=print_tool,
            on_context=print_context,
            journal=journal,
            dispatcher=approving_dispatcher(),
            resume=resume,
            retry=retry,
        )
    except ModelError as err:
        return report_model_error(err)

    report_totals(result)
    if options.show_raw and result.responses:
        report_raw(result.responses[-1])

    if result.stopped_by_limit:
        # D28, extended to every limit: no partial answer reaches stdout, no
        # matter which budget ended the run.
        print(
            f"incomplete [{result.stop_reason}]: {explain_limit(result)}. "
            "Nothing written to stdout - a partial answer is not an answer.",
            file=sys.stderr,
        )
        return LIMIT_EXIT_CODES.get(result.stop_reason, EXIT_UNKNOWN)

    if result.text is not None:
        print(result.text)
    return 0


def console_main() -> int:
    """Entry point installed as the ``mini-agent`` command."""
    return main(sys.argv)


if __name__ == "__main__":
    raise SystemExit(console_main())
