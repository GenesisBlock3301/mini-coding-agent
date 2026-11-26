"""The interactive session: many turns, one budget, one journal.

No key and no network. `main` takes a `model_fn` and a `read_line`, so a whole
conversation - including the ways one goes wrong - is a list of strings and a
scripted model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.budget import Budget, Pricing
from agent.chat import Session, answer_unfinished_calls, main
from agent.journal import Journal
from agent.model import ModelError, ModelResponse

DEFAULT_WORKSPACE = Path.cwd()  # a dummy root for hand-built Sessions

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
    """Journals land in tmp, and the developer's .env cannot reach these tests.

    `main` calls load_dotenv(), so without this every test here inherits
    whatever is in the real .env. That is not hypothetical: adding
    PROVIDER=ollama to .env broke two spend-limit tests, because a local
    provider is priced with Pricing.free() and a spend limit that can never
    be reached never fires. The code was right and the tests were coupled to
    one machine.

    Note load_dotenv() does NOT override variables already set, so pinning a
    value here beats deleting it - a delete just lets the file win.
    """
    monkeypatch.setattr("agent.chat.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setenv("PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_APIKEY", "sk-test-not-used")
    for name in (
        "MAX_SESSION_TOKENS", "MAX_SESSION_SPEND", "MAX_CONTEXT_TOKENS",
        "MAX_STEPS", "MAX_TOOL_CALLS", "MAX_RUN_SECONDS", "TOKEN_RESERVE",
        "PRICE_INPUT_CACHE_HIT_PER_M", "PRICE_INPUT_CACHE_MISS_PER_M",
        "PRICE_OUTPUT_PER_M",
    ):
        monkeypatch.delenv(name, raising=False)


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


def test_chat_can_create_a_new_file(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("WORKSPACE_DIR", str(workspace))
    create = ModelResponse(
        text=None, model="scripted", finish_reason="tool_calls",
        prompt_tokens=100, completion_tokens=10, total_tokens=110,
        duration_ms=0.0,
        tool_calls=[{
            "id": "create_1", "type": "function",
            "function": {"name": "write_file", "arguments": json.dumps({"path": "new.txt", "content": "created in chat\n"})},
        }],
        prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=100,
    )
    assert main([], model_fn=ScriptedModel(create, says("done")), read_line=lines("create new.txt")) == 0
    assert (workspace / "new.txt").read_text() == "created in chat\n"


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
    """An error the policy will not retry still discards its turn.

    This test used to raise a `timeout`, which is retryable - and it passed
    only because the REPL had no retry policy at all. Giving chat.py Session
    8's policy made the first turn SUCCEED on its second attempt, which is
    the point of the change and not a regression. The property being tested
    here is unrelated to retries, so the error is now one that no policy will
    resend: an auth failure is a fact about the key, and sending it again is
    latency with no chance of a different answer.
    """
    class Failing(ScriptedModel):
        def __call__(self, messages, *, tools=None):
            self.seen.append([dict(m) for m in messages])
            if len(self.seen) == 1:
                raise ModelError(
                    "auth",
                    "no API key",
                    retryable=False,
                    safe_to_retry=True,
                )
            return says("recovered")

    model = Failing()
    main([], model_fn=model, read_line=lines("a", "b"))

    # The failed turn left nothing behind: turn 2 is still system + one user.
    assert [m["role"] for m in model.seen[1]] == ["system", "user"]
    assert model.seen[1][-1]["content"] == "b"
    assert "turn discarded" in capsys.readouterr().err


def test_a_retryable_failure_no_longer_costs_the_user_their_turn(monkeypatch, capsys):
    """Session 8's policy, now reaching the REPL.

    Before this, chat.py called run_loop without `retry=` and ran at
    RetryPolicy.off() - so one 500 between two words ended the turn and the
    user retyped the question. The one-shot CLI had retried the same failure
    since Session 8. The difference was an oversight, not a decision.
    """
    # Zero delay: a suite that really sleeps through a backoff is a suite you
    # stop running (Session 8, step 1). `run_loop` takes an injectable clock
    # for this; main() does not thread one through, so the policy itself is
    # configured to wait for nothing.
    monkeypatch.setenv("RETRY_BASE_DELAY_S", "0")
    monkeypatch.setenv("MAX_ATTEMPTS", "3")

    class Flaky(ScriptedModel):
        def __call__(self, messages, *, tools=None):
            self.seen.append([dict(m) for m in messages])
            if len(self.seen) == 1:
                raise ModelError(
                    "server", "502 bad gateway", retryable=True, safe_to_retry=True
                )
            return says("answered on the second attempt")

    model = Flaky()
    main([], model_fn=model, read_line=lines("a"))

    assert len(model.seen) == 2, "the request was sent again"
    assert "answered on the second attempt" in capsys.readouterr().out
    assert "turn discarded" not in capsys.readouterr().err


def test_the_banner_names_the_retry_policy(capsys, monkeypatch):
    """A retry policy nobody can see is latency with no explanation. The
    compact banner hides it; the verbose one (MINI_AGENT_VERBOSE / /verbose)
    must still show it."""
    monkeypatch.setattr("agent.chat._VERBOSE", True)
    main([], model_fn=ScriptedModel(), read_line=lines())

    assert "retry:" in capsys.readouterr().err


def test_a_session_built_by_hand_still_has_no_retries():
    """The default stays `off`, so every test written before this one keeps
    measuring what it measured then."""
    from agent.chat import Session

    session = Session(
        root=DEFAULT_WORKSPACE,
        turn_limits=Budget(),
        journal=None,
        session_id="x",
    )

    assert session.retry.max_attempts == 1


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


# ---- commands, the prefix, and the menu ---------------------------------


def _menu(text: str) -> list[str]:
    """What the dropdown would offer for a half-typed line."""
    from prompt_toolkit.document import Document

    from agent.chat import _command_completer

    document = Document(text, len(text))
    return [c.text for c in _command_completer().get_completions(document, None)]


def test_slash_and_colon_name_the_same_command(capsys):
    model = ScriptedModel()

    main([], model_fn=model, read_line=lines("/cost", ":cost"))

    assert model.seen == []
    # Counted as whole lines: the end-of-session line carries the same totals
    # with a `[session <id>]` prefix, and would otherwise be a third match.
    bare = "turns=0 tokens=0 (in=0 hit=0 out=0) tools=0 spend=unpriced"
    assert capsys.readouterr().err.splitlines().count(bare) == 2


def test_status_reports_the_session_and_the_journal_trace_reads(capsys):
    model = ScriptedModel(says("one"))

    main([], model_fn=model, read_line=lines("a", "/status"))

    err = capsys.readouterr().err
    assert "session    " in err            # not the banner's "session <id>"
    assert "deepseek" in err
    assert "1 turn(s) recorded" in err
    assert "next turn  ok" in err


def test_status_names_why_the_next_turn_would_be_refused(capsys, monkeypatch):
    """The gate and the report are the same call, so they cannot disagree."""
    monkeypatch.setenv("TOKEN_RESERVE", "1")
    monkeypatch.setenv("MAX_SESSION_TOKENS", "150")
    model = ScriptedModel(says("one", prompt=200))

    main([], model_fn=model, read_line=lines("a", "/status"))

    assert "next turn  session token budget spent" in capsys.readouterr().err


def test_trace_before_any_turn_says_nothing_is_recorded(capsys):
    main([], model_fn=ScriptedModel(), read_line=lines("/trace"))

    assert "nothing recorded yet" in capsys.readouterr().err


def test_trace_shows_the_last_turn_only_by_default(capsys):
    model = ScriptedModel(says("one"), says("two"))

    main([], model_fn=model, read_line=lines("alpha", "beta", "/trace"))

    err = capsys.readouterr().err
    assert "turn 2-2 of 2" in err
    assert "START      beta" in err
    assert "START      alpha" not in err


def test_trace_all_shows_every_turn(capsys):
    model = ScriptedModel(says("one"), says("two"))

    main([], model_fn=model, read_line=lines("alpha", "beta", "/trace all"))

    err = capsys.readouterr().err
    assert "turn 1-2 of 2" in err
    assert "START      alpha" in err and "START      beta" in err


def test_trace_takes_a_turn_count(capsys):
    model = ScriptedModel(says("one"), says("two"))

    main([], model_fn=model, read_line=lines("alpha", "beta", "/trace 1"))

    err = capsys.readouterr().err
    assert "START      beta" in err and "START      alpha" not in err


def test_trace_rejects_an_argument_it_does_not_understand(capsys):
    model = ScriptedModel(says("one"))

    main([], model_fn=model, read_line=lines("a", "/trace sideways"))

    err = capsys.readouterr().err
    assert "usage: /trace" in err
    assert "START" not in err


def test_trace_full_announces_that_it_is_verbatim(capsys):
    """D92: the header, not a flag the reader has to remember, says which
    form is on screen - because the bounded one is the safe one to paste."""
    model = ScriptedModel(says("one"), says("two"))

    main([], model_fn=model, read_line=lines("a", "/trace", "/trace full"))

    err = capsys.readouterr().err
    assert "tool output bounded" in err
    assert "VERBATIM - do not paste this anywhere" in err


def test_a_path_is_a_question_not_an_unknown_command(capsys):
    """D104: `/Users/...` must reach the model. Reporting it as an unknown
    command reads as a refusal to answer a perfectly ordinary question."""
    model = ScriptedModel(says("one"))

    main([], model_fn=model, read_line=lines("/Users/sifat/notes.md - what is this?"))

    assert [m["content"] for m in model.seen[0] if m["role"] == "user"] == [
        "/Users/sifat/notes.md - what is this?"
    ]
    assert "unknown command" not in capsys.readouterr().err


def test_an_unknown_slash_command_is_not_sent_as_a_question(capsys):
    model = ScriptedModel()

    main([], model_fn=model, read_line=lines("/nope"))

    assert model.seen == []
    assert "unknown command" in capsys.readouterr().err


def test_an_argument_to_a_command_that_takes_none_is_reported(capsys):
    model = ScriptedModel()

    main([], model_fn=model, read_line=lines("/cost everything"))

    assert model.seen == []
    assert "takes no arguments" in capsys.readouterr().err


def test_help_lists_every_command_that_exists(capsys):
    """One table, three consumers (D101): help cannot drift from dispatch."""
    from agent.chat import COMMANDS, help_text

    text = help_text()
    for command in COMMANDS:
        assert f"/{command.name}" in text


def test_the_menu_offers_every_command_and_narrows_as_you_type():
    from agent.chat import COMMANDS

    assert _menu("/") == [f"/{c.name}" for c in COMMANDS]
    assert _menu("/st") == ["/status"]
    assert _menu(":c") == [":cost", ":cancel-memory"]
    assert _menu("/STA") == ["/status"]


def test_the_menu_never_opens_over_a_question_or_an_argument():
    assert _menu("what files are here") == []
    assert _menu("/trace all") == []
    assert _menu("") == []


def test_the_menu_is_not_drawn_into_a_redirected_transcript(monkeypatch):
    """D105: prompt_toolkit renders by moving the cursor around stdout, and
    `python -m agent.chat > transcript` must stay a clean transcript."""
    from agent.chat import make_read_line

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    assert make_read_line() is input


def _typed(*keys: str) -> str:
    """Drive the real prompt widget through a pipe and return the line it
    accepted.

    No wall-clock sleeps: prompt_toolkit computes completions in an async
    task, so the test yields to the event loop between keystrokes instead of
    waiting a guessed number of milliseconds. A sleep here would be a test
    that fails on a loaded machine and proves nothing on a fast one.
    """
    import asyncio

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from agent.chat import prompt_widget

    async def settle() -> None:
        for _ in range(30):
            await asyncio.sleep(0)

    async def run() -> str:
        with create_pipe_input() as pipe:
            widget = prompt_widget(input=pipe, output=DummyOutput())
            accepted = asyncio.ensure_future(widget.prompt_async("> "))
            for chunk in keys:
                await settle()
                pipe.send_text(chunk)
            await settle()
            pipe.send_text("\n")
            return await accepted

    return asyncio.run(run())


DOWN = "\x1b[B"


def test_the_menu_completes_a_half_typed_command():
    assert _typed("/st", "\t") == "/status"
    assert _typed("/tr", DOWN) == "/trace"


def test_a_bare_slash_opens_the_menu_at_the_first_command():
    """What `/` is for: the list, without having to remember a name."""
    assert _typed("/", "\t") == "/status"
    assert _typed("/", "\t", "\t") == "/trace"
    assert _typed("/", DOWN) == "/status"


def test_typing_a_question_is_never_completed_into_a_command():
    assert _typed("what files are here") == "what files are here"
    assert _typed("/Users/sifat/notes.md") == "/Users/sifat/notes.md"


def test_a_bare_prefix_prints_the_list_instead_of_asking_the_model(capsys):
    """D106: `/` alone is "what can I type here".

    In a terminal the menu answers that before Enter is pressed - but the menu
    is the one layer that can be absent (D105), and this is exactly the
    session where you cannot see the commands. Sending `/` to the model would
    bill a request whose answer is nothing.
    """
    model = ScriptedModel()

    main([], model_fn=model, read_line=lines("/", ":"))

    assert model.seen == []
    err = capsys.readouterr().err
    assert err.count("current session metrics") == 2   # the banner no longer lists commands


def test_an_unknown_command_prints_the_list_not_a_pointer_to_it(capsys):
    model = ScriptedModel()

    main([], model_fn=model, read_line=lines("/nope"))

    err = capsys.readouterr().err
    assert "unknown command /nope" in err
    assert "/trace" in err.split("unknown command /nope")[1]


def test_resume_counts_questions_asked_not_model_requests(tmp_path, capsys):
    """D107. The bug only shows when a turn takes more than one step, which
    is why every earlier resume test missed it: a text-only answer makes
    steps and turns the same number."""
    model = ScriptedModel(wants("c1"), says("one"), says("two"))
    main([], model_fn=model, read_line=lines("a", "b"))
    session_id = list((tmp_path / "runs").glob("*.jsonl"))[0].stem
    capsys.readouterr()

    main(
        ["chat", f"--resume={session_id}"],
        model_fn=ScriptedModel(says("three")),
        read_line=lines("/status"),
    )

    err = capsys.readouterr().err
    assert "turns      2  tools=1" in err      # two questions, three requests
    assert "2 turn(s) recorded" in err         # and the journal agrees
