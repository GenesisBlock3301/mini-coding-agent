"""Tests for agent.loop - the batch contract and the stopping rules.

Every test here runs against a scripted model (Session 4, step 0): no API key,
no network, no sockets. The assertions that matter most are the ones about
`model.calls` - a budget that stops the run *after* paying is the bug this
session exists to prevent, and only a call count can see the difference.
"""

import time

import pytest

from agent.budget import (
    REFUSED_TOOL_ERROR,
    STOP_ANSWER,
    STOP_STEP_LIMIT,
    STOP_TIME_LIMIT,
    STOP_TOKEN_LIMIT,
    STOP_TOOL_LIMIT,
    Budget,
    Pricing,
)
from agent.journal import ResumeState
from agent.loop import run_loop
from agent.model import ModelResponse


# --------------------------------------------------------------------------
# The scripted model and two message builders.
# --------------------------------------------------------------------------


class ScriptedModel:
    """Returns prepared responses in order and counts how often it was asked."""

    def __init__(self, *responses: ModelResponse) -> None:
        self.script = list(responses)
        self.calls = 0

    def __call__(self, messages, *, tools=None):
        if self.calls >= len(self.script):
            raise AssertionError(
                f"the loop asked for model call {self.calls + 1}; "
                f"the script has {len(self.script)}"
            )
        response = self.script[self.calls]
        self.calls += 1
        return response


def call(call_id: str, name: str = "list_files", arguments: str = '{"path": "."}'):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def wants(*calls, text=None, prompt=10, completion=5) -> ModelResponse:
    return ModelResponse(
        text=text,
        model="scripted",
        finish_reason="tool_calls",
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        duration_ms=0.0,
        tool_calls=list(calls),
    )


def says(text: str, *, prompt=10, completion=5) -> ModelResponse:
    return ModelResponse(
        text=text,
        model="scripted",
        finish_reason="stop",
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        duration_ms=0.0,
    )


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n")
    (tmp_path / "b.txt").write_text("beta\n")
    return tmp_path


def tool_messages(result):
    return [m for m in result.messages if m.get("role") == "tool"]


# --------------------------------------------------------------------------
# Phase 1: the batch.
# --------------------------------------------------------------------------


def test_three_calls_in_one_turn_are_answered_in_order(workspace):
    model = ScriptedModel(
        wants(call("c1"), call("c2", arguments='{"path": "a.txt"}'), call("c3")),
        says("done"),
    )

    result = run_loop([], root=workspace, model_fn=model)

    assert [m["tool_call_id"] for m in tool_messages(result)] == ["c1", "c2", "c3"]
    assert result.tool_calls == 3


def test_a_broken_call_does_not_cancel_the_rest_of_the_batch(workspace):
    """One valid, one malformed JSON, one unknown tool. All three answered."""
    model = ScriptedModel(
        wants(
            call("c1"),
            call("c2", arguments="{not json"),
            call("c3", name="delete_everything"),
        ),
        says("done"),
    )

    result = run_loop([], root=workspace, model_fn=model)
    answers = tool_messages(result)

    assert [m["tool_call_id"] for m in answers] == ["c1", "c2", "c3"]
    assert "a.txt" in answers[0]["content"]
    assert answers[1]["content"].startswith("ERROR invalid_arguments:")
    assert answers[2]["content"].startswith("ERROR unknown_tool:")
    assert result.stop_reason == STOP_ANSWER


def test_results_follow_the_assistant_turn_that_asked_for_them(workspace):
    """role="tool" before its assistant message is a 400 on the next POST."""
    model = ScriptedModel(wants(call("c1")), says("done"))

    result = run_loop([], root=workspace, model_fn=model)
    roles = [m["role"] for m in result.messages]

    assert roles == ["assistant", "tool", "assistant"]
    assert result.messages[0]["tool_calls"][0]["id"] == "c1"


def test_duplicate_ids_are_answered_and_reported_not_dropped(workspace):
    model = ScriptedModel(wants(call("same"), call("same")), says("done"))

    result = run_loop([], root=workspace, model_fn=model)

    assert [m["tool_call_id"] for m in tool_messages(result)] == ["same", "same"]
    assert result.malformed_call_ids == ["same"]


def test_a_call_with_no_id_is_still_answered(workspace):
    model = ScriptedModel(
        wants({"type": "function", "function": {"name": "list_files",
                                                "arguments": '{"path": "."}'}}),
        says("done"),
    )

    result = run_loop([], root=workspace, model_fn=model)

    assert [m["tool_call_id"] for m in tool_messages(result)] == [""]
    assert result.malformed_call_ids == [""]


def test_text_alongside_tool_calls_is_kept_in_history(workspace):
    model = ScriptedModel(wants(call("c1"), text="let me look"), says("done"))

    result = run_loop([], root=workspace, model_fn=model)

    assert result.messages[0]["content"] == "let me look"


# --------------------------------------------------------------------------
# Phase 2: stopping rules.
# --------------------------------------------------------------------------


def test_step_limit_makes_exactly_max_steps_model_calls(workspace):
    model = ScriptedModel(*[wants(call(f"c{i}")) for i in range(3)])

    result = run_loop([], root=workspace, budget=Budget(max_steps=3), model_fn=model)

    assert model.calls == 3
    assert result.stop_reason == STOP_STEP_LIMIT
    assert result.text is None


def test_tool_budget_finishes_the_batch_with_refusals_then_stops(workspace):
    """D33. One call runs, two are refused, three ids are answered."""
    model = ScriptedModel(wants(call("c1"), call("c2"), call("c3")))

    result = run_loop(
        [], root=workspace, budget=Budget(max_tool_calls=1), model_fn=model
    )
    answers = tool_messages(result)

    assert [m["tool_call_id"] for m in answers] == ["c1", "c2", "c3"]
    assert "a.txt" in answers[0]["content"]
    for refused in answers[1:]:
        assert refused["content"].startswith(f"ERROR {REFUSED_TOOL_ERROR}:")
        assert "tool-call budget (1)" in refused["content"]
    assert result.stop_reason == STOP_TOOL_LIMIT
    assert result.tool_calls == 1
    assert model.calls == 1  # no second call once the batch is answered


def test_token_budget_stops_before_making_the_call(workspace):
    """The only assertion that proves it: the model was never asked."""
    model = ScriptedModel(says("you should never see this"))
    budget = Budget(max_total_tokens=100, token_reserve=150)

    result = run_loop([], root=workspace, budget=budget, model_fn=model)

    assert model.calls == 0
    assert result.stop_reason == STOP_TOKEN_LIMIT
    assert result.total_tokens == 0


def test_a_call_that_fits_its_reserve_exactly_is_allowed(workspace):
    """The boundary is "exceeds", not "reaches". 0 + 100 against a 100 budget
    is a call that fits - stopping here would cost a step for nothing."""
    model = ScriptedModel(says("allowed"))
    budget = Budget(max_total_tokens=100, token_reserve=100)

    result = run_loop([], root=workspace, budget=budget, model_fn=model)

    assert model.calls == 1
    assert result.stop_reason == STOP_ANSWER


def test_token_budget_stops_between_steps_once_usage_is_known(workspace):
    model = ScriptedModel(
        wants(call("c1"), prompt=60, completion=10),
        says("never reached"),
    )
    budget = Budget(max_total_tokens=100, token_reserve=50)

    result = run_loop([], root=workspace, budget=budget, model_fn=model)

    assert model.calls == 1
    assert result.total_tokens == 70
    assert result.stop_reason == STOP_TOKEN_LIMIT


def test_spend_budget_stops_before_making_the_call(workspace):
    prices = Pricing(
        input_cache_hit_per_m=0.07, input_cache_miss_per_m=0.27, output_per_m=1.10
    )
    budget = Budget(max_spend=0.0001, token_reserve=1000, pricing=prices)
    model = ScriptedModel(says("never reached"))

    result = run_loop([], root=workspace, budget=budget, model_fn=model)

    assert model.calls == 0
    assert result.cost_usd == 0.0


def test_a_zero_second_budget_never_makes_a_call(workspace):
    model = ScriptedModel(says("never reached"))

    result = run_loop(
        [], root=workspace, budget=Budget(max_seconds=0.0), model_fn=model
    )

    assert model.calls == 0
    assert result.stop_reason == STOP_TIME_LIMIT


def test_time_limit_refuses_the_tools_and_stops_after_the_batch(workspace):
    """The deadline passes DURING the model call, so the batch it asked for is
    answered - as refusals - rather than left hanging."""

    class SlowModel(ScriptedModel):
        def __call__(self, messages, *, tools=None):
            time.sleep(0.06)
            return super().__call__(messages, tools=tools)

    model = SlowModel(wants(call("c1"), call("c2")))

    result = run_loop(
        [], root=workspace, budget=Budget(max_seconds=0.05), model_fn=model
    )
    answers = tool_messages(result)

    assert model.calls == 1
    assert [m["tool_call_id"] for m in answers] == ["c1", "c2"]
    assert all("time limit" in m["content"] for m in answers)
    assert result.stop_reason == STOP_TIME_LIMIT
    assert result.tool_calls == 0


def test_cost_is_none_when_unpriced_not_zero(workspace):
    model = ScriptedModel(says("hi", prompt=1000, completion=1000))

    result = run_loop([], root=workspace, model_fn=model)

    assert result.cost_usd is None
    assert result.total_tokens == 2000


def test_budget_and_max_steps_together_is_a_caller_bug(workspace):
    with pytest.raises(ValueError):
        run_loop([], root=workspace, budget=Budget(), max_steps=3)


# --------------------------------------------------------------------------
# Step 9: a budget you cannot reset with kill -9.
# --------------------------------------------------------------------------


def test_a_resumed_run_inherits_the_tokens_the_dead_process_spent(workspace):
    resume = ResumeState(
        messages=[{"role": "user", "content": "task"}],
        steps_done=1,
        prompt_tokens=900,
        completion_tokens=0,
        cache_miss_tokens=900,
    )
    budget = Budget(max_total_tokens=1000, token_reserve=200)
    model = ScriptedModel(says("never reached"))

    result = run_loop(
        [], root=workspace, budget=budget, model_fn=model, resume=resume
    )

    assert model.calls == 0
    assert result.stop_reason == STOP_TOKEN_LIMIT
    assert result.total_tokens == 900


def test_a_resumed_run_inherits_the_tool_calls_too(workspace):
    resume = ResumeState(
        messages=[{"role": "user", "content": "task"}],
        steps_done=1,
        tool_calls_done=2,
    )
    model = ScriptedModel(says("never reached"))

    result = run_loop(
        [],
        root=workspace,
        budget=Budget(max_tool_calls=2),
        model_fn=model,
        resume=resume,
    )

    assert model.calls == 0
    assert result.stop_reason == STOP_TOOL_LIMIT
