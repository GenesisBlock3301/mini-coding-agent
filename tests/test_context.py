"""Tests for agent.context - how big the conversation is, and the stop.

Session 9, Phase 3. The measure is characters/4 and is wrong by a factor that
moves with the content; these tests pin down the SHAPE of the decision it
feeds, not the accuracy of the estimate. Where accuracy matters, the ratio
against the provider's own `prompt_tokens` is asserted instead - which is the
error bar, and the only honest thing to assert about an estimator.
"""

import json

import pytest

from agent.budget import STOP_CONTEXT_LIMIT, Budget, Ledger, check_before_step
from agent.context import calibrate, estimate_tokens, measure, message_chars
from agent.loop import run_loop
from agent.model import ModelResponse


class ScriptedModel:
    def __init__(self, *responses: ModelResponse) -> None:
        self.script = list(responses)
        self.calls = 0

    def __call__(self, messages, *, tools=None):
        if self.calls >= len(self.script):
            raise AssertionError("the loop asked for more model calls than scripted")
        response = self.script[self.calls]
        self.calls += 1
        return response


def call(call_id, name, **arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def wants(*calls) -> ModelResponse:
    return ModelResponse(
        text=None, model="scripted", finish_reason="tool_calls",
        prompt_tokens=10, completion_tokens=5, total_tokens=15,
        duration_ms=0.0, tool_calls=list(calls),
    )


def says(text="done") -> ModelResponse:
    return ModelResponse(
        text=text, model="scripted", finish_reason="stop",
        prompt_tokens=10, completion_tokens=5, total_tokens=15,
        duration_ms=0.0, tool_calls=[],
    )


@pytest.fixture
def big_workspace(tmp_path):
    """Ten files, 30KB each. Each one fits under read_file's 64KB cap, so
    nothing here is truncated - which is the point of step 8: the per-call
    cap is doing its job and the history still grows without bound."""
    for n in range(10):
        (tmp_path / f"big_{n}.py").write_text("x = 1  # padding\n" * 1800, encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------
# The measure
# --------------------------------------------------------------------------


def test_the_estimate_counts_tool_calls_and_not_only_content():
    """An agent transcript is mostly tool_calls by step four. Counting
    `content` alone under-reads it by exactly the part that makes it an
    agent transcript."""
    plain = {"role": "assistant", "content": "ok"}
    with_calls = {
        "role": "assistant",
        "content": "ok",
        "tool_calls": [call("c1", "replace_text", path="a.py", old="x" * 400, new="y")],
    }

    assert message_chars(with_calls) > message_chars(plain) + 400


def test_an_empty_history_is_not_a_negative_number():
    assert estimate_tokens([]) == 0


def test_the_estimate_grows_with_the_history():
    one = [{"role": "user", "content": "a" * 4000}]
    two = one + [{"role": "assistant", "content": "b" * 4000}]

    assert estimate_tokens(two) > estimate_tokens(one)


def test_the_ratio_is_the_error_bar_and_nothing_is_corrected_by_it():
    """D85. estimate/exact is reported, never applied: scaling the estimate
    by a ratio measured on the previous request is a second estimator with
    no error bar of its own."""
    size = measure(
        [{"role": "user", "content": "a" * 4000}],
        window=10_000,
        last_prompt_tokens=800,
    )

    assert size.ratio == pytest.approx(size.estimated_tokens / 800)
    assert calibrate(1000, 0) is None, "no exact number, no ratio"
    assert "est/actual" in size.line()


def test_half_a_window_is_where_a_human_should_notice():
    quiet = measure([{"role": "user", "content": "a" * 400}], window=10_000)
    loud = measure([{"role": "user", "content": "a" * 40_000}], window=10_000)

    assert not quiet.loud
    assert loud.loud


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_a_history_just_under_the_line_starts_the_step():
    budget = Budget(context_window=10_000, token_reserve=2_000)

    assert check_before_step(budget, Ledger(), history_tokens=7_999) is None


def test_a_history_just_over_the_line_does_not():
    budget = Budget(context_window=10_000, token_reserve=2_000)

    assert check_before_step(budget, Ledger(), history_tokens=8_001) == STOP_CONTEXT_LIMIT


def test_an_unmeasured_context_is_never_a_reason_to_stop():
    """None means the caller did not measure. A limit enforced against a
    number nobody computed is a limit that fires at random."""
    budget = Budget(context_window=10)

    assert check_before_step(budget, Ledger(), history_tokens=None) is None


def test_no_window_means_no_check():
    """The honest setting for a provider whose window you do not know."""
    budget = Budget(context_window=None)

    assert check_before_step(budget, Ledger(), history_tokens=10_000_000) is None


def test_the_context_check_runs_before_the_clock():
    """Deterministic limits first: `context_limit` reproduces on a re-run of
    the same trace, `time_limit` depends on how busy the machine was, and
    Session 12 compares runs by the reason they stopped."""
    budget = Budget(context_window=10_000, token_reserve=2_000, max_seconds=0.0)

    assert check_before_step(budget, Ledger(), history_tokens=50_000) == STOP_CONTEXT_LIMIT


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def test_ten_large_reads_stop_the_run_before_the_provider_does(big_workspace):
    """D87, answered with no new limit.

    Ten 30KB reads is 300KB of history that no per-call cap notices - the
    cap is per call and the conversation is the sum. The run-level bound is
    the context check, measured in the unit that actually matters (what the
    next request carries) rather than in bytes of tool output.
    """
    model = ScriptedModel(
        *[wants(call(f"c{n}", "read_file", path=f"big_{n}.py")) for n in range(10)],
        says(),
    )

    result = run_loop(
        [{"role": "user", "content": "read everything"}],
        root=big_workspace,
        model_fn=model,
        budget=Budget(max_steps=20, max_tool_calls=50, context_window=32_768),
    )

    assert result.stop_reason == STOP_CONTEXT_LIMIT
    assert model.calls < 10, "it stopped before asking for the request that would fail"
    assert result.context.estimated_tokens > 20_000


def test_the_run_reports_the_size_that_stopped_it(big_workspace):
    from agent.cli import explain_limit

    model = ScriptedModel(
        *[wants(call(f"c{n}", "read_file", path=f"big_{n}.py")) for n in range(10)],
        says(),
    )
    result = run_loop(
        [{"role": "user", "content": "read everything"}],
        root=big_workspace,
        model_fn=model,
        budget=Budget(max_steps=20, max_tool_calls=50, context_window=32_768),
    )

    explanation = explain_limit(result)
    assert "32768-token context window" in explanation
    assert "would not fit" in explanation


def test_the_context_stop_has_its_own_exit_code():
    """D88 and D40: a runner has to tell this from a step limit without
    parsing English."""
    from agent.cli import LIMIT_EXIT_CODES

    assert LIMIT_EXIT_CODES[STOP_CONTEXT_LIMIT] == 17
    assert len(set(LIMIT_EXIT_CODES.values())) == len(LIMIT_EXIT_CODES)


def test_a_tiny_window_refuses_to_start_at_all(big_workspace):
    """The Ollama case: a server handing out 4096 tokens silently drops the
    OLDEST messages on overflow, including the assistant turn whose
    tool_call ids are still being answered. Refusing loudly before the first
    request is the only version of this that leaves a readable trace."""
    model = ScriptedModel(says())

    result = run_loop(
        [{"role": "user", "content": "x" * 20_000}],
        root=big_workspace,
        model_fn=model,
        budget=Budget(max_steps=5, context_window=4_096),
    )

    assert result.stop_reason == STOP_CONTEXT_LIMIT
    assert model.calls == 0, "the request was never sent"


def test_the_measurement_is_reported_on_every_stop(big_workspace):
    """Not only on the stop it caused. A number that only exists when a rule
    fires cannot tell you the threshold is wrong."""
    result = run_loop(
        [{"role": "user", "content": "hello"}],
        root=big_workspace,
        model_fn=ScriptedModel(says()),
        max_steps=5,
    )

    assert result.ok
    assert result.context is not None
    assert result.context.estimated_tokens > 0


def test_the_callback_sees_the_size_before_each_step(big_workspace):
    seen = []
    run_loop(
        [{"role": "user", "content": "hello"}],
        root=big_workspace,
        model_fn=ScriptedModel(wants(call("c0", "read_file", path="big_0.py")), says()),
        max_steps=5,
        on_context=lambda step, size: seen.append((step, size.estimated_tokens)),
    )

    assert [step for step, _ in seen] == [1, 2]
    assert seen[1][1] > seen[0][1], "the history grew by one 30KB read"
