"""Tests for agent.repetition - the same thing, again, and when that stops a run.

Session 9, Phases 0 through 2. Nothing here needs an API key or a network:
the model is scripted, so "the agent gets stuck" is a fact about the script
rather than something to wait for.

The case this whole file is organised around is NOT the stuck agent. It is
the Session 8 live run, which read `stats.py`, was told its edit had already
landed, and read `stats.py` again before reporting - the most correct thing
an agent did in this project, and the thing a naive "stop when a call
repeats" rule would have killed. Every threshold here is set the way it is to
keep that run alive.
"""

import json

import pytest

from agent.budget import Budget
from agent.loop import run_loop
from agent.model import ModelResponse
from agent.repetition import (
    Tally,
    canonical_arguments,
    is_repeat,
    signature,
)

TARGET = "stats.py"
BEFORE = "def tail(values, n):\n    return values[-n + 1:]\n"
AFTER = "def tail(values, n):\n    return values[-n:]\n"


# --------------------------------------------------------------------------
# Scaffolding
# --------------------------------------------------------------------------


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


def raw_call(call_id, name, arguments: str):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
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
def workspace(tmp_path):
    (tmp_path / TARGET).write_text(BEFORE, encoding="utf-8")
    (tmp_path / "other.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def tool_messages(result):
    return [m for m in result.messages if m.get("role") == "tool"]


# --------------------------------------------------------------------------
# Phase 0 - counting, before any rule
# --------------------------------------------------------------------------


def test_five_identical_failures_report_five(workspace):
    """Step 1's acceptance test, on the record itself."""
    tally = Tally(root=workspace)
    for _ in range(5):
        tally.record("read_file", '{"path": "nope.py"}', ok=False)

    assert tally.worst == 5
    assert tally.repeated_failures == 4  # occurrences beyond the first
    assert tally.repeated_calls == 0


def test_the_session_8_transcript_reports_two_for_read_file(workspace):
    """read -> edit -> read. The run that must survive this session.

    Two identical reads, and the number the rule sees is 2 - under the
    success threshold of 5, which is not a coincidence but the reason that
    threshold is 5.
    """
    tally = Tally(root=workspace)
    tally.record("read_file", '{"path": "stats.py"}', ok=True)
    tally.record("replace_text", '{"path": "stats.py"}', ok=True)
    last = tally.record("read_file", '{"path": "stats.py"}', ok=True)

    assert last.count == 2
    assert not last.stops
    assert tally.worst == 2


def test_a_path_and_its_dot_slash_spelling_are_one_call(workspace):
    """D77. The spelling test byte-identity fails, and the reason for the
    canonical form: the model rewrites the path it was just told about."""
    plain = signature("read_file", '{"path": "stats.py"}', root=workspace)
    dotted = signature("read_file", '{"path": "./stats.py"}', root=workspace)
    nested = signature("read_file", '{"path": "sub/../stats.py"}', root=workspace)

    assert plain == dotted == nested


def test_key_order_does_not_make_a_new_call(workspace):
    one = signature("replace_text", '{"path": "a.py", "old": "x", "new": "y"}', root=workspace)
    two = signature("replace_text", '{"new": "y", "old": "x", "path": "a.py"}', root=workspace)

    assert one == two


def test_unparseable_arguments_still_compare_equal(workspace):
    """The most common stuck loop there is: invalid_arguments, three times.

    Dropping it because it cannot be parsed would drop the case the session
    exists for.
    """
    one = signature("read_file", "{not json", root=workspace)
    two = signature("read_file", "{not json", root=workspace)

    assert one == two
    assert canonical_arguments("{not json", root=workspace) == "{not json"


def test_a_rejected_path_keeps_its_raw_spelling(workspace):
    """An absolute path never reaches a tool, so there is no canonical form
    to resolve it to. It still compares equal to itself, which is all the
    rule needs - and `/etc/passwd` stays distinct from `../etc/passwd`."""
    absolute = signature("read_file", '{"path": "/etc/passwd"}', root=workspace)
    escape = signature("read_file", '{"path": "../../etc/passwd"}', root=workspace)

    assert absolute == signature("read_file", '{"path": "/etc/passwd"}', root=workspace)
    assert absolute != escape


def test_a_different_path_is_a_different_call(workspace):
    assert signature("read_file", '{"path": "a.py"}', root=workspace) != signature(
        "read_file", '{"path": "b.py"}', root=workspace
    )


def test_a_different_tool_on_the_same_path_is_a_different_call(workspace):
    assert signature("read_file", '{"path": "a.py"}', root=workspace) != signature(
        "list_files", '{"path": "a.py"}', root=workspace
    )


# --------------------------------------------------------------------------
# Phase 1 - the rule, with no loop at all
# --------------------------------------------------------------------------


def test_is_repeat_is_the_predicate_the_rule_is_stated_in(workspace):
    seen = [signature("read_file", '{"path": "a.py"}', root=workspace)]

    assert is_repeat(seen, signature("read_file", '{"path": "./a.py"}', root=workspace))
    assert not is_repeat(seen, signature("read_file", '{"path": "b.py"}', root=workspace))


def test_the_two_cycle_is_caught_without_building_a_cycle_detector(workspace):
    """A, B, A, B, A, B.

    Byte-identity sees two calls, each three times - and cumulative counting
    stops on A's third occurrence regardless of how B is spaced between them.
    A consecutive rule sees six calls and nothing to say about any of them.
    """
    tally = Tally(root=workspace)
    verdicts = []
    for _ in range(3):
        verdicts.append(tally.record("read_file", '{"path": "a.py"}', ok=False))
        verdicts.append(tally.record("read_file", '{"path": "b.py"}', ok=False))

    assert [v.count for v in verdicts] == [1, 1, 2, 2, 3, 3]
    assert verdicts[4].stops, "A's third occurrence is A's third occurrence"


def test_a_repeat_after_an_intervening_success_still_counts(workspace):
    """D80, stated as the case that decided it.

    Twice, recover, twice again. Under a consecutive rule this run never
    stops - and 'reads the same file between every edit' is precisely the
    shape of agent that goes nowhere expensively.
    """
    tally = Tally(root=workspace)
    tally.record("read_file", '{"path": "a.py"}', ok=False)
    tally.record("read_file", '{"path": "a.py"}', ok=False)
    tally.record("replace_text", '{"path": "b.py"}', ok=True)
    third = tally.record("read_file", '{"path": "a.py"}', ok=False)

    assert third.count == 3
    assert third.stops


def test_successes_get_a_longer_rope_than_failures(workspace):
    """D78, as two numbers rather than one.

    Four identical successful reads are verification and survive. The fifth
    is a run going nowhere politely, and stops.
    """
    tally = Tally(root=workspace)
    counts = [
        tally.record("read_file", '{"path": "a.py"}', ok=True) for _ in range(5)
    ]

    assert [v.stops for v in counts] == [False, False, False, False, True]


def test_failures_and_successes_are_counted_separately(workspace):
    """The same call that failed twice and then succeeded has not 'repeated
    three times' in any sense a rule should act on: it recovered."""
    tally = Tally(root=workspace)
    tally.record("read_file", '{"path": "a.py"}', ok=False)
    tally.record("read_file", '{"path": "a.py"}', ok=False)
    recovered = tally.record("read_file", '{"path": "a.py"}', ok=True)

    assert recovered.count == 1
    assert not recovered.stops


def test_the_rule_can_be_switched_off_and_still_counts(workspace):
    """Session 12's control arm: measured, never acted on."""
    tally = Tally(root=workspace, max_failures=None, max_successes=None)
    for _ in range(9):
        last = tally.record("read_file", '{"path": "a.py"}', ok=False)

    assert not last.stops
    assert tally.worst == 9


def test_the_warning_fires_one_below_the_threshold_and_only_once(workspace):
    tally = Tally(root=workspace)
    first = tally.record("read_file", '{"path": "a.py"}', ok=False)
    second = tally.record("read_file", '{"path": "a.py"}', ok=False)

    assert not first.warns
    assert second.warns
    assert tally.should_warn(second)
    assert not tally.should_warn(second), "D82: told once, not every time"


# --------------------------------------------------------------------------
# Phase 2 - the stop, inside the loop
# --------------------------------------------------------------------------


def test_the_batch_is_answered_in_full_when_repetition_stops_it(workspace):
    """D33, which does not bend for a new stop reason.

    The rule trips on call 3 of 5. Calls 4 and 5 are REFUSED - answered
    without running - because a batch with unanswered tool_call ids is the
    corrupted conversation Session 5 spent a phase proving you must not
    produce.
    """
    batch = [raw_call(f"c{n}", "read_file", "{not json") for n in range(5)]
    result = run_loop(
        [{"role": "user", "content": "go"}],
        root=workspace, model_fn=ScriptedModel(wants(*batch)), max_steps=5,
    )

    assert result.stop_reason == "repetition"
    assert len(tool_messages(result)) == 5, "every id answered"
    assert result.tool_calls == 3, "two were answered without running"
    assert result.stopped_by_limit


def test_the_model_is_warned_once_in_the_conversation_before_the_stop(workspace):
    """D82. A silent stop is not an instruction; this one is in the history."""
    batch = [raw_call(f"c{n}", "read_file", "{not json") for n in range(3)]
    result = run_loop(
        [{"role": "user", "content": "go"}],
        root=workspace, model_fn=ScriptedModel(wants(*batch)), max_steps=5,
    )

    warned = [m for m in tool_messages(result) if "[repetition:" in m["content"]]
    assert len(warned) == 1
    assert "will not be answered" in warned[0]["content"]
    assert result.stop_reason == "repetition", "the warning is not a reprieve"


def test_the_warning_does_not_rewrite_the_tool_result(workspace):
    """The note goes in the MESSAGE, never in the ToolResult.

    ToolResult is frozen so a trace cannot be edited after the fact. The
    loop is entitled to add its own sentence to the conversation; it is not
    entitled to change what the tool said it did.
    """
    seen = []
    batch = [raw_call(f"c{n}", "read_file", "{not json") for n in range(2)]
    run_loop(
        [{"role": "user", "content": "go"}],
        root=workspace, model_fn=ScriptedModel(wants(*batch), says()),
        max_steps=5, on_tool=lambda step, call, result: seen.append(result),
    )

    assert all("[repetition:" not in r.content for r in seen)


def test_the_same_call_twice_in_one_response_counts_twice(workspace):
    """Decided and written down (C's failure table).

    A human reading the transcript calls a batch of identical calls one
    mistake. The tool-call budget calls it N. This counts it N, for the same
    reason the budget does: each one was dispatched, each one cost a slot,
    and a model that can get N free attempts by batching them has found a
    hole in the rule rather than a better way to work.
    """
    pair = [
        call("a", "read_file", path=TARGET),
        call("b", "read_file", path="./" + TARGET),
    ]
    result = run_loop(
        [{"role": "user", "content": "go"}],
        root=workspace, model_fn=ScriptedModel(wants(*pair), says()), max_steps=5,
    )

    assert result.longest_repeat == 2
    assert result.repeated_calls == 1
    assert result.ok, "two identical successes are not a stop"


def test_a_read_edit_read_run_is_not_stopped(workspace):
    """The Session 8 live run, re-enacted offline. The regression test for
    the whole session: if this ever goes red, the rule has started banning
    the behaviour the project spent eight sessions trying to produce."""
    model = ScriptedModel(
        wants(call("r1", "read_file", path=TARGET)),
        wants(call("e1", "replace_text", path=TARGET,
                   old="return values[-n + 1:]", new="return values[-n:]")),
        wants(call("r2", "read_file", path=TARGET)),
        says("fixed the off-by-one in tail()"),
    )

    result = run_loop(
        [{"role": "user", "content": "fix the window"}],
        root=workspace, model_fn=model, max_steps=6,
    )

    assert result.ok
    assert result.changed_paths == [TARGET]
    assert (workspace / TARGET).read_text() == AFTER
    assert result.longest_repeat == 2, "the two reads were seen and allowed"


def test_a_repetition_stop_still_reports_what_it_changed(workspace):
    """D52 applies to every stop, including the new ones."""
    model = ScriptedModel(
        wants(call("e1", "replace_text", path=TARGET,
                   old="return values[-n + 1:]", new="return values[-n:]")),
        wants(*[raw_call(f"c{n}", "read_file", "{not json") for n in range(3)]),
    )

    result = run_loop(
        [{"role": "user", "content": "fix it then flail"}],
        root=workspace, model_fn=model, max_steps=6,
    )

    assert result.stop_reason == "repetition"
    assert result.changed_paths == [TARGET]


def test_the_stop_names_the_call_that_caused_it(workspace):
    batch = [raw_call(f"c{n}", "read_file", '{"path": "ghost.py"}') for n in range(3)]
    result = run_loop(
        [{"role": "user", "content": "go"}],
        root=workspace, model_fn=ScriptedModel(wants(*batch)), max_steps=5,
    )

    assert "read_file" in result.repetition_stopped_on
    assert "ghost.py" in result.repetition_stopped_on


def test_the_counter_survives_a_resume(workspace, tmp_path):
    """A budget you can clear with `kill -9` is not a budget (D39), and a
    repetition counter is no different: a run that was stuck before the
    crash is still stuck after it.
    """
    from agent.journal import Journal, new_run_id, rebuild

    journal = Journal.create(tmp_path / "runs", new_run_id())
    first = ScriptedModel(
        wants(*[raw_call(f"c{n}", "read_file", "{not json") for n in range(2)]),
        says(),
    )
    run_loop(
        [{"role": "user", "content": "go"}],
        root=workspace, model_fn=first, journal=journal, max_steps=5,
    )

    state = rebuild(journal.events())
    assert len(state.calls_done) == 2
    assert all(c["name"] == "read_file" for c in state.calls_done)

    tally = Tally(root=workspace)
    tally.seed(state.calls_done)
    assert tally.record("read_file", "{not json", ok=False).stops


def test_a_crash_does_not_inflate_the_counter(workspace, tmp_path, monkeypatch):
    """One call id is one call, however many processes it took.

    A resumed run that re-runs an interrupted READ writes a second
    `tool_intent` under the same call id. Counting both would mean `kill -9`
    can push a run toward a repetition stop - the mirror image of the bug
    D39 fixed for tokens, where a crash could REFUND a budget.

    Found by scripts/repeat_audit.py against this project's own journals: 12
    phantom calls across 91 runs, every one of them a crash artefact.
    """
    import agent.loop as loop_module
    from agent.journal import Journal, new_run_id, rebuild

    class Boom(Exception):
        pass

    journal = Journal.create(tmp_path / "runs", new_run_id())
    real = loop_module.dispatch
    state = {"crashed": False}

    def crashing(name, arguments, *, root):
        if not state["crashed"]:
            state["crashed"] = True
            raise Boom("killed inside the tool")
        return real(name, arguments, root=root)

    monkeypatch.setattr(loop_module, "dispatch", crashing)

    with pytest.raises(Boom):
        run_loop(
            [{"role": "user", "content": "read it"}],
            root=workspace, journal=journal, max_steps=5,
            model_fn=ScriptedModel(wants(call("r1", "read_file", path=TARGET))),
        )

    resumed = run_loop(
        [], root=workspace, journal=journal, max_steps=5,
        resume=rebuild(journal.events()),
        model_fn=ScriptedModel(says("it reads fine")),
    )

    assert resumed.ok
    # Two intents on disk, one call counted.
    intents = [e for e in journal.events() if e.get("t") == "tool_intent"]
    assert len(intents) == 2
    assert len(rebuild(journal.events()).calls_done) == 1


def test_an_unarmed_run_reaches_the_tool_limit_instead(workspace):
    """--no-repeat-stop, as Session 12's control arm will run it."""
    batch = [raw_call(f"c{n}", "read_file", "{not json") for n in range(5)]
    result = run_loop(
        [{"role": "user", "content": "go"}],
        root=workspace,
        model_fn=ScriptedModel(wants(*batch), says()),
        budget=Budget(
            max_steps=5, max_repeated_failures=None, max_repeated_calls=None
        ),
    )

    assert result.ok
    assert result.tool_calls == 5
    assert result.repeated_failures == 4, "counted, never acted on"
