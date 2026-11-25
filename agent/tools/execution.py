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

def sandbox_prefix(root: Path) -> list[str]:
    """macOS only: confine the child with sandbox-exec, else return [].

    Defence in depth, not a complete sandbox. It denies all network access,
    denies writes outside the workspace and the temp dirs, and denies reading
    the agent's own state and the usual credential folders. Set
    MINI_AGENT_NO_SANDBOX=1 to run unconfined (the result says which).
    Linux has no equivalent wired in: scripts there run unconfined.
    """
    if sys.platform != "darwin" or os.getenv("MINI_AGENT_NO_SANDBOX") == "1":
        return []
    exe = "/usr/bin/sandbox-exec"
    if not os.access(exe, os.X_OK):
        return []
    from agent.config import config_path
    from agent.storage import PATHS

    home = Path.home().resolve()
    allow_write = [Path(root).resolve(), Path("/private/tmp"), Path("/private/var/folders")]
    deny_read = [home / ".ssh", home / ".aws", home / ".gnupg", config_path().parent, PATHS.root]
    quote = lambda path: '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'
    profile = "(version 1)(allow default)(deny network*)(deny file-write*)"
    profile += "(allow file-write* (literal \"/dev/null\") (literal \"/dev/tty\") " + " ".join(
        f"(subpath {quote(path)})" for path in allow_write) + ")"
    profile += "(deny file-read* " + " ".join(f"(subpath {quote(path)})" for path in deny_read) + ")"
    return [exe, "-p", profile]


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
    sandbox = sandbox_prefix(root)
    try:
        process = subprocess.Popen(
            [*sandbox, sys.executable, str(target)],
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
        content=(f"exit code: {process.returncode}\n{shown}" if shown else f"exit code: {process.returncode} (no output)")
        + ("" if sandbox else "\n(ran without a sandbox)"),
        error_code=None if process.returncode == 0 else "execution_failed",
    )


