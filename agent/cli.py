"""Entry point: accept the user's task from the command line and print the result.

Owns: argument parsing, loading the API key, printing the final answer plus
duration and token usage, and turning a ModelError into one actionable line.
Owns no agent logic.

    python -m agent.cli "say hello in five words"

Answer goes to stdout. Metrics and errors go to stderr, so Session 11's eval
runner can read the answer without parsing around them.
"""

from __future__ import annotations

import json
import sys

from dotenv import load_dotenv

from agent.model import ModelError, call_model

SYSTEM_PROMPT = "You are a terse assistant."

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


def main(argv: list[str]) -> int:
    load_dotenv()

    if len(argv) < 2 or not argv[1].strip():
        print('usage: python -m agent.cli "your task" [--raw]', file=sys.stderr)
        return 2

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": argv[1]},
    ]

    try:
        result = call_model(messages)
    except ModelError as err:
        print(f"error [{err.kind}]: {err.user_message}", file=sys.stderr)
        if err.detail:
            print(f"  detail: {err.detail}", file=sys.stderr)
        print(
            f"  retryable={err.retryable} safe_to_retry={err.safe_to_retry}",
            file=sys.stderr,
        )
        return EXIT_CODES.get(err.kind, 1)

    if result.text is not None:
        print(result.text)

    print(
        f"[{result.model}] finish={result.finish_reason} "
        f"{result.duration_ms:.0f}ms "
        f"tokens in={result.prompt_tokens} "
        f"(cache hit={result.prompt_cache_hit_tokens} "
        f"miss={result.prompt_cache_miss_tokens}) "
        f"out={result.completion_tokens} total={result.total_tokens}",
        file=sys.stderr,
    )
    if result.finish_reason != "stop":
        print(
            f"  warning: finish_reason={result.finish_reason!r} — "
            "this is a 200, but the answer is not complete.",
            file=sys.stderr,
        )
    if "--raw" in argv:
        print("--- raw provider response ---", file=sys.stderr)
        print(json.dumps(result.raw, indent=2), file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
