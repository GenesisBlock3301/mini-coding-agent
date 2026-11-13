"""The interactive session: many turns, one budget, one journal.

No key and no network. `main` takes a `model_fn` and a `read_line`, so a whole
conversation - including the ways one goes wrong - is a list of strings and a
scripted model.
"""

from __future__ import annotations

import json

import pytest

from agent.budget import Budget, Pricing
from agent.chat import Session, answer_unfinished_calls, main
from agent.journal import Journal
from agent.model import ModelError, ModelResponse
from agent.policy import DEFAULT_WORKSPACE

PRICES = Pricing(
    input_cache_hit_per_m=0.07, input_cache_miss_per_m=0.27, output_per_m=1.10
)


def says(text: str, prompt: int = 100, completion: int = 10) -> ModelResponse:
    return ModelResponse(
        text=text,
        model="scripted",
        finish_reason="stop",
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        duration_ms=0.0,
        tool_calls=[],
        prompt_cache_hit_tokens=0,
        prompt_cache_miss_tokens=prompt,
    )


def wants(call_id: str, path: str = "src", prompt: int = 100) -> ModelResponse:
    return ModelResponse(
        text=None,
        model="scripted",
        finish_reason="tool_calls",
        prompt_tokens=prompt,
        completion_tokens=10,
        total_tokens=prompt + 10,
        duration_ms=0.0,
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "list_files",
                    "arguments": json.dumps({"path": path}),
                },
            }
        ],
        prompt_cache_hit_tokens=0,
        prompt_cache_miss_tokens=prompt,
    )


class ScriptedModel:
    """Plays a fixed script and records every history it was handed."""

    def __init__(self, *responses: ModelResponse) -> None:
        self.script = list(responses)
        self.seen: list[list[dict]] = []

    def __call__(self, messages, *, tools=None):
        self.seen.append([dict(m) for m in messages])
        if not self.script:
            raise AssertionError("the loop wanted another call; script exhausted")
        return self.script.pop(0)


def lines(*inputs: str):
    """A read_line that plays `inputs` then reports end of input."""
    queue = list(inputs)

    def read_line(_prompt: str) -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    return read_line


@pytest.fixture(autouse=True)
def runs_in_tmp(tmp_path, monkeypatch):
    """Journals land in tmp, never in the project's runs/."""
    monkeypatch.setattr("agent.chat.RUNS_DIR", tmp_path / "runs")
    monkeypatch.delenv("MAX_SESSION_TOKENS", raising=False)
    monkeypatch.delenv("MAX_SESSION_SPEND", raising=False)
    monkeypatch.delenv("MAX_CONTEXT_TOKENS", raising=False)


# ---- the feature itself -------------------------------------------------


def test_history_carries_across_turns(capsys):
    """Phase 0's criterion: turn 3 must see turns 1 and 2."""
    model = ScriptedModel(says("one"), says("two"), says("three"))

    assert main([], model_fn=model, read_line=lines("a", "b", "c")) == 0

    first, second, third = model.seen
    assert [m["role"] for m in first] == ["system", "user"]
    assert len(second) == 4   # system, user, assistant, user
    assert len(third) == 6
    assert [m["content"] for m in third if m["role"] == "user"] == ["a", "b", "c"]
    assert capsys.readouterr().out.split() == ["one", "two", "three"]


def test_one_journal_holds_every_turn(tmp_path):
    model = ScriptedModel(says("one"), says("two"))
    main([], model_fn=model, read_line=lines("a", "b"))

    files = list((tmp_path / "runs").glob("*.jsonl"))
    assert len(files) == 1, "D56: one journal per session, not per turn"
    kinds = [json.loads(line)["t"] for line in files[0].read_text().splitlines()]
    assert kinds.count("run_started") == 2
    assert kinds.count("run_finished") == 2


# ---- session budgets (D54/D55) -----------------------------------------


def test_session_token_budget_refuses_a_later_turn(monkeypatch, capsys):
    # TOKEN_RESERVE defaults to 2000, and check_before_step compares
    # total + reserve against the cap - so a session cap below the reserve
    # refuses every turn before a single call. Realistic here, in the way.
    monkeypatch.setenv("TOKEN_RESERVE", "1")
    monkeypatch.setenv("MAX_SESSION_TOKENS", "400")
    model = ScriptedModel(says("one", prompt=200), says("two", prompt=200))

    main([], model_fn=model, read_line=lines("a", "b", "c"))

    err = capsys.readouterr().err
    assert "session token budget spent" in err
    assert model.script == []       # the third turn never reached the model


def test_session_spend_budget_refuses_a_later_turn(monkeypatch, capsys):
    monkeypatch.setenv("TOKEN_RESERVE", "1")
    monkeypatch.setenv("MAX_SESSION_SPEND", "0.0001")
    monkeypatch.setenv("PRICE_INPUT_CACHE_HIT_PER_M", "0.07")
    monkeypatch.setenv("PRICE_INPUT_CACHE_MISS_PER_M", "0.27")
    monkeypatch.setenv("PRICE_OUTPUT_PER_M", "1.10")
    model = ScriptedModel(says("one", prompt=1000, completion=500))

    main([], model_fn=model, read_line=lines("a", "b"))

    assert "session spend limit reached" in capsys.readouterr().err


def test_a_spend_limit_without_prices_is_refused(monkeypatch, capsys):
    monkeypatch.setenv("MAX_SESSION_SPEND", "1.00")
    monkeypatch.delenv("PRICE_INPUT_CACHE_HIT_PER_M", raising=False)
    monkeypatch.delenv("PRICE_INPUT_CACHE_MISS_PER_M", raising=False)
    monkeypatch.delenv("PRICE_OUTPUT_PER_M", raising=False)

    assert main([], model_fn=ScriptedModel(), read_line=lines("a")) == 2
    assert "needs prices" in capsys.readouterr().err


def test_turn_budget_is_tightened_by_what_the_session_has_left(tmp_path):
    """D55 in one assertion: no ledger is threaded anywhere."""
    session = Session(
        root=DEFAULT_WORKSPACE,
        turn_limits=Budget(max_total_tokens=None, pricing=PRICES),
        journal=Journal(tmp_path / "s.jsonl"),
        session_id="s",
        max_session_tokens=1000,
    )
    assert session.turn_budget().max_total_tokens == 1000

    session.prompt_tokens = 600
    assert session.turn_budget().max_total_tokens == 400


def test_a_tighter_per_turn_limit_still_wins(tmp_path):
    session = Session(
        root=DEFAULT_WORKSPACE,
        turn_limits=Budget(max_total_tokens=50),
        journal=Journal(tmp_path / "s.jsonl"),
        session_id="s",
        max_session_tokens=1000,
    )
    assert session.turn_budget().max_total_tokens == 50


# ---- context ceiling (D59) ---------------------------------------------


def test_context_ceiling_refuses_a_new_turn(monkeypatch, capsys):
    monkeypatch.setenv("MAX_CONTEXT_TOKENS", "150")
    model = ScriptedModel(says("one", prompt=200))

    main([], model_fn=model, read_line=lines("a", "b"))

    err = capsys.readouterr().err
    assert "too long to continue" in err
    assert ":reset" in err


def test_reset_clears_the_conversation_but_not_the_bill(monkeypatch, capsys):
    monkeypatch.setenv("MAX_CONTEXT_TOKENS", "150")
    model = ScriptedModel(says("one", prompt=200), says("two", prompt=100))

    main([], model_fn=model, read_line=lines("a", ":reset", "b"))

    # After :reset the third turn runs again...
    assert model.script == []
    # ...with a fresh conversation...
    assert [m["role"] for m in model.seen[-1]] == ["system", "user"]
    # ...but the tokens from before are still counted.
    assert "tokens=320" in capsys.readouterr().err


# ---- commands (D61) -----------------------------------------------------


def test_commands_never_reach_the_model(capsys):
    model = ScriptedModel(says("one"))

    main([], model_fn=model, read_line=lines(":help", ":cost", ":history", "a"))

    assert len(model.seen) == 1
    assert [m["content"] for m in model.seen[0] if m["role"] == "user"] == ["a"]


def test_quit_ends_the_session_before_later_input():
    model = ScriptedModel(says("one"))
    assert main([], model_fn=model, read_line=lines(":quit", "a")) == 0
    assert model.seen == []


def test_an_unknown_command_is_not_sent_as_a_question(capsys):
    model = ScriptedModel()
    main([], model_fn=model, read_line=lines(":nope"))
    assert model.seen == []
    assert "unknown command" in capsys.readouterr().err


# ---- failure paths ------------------------------------------------------


def test_a_model_error_leaves_the_conversation_unchanged(capsys):
    class Failing(ScriptedModel):
        def __call__(self, messages, *, tools=None):
            self.seen.append([dict(m) for m in messages])
            if len(self.seen) == 1:
                raise ModelError(
                    "timeout",
                    "the request timed out",
                    retryable=True,
                    safe_to_retry=True,
                )
            return says("recovered")

    model = Failing()
    main([], model_fn=model, read_line=lines("a", "b"))

    # The failed turn left nothing behind: turn 2 is still system + one user.
    assert [m["role"] for m in model.seen[1]] == ["system", "user"]
    assert model.seen[1][-1]["content"] == "b"
    assert "turn discarded" in capsys.readouterr().err


def test_ctrl_c_during_a_turn_keeps_the_session(capsys):
    class Interrupting(ScriptedModel):
        def __call__(self, messages, *, tools=None):
            self.seen.append([dict(m) for m in messages])
            if len(self.seen) == 1:
                raise KeyboardInterrupt
            return says("still here")

    model = Interrupting()
    assert main([], model_fn=model, read_line=lines("a", "b")) == 0

    assert [m["role"] for m in model.seen[1]] == ["system", "user"]
    assert "session intact" in capsys.readouterr().err


def test_a_limit_stop_keeps_the_partial_turn_and_prints_no_answer(
    monkeypatch, capsys
):
    """D58. The tool result was paid for, so it stays in the history."""
    monkeypatch.setenv("MAX_STEPS", "2")
    model = ScriptedModel(wants("call_a"), wants("call_b"), says("done"))

    main([], model_fn=model, read_line=lines("a", "b"))

    captured = capsys.readouterr()
    assert "incomplete [step_limit]" in captured.err
    # Turn 2 sees turn 1's assistant turns and tool results.
    roles = [m["role"] for m in model.seen[-1]]
    assert "tool" in roles
    assert roles[-1] == "user"


# ---- resume (D62) -------------------------------------------------------


def test_resume_restores_the_conversation_and_the_totals(tmp_path, capsys):
    model = ScriptedModel(says("one", prompt=100), says("two", prompt=200))
    main([], model_fn=model, read_line=lines("a", "b"))
    session_id = list((tmp_path / "runs").glob("*.jsonl"))[0].stem
    capsys.readouterr()

    resumed = ScriptedModel(says("three"))
    assert main(
        ["chat", f"--resume={session_id}"],
        model_fn=resumed,
        read_line=lines("c"),
    ) == 0

    history = resumed.seen[0]
    assert [m["content"] for m in history if m["role"] == "user"] == ["a", "b", "c"]
    # Nothing was replayed through the model, so nothing was re-billed.
    assert "restored 5 messages" in capsys.readouterr().err


def test_resume_of_a_missing_session_is_a_usage_error(capsys):
    assert main(["chat", "--resume=nope"], model_fn=ScriptedModel()) == 2
    assert "no_journal" in capsys.readouterr().err


def test_resume_closes_tool_calls_the_crash_left_open():
    """A restored history must never end with an unanswered tool call: the
    provider rejects the next request with a 400 about something else."""
    messages = [
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "a", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "b", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
    ]

    assert answer_unfinished_calls(messages) == 1

    assert messages[-1]["tool_call_id"] == "b"
    assert "Not run" in messages[-1]["content"]


def test_a_complete_history_has_no_orphans():
    messages = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "a", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
        {"role": "assistant", "content": "done", "tool_calls": None},
    ]
    assert answer_unfinished_calls(messages) == 0
