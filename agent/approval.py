"""Ask the person before a script runs. (run_python only.)

Claude Code asks before it runs a command; this does the same for the one tool
that is not contained by the workspace policy. A refusal comes back to the model
as a tool result, so it can say what it needs instead of retrying.

Set ALLOW_RUN_PYTHON=1 to skip the question (for scripts and evals).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Callable

from agent.tools import ToolResult, dispatch


def _path(arguments: str) -> str:
    try:
        value = json.loads(arguments).get("path")
        return value if isinstance(value, str) else "?"
    except (ValueError, AttributeError):
        return "?"


def approving_dispatcher(
    ask: Callable[[str], str] = input,
    *,
    interactive: bool | None = None,
) -> Callable[..., ToolResult]:
    """A dispatcher that gates run_python. Other tools pass straight through.

    "a" at the prompt allows run_python for the rest of this dispatcher's life
    (one chat session); anything but y/a is a refusal.
    """
    always = False

    def dispatcher(name: str, arguments: str, *, root):
        nonlocal always
        if name != "run_python" or always or os.getenv("ALLOW_RUN_PYTHON") == "1":
            return dispatch(name, arguments, root=root)
        tty = sys.stdin.isatty() if interactive is None else interactive
        if not tty:
            return ToolResult(
                ok=False,
                content="Running Python needs the user's approval and there is no terminal to ask. "
                        "Tell the user; do not retry.",
                error_code="approval_required",
            )
        path = _path(arguments)
        print(f"\nThe agent wants to run: {path}\n  in {root}", file=sys.stderr)
        try:
            answer = ask("Run it? [y]es / [a]lways this session / [N]o: ").strip().lower()
        except (EOFError, OSError):
            answer = ""
        if answer in ("a", "always"):
            always = True
        elif answer not in ("y", "yes"):
            return ToolResult(
                ok=False,
                content=f"The user declined to run {path}. Do not run it. Ask them what they want instead.",
                error_code="denied_by_user",
            )
        return dispatch(name, arguments, root=root)

    return dispatcher
