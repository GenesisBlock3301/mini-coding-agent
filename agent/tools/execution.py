"""Bounded Python script execution tool."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from agent.policy import PolicyViolation, echo_path, resolve_in_workspace
from .common import (
    MAX_RUN_OUTPUT_BYTES,
    MAX_RUN_SECONDS,
    ToolResult,
    _rejected,
    _tool_error,
)

def run_python(path: str, *, root: Path) -> ToolResult:
    """Run one workspace Python script, without a shell, under a hard timeout."""
    try:
        target = resolve_in_workspace(path, root=root)
    except PolicyViolation as violation:
        return _rejected(violation)
    if target.suffix != ".py":
        return _tool_error("invalid_arguments", "run_python accepts only .py files.")
    if not target.is_file():
        return _tool_error("not_found", f"{echo_path(path)} is not a file in the workspace.")

    # Do not pass API credentials or arbitrary shell configuration to the
    # child. This is not an OS sandbox: the script still runs as this user.
    child_env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP")
        if key in os.environ
    }
    child_env["PYTHONPATH"] = str(Path(root).resolve())
    try:
        process = subprocess.Popen(
            [sys.executable, str(target)],
            cwd=root,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            output, _ = process.communicate(timeout=MAX_RUN_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate()
            shown = output[:MAX_RUN_OUTPUT_BYTES].decode("utf-8", errors="replace")
            return _tool_error(
                "execution_timeout",
                f"Python execution timed out after {MAX_RUN_SECONDS}s. Output:\n{shown}",
            )
    except OSError as exc:
        return _tool_error("execution_failed", f"Could not run Python: {exc}")

    shown = output[:MAX_RUN_OUTPUT_BYTES].decode("utf-8", errors="replace")
    if len(output) > MAX_RUN_OUTPUT_BYTES:
        shown += f"\n(output truncated at {MAX_RUN_OUTPUT_BYTES} bytes)"
    return ToolResult(
        ok=process.returncode == 0,
        content=(f"exit code: {process.returncode}\n{shown}" if shown else f"exit code: {process.returncode} (no output)"),
        error_code=None if process.returncode == 0 else "execution_failed",
    )


