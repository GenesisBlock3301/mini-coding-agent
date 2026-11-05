"""Entry point: accept the user's task from the command line and print the result.

Owns: argument parsing, loading the API key, printing the trace and the final
answer, and turning a ModelError into one actionable line. Owns no agent
logic - the loop lives in agent/loop.py.

    python -m agent.cli "say hello in five words"
    python -m agent.cli --tools "what's in the awkward folder?"
    python -m agent.cli --tools --max-steps=3 "read src/utils.py and summarise it"

Can also be run straight from an IDE with no arguments: it then falls back to
the DEBUG_TASK preset at the bottom of this file.

With --tools the model is offered list_files and read_file and the loop runs:
tool call -> execution -> tool result -> model, until the model answers
without asking for a tool, or max_steps model calls have been made.

stdout gets the answer and nothing else. The trace, the metrics and the errors
go to stderr, so Session 11's eval runner can read the answer without parsing
around them. A step-limit stop writes NOTHING to stdout: a partial answer is
not an answer (D28).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

# Running this file directly (PyCharm's green arrow) puts agent/ on sys.path
# instead of the project root, so `import agent.model` fails before main() is
# ever reached. Launching with `python -m agent.cli` sets __package__ and this
# is a no-op.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from agent.loop import DEFAULT_MAX_STEPS, STOP_STEP_LIMIT, LoopResult, run_loop
from agent.model import ModelError, ModelResponse
from agent.policy import workspace_root
from agent.tools import TOOL_SCHEMAS, ToolResult

SYSTEM_PROMPT = "You are a terse assistant."

# Separate prompt for --tools. It DESCRIBES the workspace rule so the model
# wastes fewer turns discovering it by rejection. It does not enforce it:
# delete this paragraph and the agent can do exactly the same set of things,
# just less gracefully. See docs/session-04.md E1.
#
# The last two sentences are the optimistic-path rule: guess, then recover.
# Without them the model spends its first call on list_files('.') to find out
# where things live. See docs/session-04.md E2.
TOOLS_SYSTEM_PROMPT = (
    "You are a terse coding assistant with two tools: list_files and "
    "read_file. All paths are relative to a workspace root - never absolute, "
    "never outside it. Call a tool when the answer depends on what is "
    "actually on disk. "
    "When the user names a file or folder, treat that name as a "
    "workspace-relative path and call the tool on it directly - do not list "
    "the root first to go looking for it. Only list a parent directory if "
    "the path you tried is reported missing."
)

# Named once so the parser, the usage line and the debug presets below cannot
# drift apart.
TOOLS_FLAG = "--tools"
RAW_FLAG = "--raw"
MAX_STEPS_FLAG = "--max-steps="
USAGE = (
    f'usage: python -m agent.cli "your task" '
    f"[{TOOLS_FLAG}] [{RAW_FLAG}] [{MAX_STEPS_FLAG}N]"
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


@dataclass(frozen=True)
class Options:
    """One run's settings, already validated. Nothing downstream reads argv."""

    task: str
    use_tools: bool
    show_raw: bool
    max_steps: int = DEFAULT_MAX_STEPS

    @property
    def system_prompt(self) -> str:
        return TOOLS_SYSTEM_PROMPT if self.use_tools else SYSTEM_PROMPT

    @property
    def tool_schemas(self) -> list[dict] | None:
        return TOOL_SCHEMAS if self.use_tools else None


def parse_argv(argv: list[str]) -> Options | None:
    """Options, or None when the task is missing, blank or --max-steps is junk.

    Flags are order-free and may sit on either side of the task. Anything that
    is not a flag is positional; only the first positional is used.
    """
    flags = {a for a in argv[1:] if a.startswith("--")}
    positional = [a for a in argv[1:] if not a.startswith("--")]

    if not positional or not positional[0].strip():
        return None

    max_steps = DEFAULT_MAX_STEPS
    for flag in flags:
        if not flag.startswith(MAX_STEPS_FLAG):
            continue
        value = flag[len(MAX_STEPS_FLAG):]
        if not value.isdigit() or int(value) < 1:
            return None
        max_steps = int(value)

    return Options(
        task=positional[0],
        use_tools=TOOLS_FLAG in flags,
        show_raw=RAW_FLAG in flags,
        max_steps=max_steps,
    )


def report_model_error(err: ModelError) -> int:
    """Print one actionable line about a failed call; return its exit code."""
    print(f"error [{err.kind}]: {err.user_message}", file=sys.stderr)
    if err.detail:
        print(f"  detail: {err.detail}", file=sys.stderr)
    print(
        f"  retryable={err.retryable} safe_to_retry={err.safe_to_retry}",
        file=sys.stderr,
    )
    return EXIT_CODES.get(err.kind, EXIT_UNKNOWN)


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


def report_totals(result: LoopResult) -> None:
    """The whole run, which is what you actually pay for."""
    print(
        f"\n[total] stop={result.stop_reason} steps={result.steps} "
        f"{result.duration_ms:.0f}ms "
        f"tokens in={result.prompt_tokens} (cache hit={result.cache_hit_tokens}) "
        f"out={result.completion_tokens} total={result.total_tokens}",
        file=sys.stderr,
    )


def report_raw(response: ModelResponse) -> None:
    print("--- raw provider response (last step) ---", file=sys.stderr)
    print(json.dumps(response.raw, indent=2), file=sys.stderr)


def main(argv: list[str]) -> int:
    load_dotenv()

    options = parse_argv(argv)
    if options is None:
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE

    messages = [
        {"role": "system", "content": options.system_prompt},
        {"role": "user", "content": options.task},
    ]

    try:
        result = run_loop(
            messages,
            root=workspace_root(),
            tools=options.tool_schemas,
            max_steps=options.max_steps,
            on_response=print_response,
            on_tool=print_tool,
        )
    except ModelError as err:
        return report_model_error(err)

    report_totals(result)
    if options.show_raw and result.responses:
        report_raw(result.responses[-1])

    if result.stop_reason == STOP_STEP_LIMIT:
        print(
            f"incomplete: {options.max_steps} steps used without a final answer. "
            "Nothing written to stdout - a partial answer is not an answer.",
            file=sys.stderr,
        )
        return EXIT_STEP_LIMIT

    if result.text is not None:
        print(result.text)
    return 0


# Presets for running this file straight from the IDE, where no command-line
# arguments are passed. Change DEBUG_TASK to pick one; real command-line
# arguments always win, so shell behaviour is unchanged.
DEBUG_TASKS = {
    "awkward": [TOOLS_FLAG, "what's in the awkward folder?"],
    "escape": [TOOLS_FLAG, "read ../playground-evil/loot.txt"],
    "injection": [TOOLS_FLAG, RAW_FLAG, "show me read/injection.md"],
}
DEBUG_TASK = "awkward"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv if len(sys.argv) > 1 else ["agent.cli", *DEBUG_TASKS[DEBUG_TASK]]))
