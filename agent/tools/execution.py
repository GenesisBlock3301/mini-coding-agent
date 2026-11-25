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

    return _execute([sys.executable, str(target)], root=root, what="Python execution")


def run_tests(*, root: Path) -> ToolResult:
    """Run the workspace's whole unittest suite: `python -m unittest discover`.

    No arguments, so the model cannot choose flags. Starts in `tests/` when it
    exists, otherwise the workspace root. Zero tests found is an ERROR, never a
    pass: "no tests" must not read as "all good". The END of the output is kept
    when it is long, because the summary line is at the end.
    """
    start = "tests" if (Path(root) / "tests").is_dir() else "."
    return _execute(
        [sys.executable, "-m", "unittest", "discover", "-s", start],
        root=root, what="Test run", keep_tail=True, no_tests_code=5,
    )


def _execute(argv, *, root, what, keep_tail=False, no_tests_code=None) -> ToolResult:
    # Do not pass API credentials or arbitrary shell configuration to the
    # child. The macOS sandbox, when present, confines it further.
    child_env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP")
        if key in os.environ
    }
    child_env["PYTHONPATH"] = str(Path(root).resolve())
    sandbox = sandbox_prefix(root)

    def show(raw: bytes) -> str:
        text = (raw[-MAX_RUN_OUTPUT_BYTES:] if keep_tail else raw[:MAX_RUN_OUTPUT_BYTES]).decode("utf-8", errors="replace")
        if len(raw) > MAX_RUN_OUTPUT_BYTES:
            text = ("(earlier output cut)\n" + text) if keep_tail else text + f"\n(output truncated at {MAX_RUN_OUTPUT_BYTES} bytes)"
        return text

    try:
        process = subprocess.Popen(
            [*sandbox, *argv],
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
            return _tool_error(
                "execution_timeout",
                f"{what} timed out after {MAX_RUN_SECONDS}s. Output:\n{show(output)}",
            )
    except OSError as exc:
        return _tool_error("execution_failed", f"Could not run: {exc}")

    shown = show(output)
    note = "" if sandbox else "\n(ran without a sandbox)"
    if no_tests_code is not None and process.returncode == no_tests_code:
        return _tool_error("no_tests_found", f"No tests were found, so nothing was verified.\n{shown}{note}")
    return ToolResult(
        ok=process.returncode == 0,
        content=(f"exit code: {process.returncode}\n{shown}" if shown else f"exit code: {process.returncode} (no output)") + note,
        error_code=None if process.returncode == 0 else "execution_failed",
    )
