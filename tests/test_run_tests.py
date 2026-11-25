"""run_tests: the whole suite, never a silent pass; and the loop's reminder."""

import json
from pathlib import Path

import pytest

from agent.budget import Budget
from agent.loop import run_loop
from agent.model import ModelResponse
from agent.tools.execution import run_tests
from agent.tools.registry import dispatch


def project(root: Path, *, passing: bool = True) -> Path:
    (root / "tests").mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    expected = "3" if passing else "4"
    (root / "tests" / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\n"
        "class T(unittest.TestCase):\n    def test_add(self):\n"
        f"        self.assertEqual(add(1, 2), {expected})\n"
    )
    return root


def test_passing_suite_is_ok_and_shows_the_summary(tmp_path):
    result = run_tests(root=project(tmp_path))
    assert result.ok and "OK" in result.content and "Ran 1 test" in result.content


def test_failing_suite_is_a_failure_with_the_reason(tmp_path):
    result = run_tests(root=project(tmp_path, passing=False))
    assert not result.ok and result.error_code == "execution_failed"
    assert "FAILED" in result.content and "AssertionError" in result.content


def test_no_tests_is_an_error_not_a_pass(tmp_path):
    (tmp_path / "calc.py").write_text("x = 1\n")
    result = run_tests(root=tmp_path)
    assert not result.ok and result.error_code == "no_tests_found"


def test_falls_back_to_the_root_when_there_is_no_tests_folder(tmp_path):
    (tmp_path / "test_x.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(True)\n"
    )
    assert run_tests(root=tmp_path).ok


def test_long_output_keeps_the_end_where_the_summary_is(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_big.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_big(self):\n"
        "        print('x' * 20000)\n        self.assertTrue(True)\n"
    )
    result = run_tests(root=tmp_path)
    assert result.ok and "OK" in result.content and "earlier output cut" in result.content


def test_it_takes_no_arguments(tmp_path):
    project(tmp_path)
    assert dispatch("run_tests", "{}", root=tmp_path).ok
    assert not dispatch("run_tests", json.dumps({"path": "tests"}), root=tmp_path).ok


# --- the loop's reminder and the unverified flag --------------------------

def reply(text=None, calls=()):
    return ModelResponse(
        text=text, model="scripted", finish_reason="tool_calls" if calls else "stop",
        prompt_tokens=10, completion_tokens=5, total_tokens=15, duration_ms=1.0,
        tool_calls=[
            {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(calls)
        ],
    )


class Scripted:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.seen = []

    def __call__(self, messages, *, tools=None, **kw):
        self.seen.append([dict(m) for m in messages])
        return self.responses.pop(0)


EDIT = ("replace_text", {"path": "calc.py", "old": "a + b", "new": "a + b  # sum"})


def run(root, *responses):
    model = Scripted(*responses)
    result = run_loop(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}],
        root=root, budget=Budget(max_steps=6), model_fn=model,
    )
    return result, model


def test_an_edit_result_tells_the_model_to_run_tests_next(tmp_path):
    project(tmp_path)
    _, model = run(tmp_path, reply(calls=[EDIT]), reply("done"))
    tool_msg = [m for m in model.seen[1] if m.get("role") == "tool"][0]
    assert "call run_tests" in tool_msg["content"]


def test_edit_without_tests_ends_flagged_unverified(tmp_path):
    project(tmp_path)
    result, _ = run(tmp_path, reply(calls=[EDIT]), reply("done"))
    assert result.changed_paths and result.unverified_changes


def test_a_passing_run_tests_after_the_edit_clears_the_flag(tmp_path):
    project(tmp_path)
    result, _ = run(tmp_path, reply(calls=[EDIT]), reply(calls=[("run_tests", {})]), reply("done"))
    assert not result.unverified_changes


def test_a_failing_run_tests_leaves_it_flagged(tmp_path):
    project(tmp_path, passing=False)
    result, _ = run(tmp_path, reply(calls=[EDIT]), reply(calls=[("run_tests", {})]), reply("done"))
    assert result.unverified_changes


def test_a_later_edit_flags_again_after_a_pass(tmp_path):
    project(tmp_path)
    result, _ = run(
        tmp_path, reply(calls=[EDIT]), reply(calls=[("run_tests", {})]), reply(calls=[EDIT]), reply("done")
    )
    assert result.unverified_changes


def test_no_edit_means_nothing_to_verify(tmp_path):
    project(tmp_path)
    result, _ = run(tmp_path, reply("hello"))
    assert not result.unverified_changes
