"""run_python: one workspace .py file, no shell, bounded time and output."""

import json
import os
from pathlib import Path

import pytest

from agent.tools import registry
from agent.tools.common import MAX_RUN_OUTPUT_BYTES
from agent.tools.execution import run_python
from agent.tools.registry import dispatch


def write(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")


def test_runs_a_script_and_returns_exit_code_and_output(tmp_path):
    write(tmp_path, "hi.py", "print('hello')")
    result = run_python("hi.py", root=tmp_path)
    assert result.ok and "exit code: 0" in result.content and "hello" in result.content


def test_nonzero_exit_is_a_failed_result_with_the_output(tmp_path):
    write(tmp_path, "bad.py", "import sys; print('boom'); sys.exit(3)")
    result = run_python("bad.py", root=tmp_path)
    assert not result.ok and result.error_code == "execution_failed"
    assert "exit code: 3" in result.content and "boom" in result.content


def test_only_py_files_and_existing_files_are_accepted(tmp_path):
    write(tmp_path, "notes.txt", "x")
    assert run_python("notes.txt", root=tmp_path).error_code == "invalid_arguments"
    assert run_python("missing.py", root=tmp_path).error_code == "not_found"


def test_paths_outside_the_workspace_are_refused(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    write(tmp_path, "outside.py", "print('escaped')")
    result = run_python("../outside.py", root=root)
    assert not result.ok and "escaped" not in result.content


def test_symlink_to_a_script_is_refused(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    write(tmp_path, "real.py", "print('via link')")
    os.symlink(tmp_path / "real.py", root / "link.py")
    result = run_python("link.py", root=root)
    assert not result.ok and "via link" not in result.content


def test_output_is_truncated(tmp_path):
    write(tmp_path, "loud.py", f"print('x' * {MAX_RUN_OUTPUT_BYTES * 2})")
    result = run_python("loud.py", root=tmp_path)
    assert "output truncated" in result.content
    assert len(result.content) < MAX_RUN_OUTPUT_BYTES + 200


def test_timeout_kills_the_script(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.tools.execution.MAX_RUN_SECONDS", 1)
    write(tmp_path, "slow.py", "import time; time.sleep(30)")
    result = run_python("slow.py", root=tmp_path)
    assert result.error_code == "execution_timeout"


def test_child_does_not_receive_api_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_APIKEY", "secret-value")
    write(tmp_path, "env.py", "import os; print(os.getenv('DEEPSEEK_APIKEY'))")
    result = run_python("env.py", root=tmp_path)
    assert "secret-value" not in result.content and "None" in result.content


def test_runs_inside_the_workspace_and_can_import_its_modules(tmp_path):
    write(tmp_path, "helper.py", "VALUE = 7")
    write(tmp_path, "main.py", "import helper, os; print(helper.VALUE, os.getcwd())")
    result = run_python("main.py", root=tmp_path)
    assert result.ok and "7" in result.content and str(tmp_path.resolve()) in result.content


def test_dispatch_rejects_extra_arguments_and_unknown_shapes(tmp_path):
    write(tmp_path, "hi.py", "print(1)")
    assert not dispatch("run_python", json.dumps({"path": "hi.py", "args": ["--x"]}), root=tmp_path).ok
    assert not dispatch("run_python", "not json", root=tmp_path).ok


def test_run_python_counts_as_a_writer_and_is_never_replayed():
    assert "run_python" in registry.WRITES
