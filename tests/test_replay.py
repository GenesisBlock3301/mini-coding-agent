"""Tests for agent.replay - what a resumed run is allowed to run again.

Session 8, Phases 2 and 3. Every test here kills a run between `tool_intent`
and `tool_outcome`, which is the one window where the journal used to describe
two different worlds with the same words.

The kill is an exception raised from inside a patched `dispatch`, not
`os._exit`: a test that really exits takes the test runner with it. The
journal is identical either way, because the intent record is already fsynced
before dispatch is entered - which is the only property these tests depend on.
`scripts/crash_demo.py --edit` does the same thing with a real os._exit in a
real subprocess, and `scripts/fault_matrix.py` does it against the Session 7
fixtures. The three agree; this file is the one that runs in 50ms.
"""

import json

import pytest

from agent import replay
from agent.budget import Budget
from agent.journal import TOOL_INTENT, Journal, new_run_id, rebuild
from agent.loop import run_loop
from agent.model import ModelResponse
from agent.tools import dispatch

TARGET = "crash_target.py"
BEFORE = "def subtract(a, b):\n    return a + b\n"
AFTER = "def subtract(a, b):\n    return a - b\n"


# --------------------------------------------------------------------------
# Scaffolding: a workspace, a scripted model, and a dispatch that dies.
# --------------------------------------------------------------------------


class Boom(Exception):
    """Stands in for SIGKILL. Nothing catches it; the run is over."""


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


def call(call_id="call_edit_1", name="replace_text", **arguments):
    if not arguments:
        arguments = {"path": TARGET, "old": "return a + b", "new": "return a - b"}
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def raw_call(call_id, name, arguments: str):
    """A call whose arguments are a string we control byte for byte."""
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
    return tmp_path


@pytest.fixture
def journal(tmp_path):
    return Journal.create(tmp_path / "runs", new_run_id())


def crash_at(monkeypatch, when: str) -> dict:
    """Patch loop.dispatch to die `before` or `after` the real call.

    Returns a counter dict so a test can prove the tool was - or was not -
    entered a second time.
    """
    import agent.loop as loop_module

    real = loop_module.dispatch
    seen = {"calls": 0, "crashed": False}

    def crashing(name, arguments, *, root):
        # The kill happens once. Afterwards this is the real dispatcher, so
        # the resumed run in the same test is running the real code path.
        seen["calls"] += 1
        if seen["crashed"]:
            return real(name, arguments, root=root)
        seen["crashed"] = True
        if when == "before":
            raise Boom("killed before the write")
        real(name, arguments, root=root)
        raise Boom("killed after the write, before the outcome")

    monkeypatch.setattr(loop_module, "dispatch", crashing)
    return seen


def kill_mid_edit(workspace, journal, monkeypatch, when, *, edit=None):
    """Run until the edit, die there, and hand back the rebuilt state."""
    seen = crash_at(monkeypatch, when)
    model = ScriptedModel(wants(edit or call()))
    with pytest.raises(Boom):
        run_loop(
            [{"role": "user", "content": "fix the subtraction"}],
            root=workspace, journal=journal, model_fn=model, max_steps=5,
        )
    return rebuild(journal.events()), seen


def resume_run(workspace, journal, state, model=None):
    return run_loop(
        [], root=workspace, journal=journal, resume=state,
        model_fn=model or ScriptedModel(says()), max_steps=5,
    )


# --------------------------------------------------------------------------
# D73. The record that makes the question answerable.
# --------------------------------------------------------------------------


def test_the_intent_of_a_write_carries_a_fingerprint(workspace, journal):
    run_loop(
        [{"role": "user", "content": "fix it"}],
        root=workspace, journal=journal, max_steps=5,
        model_fn=ScriptedModel(wants(call()), says()),
    )
    intents = [e for e in journal.events() if e["t"] == TOOL_INTENT]
    assert len(intents) == 1
    assert intents[0]["before"] == replay.digest(BEFORE.encode())


def test_the_intent_of_a_read_carries_none(workspace, journal):
    """Absence is the signal: there was nothing here that could be lost."""
    read = call("call_read_1", name="read_file", path=TARGET)
    run_loop(
        [{"role": "user", "content": "read it"}],
        root=workspace, journal=journal, max_steps=5,
        model_fn=ScriptedModel(wants(read), says()),
    )
    intents = [e for e in journal.events() if e["t"] == TOOL_INTENT]
    assert intents[0]["before"] is None


def test_a_missing_file_fingerprints_as_absent(workspace):
    assert replay.fingerprint(workspace / "nope.py") == replay.ABSENT


def test_a_path_outside_the_workspace_fingerprints_as_outside(workspace):
    before = replay.before_fingerprint(
        "replace_text",
        json.dumps({"path": "../secret.txt", "old": "a", "new": "b"}),
        root=workspace,
    )
    assert before == replay.OUTSIDE


def test_unparseable_arguments_fingerprint_as_no_path(workspace):
    assert replay.before_fingerprint("replace_text", "{not json", root=workspace) == (
        replay.NO_PATH
    )


# --------------------------------------------------------------------------
# D72. The four outcomes, one test each.
# --------------------------------------------------------------------------


def test_the_edit_did_not_land_and_is_retried(workspace, journal, monkeypatch):
    """Outcome 3. The fingerprint matches, so nothing was lost: run it again."""
    state, seen = kill_mid_edit(workspace, journal, monkeypatch, "before")
    assert (workspace / TARGET).read_text() == BEFORE

    result = resume_run(workspace, journal, state)

    assert (workspace / TARGET).read_text() == AFTER
    assert result.changed_paths == [TARGET]
    assert result.interrupted_calls == []


def test_the_edit_landed_and_is_reported(workspace, journal, monkeypatch):
    """Outcome 1, and the whole point of the session.

    The file changed in a process that died before it could say so. The
    resumed run says so instead - without re-entering the tool.
    """
    state, seen = kill_mid_edit(workspace, journal, monkeypatch, "after")
    assert (workspace / TARGET).read_text() == AFTER
    entered_once = seen["calls"]

    result = resume_run(workspace, journal, state)

    assert seen["calls"] == entered_once, "the tool must not be entered again"
    assert result.interrupted_calls == [replay.CHANGED]
    assert result.changed_paths == [TARGET]
    assert result.ok


def test_the_report_of_a_landed_edit_reaches_the_model(workspace, journal, monkeypatch):
    """The tool message is the model's only view of what happened.

    A correct `changed_paths` that the model never sees would still leave it
    believing its edit was lost, and the next thing it does is edit again.
    """
    state, _ = kill_mid_edit(workspace, journal, monkeypatch, "after")

    result = resume_run(workspace, journal, state)

    answer = [m for m in result.messages if m.get("role") == "tool"][-1]
    assert not answer["content"].startswith("ERROR")
    assert "was NOT repeated" in answer["content"]
    assert TARGET in answer["content"]


def test_an_unprovable_call_is_refused_and_reported(workspace, journal, monkeypatch):
    """Outcome 4, made loud. A journal with no fingerprint - one written
    before this session - cannot be decided, so it is not guessed at."""
    state, seen = kill_mid_edit(workspace, journal, monkeypatch, "after")
    for intent in state.in_flight_intents:
        intent.pop("before")
    entered_once = seen["calls"]

    result = resume_run(workspace, journal, state)

    assert seen["calls"] == entered_once
    assert result.interrupted_calls == [replay.UNKNOWN]
    assert result.changed_paths == []
    assert replay.INTERRUPTED_TOOL_ERROR in result.tool_errors
    answer = [m for m in result.messages if m.get("role") == "tool"][-1]
    assert answer["content"].startswith(f"ERROR {replay.INTERRUPTED_TOOL_ERROR}:")


def test_outcome_two_is_what_a_naive_replay_produces(workspace, journal, monkeypatch):
    """The bug this session exists to remove, demonstrated on purpose.

    Replaying a landed edit gets `no_match`: the model is told its edit failed
    while the file on disk says it succeeded. Nothing here asserts that the
    agent does this - it asserts that the TOOL cannot tell the difference,
    which is why the decision had to move into the journal.
    """
    state, _ = kill_mid_edit(workspace, journal, monkeypatch, "after")
    intent = state.in_flight_intents[0]

    naive = dispatch(intent["name"], intent["arguments"], root=workspace)

    assert not naive.ok
    assert naive.error_code == "no_match"
    assert (workspace / TARGET).read_text() == AFTER


# --------------------------------------------------------------------------
# Step 8. Atomic is not idempotent.
# --------------------------------------------------------------------------


def test_an_identical_edit_applied_twice_is_refused_the_second_time(workspace):
    first = dispatch(
        "replace_text",
        json.dumps({"path": TARGET, "old": "return a + b", "new": "return a - b"}),
        root=workspace,
    )
    second = dispatch(
        "replace_text",
        json.dumps({"path": TARGET, "old": "return a + b", "new": "return a - b"}),
        root=workspace,
    )
    assert first.ok and first.changed_path == TARGET
    assert not second.ok and second.error_code == "no_match"


def test_a_zero_match_refusal_cannot_name_its_own_cause(workspace):
    """Two different worlds, one error code. This is the ambiguity D73 breaks.

    An anchor the model invented and an anchor its own successful edit removed
    produce byte-identical results - so no amount of reading the tool result
    can distinguish them. Only a record made BEFORE the call can.
    """
    dispatch(
        "replace_text",
        json.dumps({"path": TARGET, "old": "return a + b", "new": "return a - b"}),
        root=workspace,
    )
    already_applied = dispatch(
        "replace_text",
        json.dumps({"path": TARGET, "old": "return a + b", "new": "return a - b"}),
        root=workspace,
    )
    never_existed = dispatch(
        "replace_text",
        json.dumps({"path": TARGET, "old": "return a * b", "new": "return a - b"}),
        root=workspace,
    )
    assert already_applied.error_code == never_existed.error_code == "no_match"
    assert already_applied.content == never_existed.content, (
        "byte-identical refusals: the tool result carries nothing that could "
        "tell a successful edit from a wrong anchor"
    )


def test_a_replay_that_would_apply_twice_does_not(workspace, journal, monkeypatch):
    """The expensive case: `new` contains `old`, so a replay compounds.

    `no_match` at least fails loudly. An edit whose result still contains its
    own anchor applies again and SUCCEEDS, and the file is wrong in a way no
    error code reports.
    """
    (workspace / "fees.py").write_text("FEE = 10\n", encoding="utf-8")
    edit = call("call_fee_1", path="fees.py", old="= 10", new="= 10 + 5")

    state, seen = kill_mid_edit(workspace, journal, monkeypatch, "after", edit=edit)
    assert (workspace / "fees.py").read_text() == "FEE = 10 + 5\n"
    entered_once = seen["calls"]

    result = resume_run(workspace, journal, state)

    assert (workspace / "fees.py").read_text() == "FEE = 10 + 5\n"
    assert seen["calls"] == entered_once
    assert result.interrupted_calls == [replay.CHANGED]


def test_an_in_flight_read_is_simply_replayed(workspace, journal, monkeypatch):
    """A read cannot have changed anything, so there is nothing to decide."""
    read = call("call_read_1", name="read_file", path=TARGET)
    state, seen = kill_mid_edit(workspace, journal, monkeypatch, "before", edit=read)

    result = resume_run(workspace, journal, state)

    assert seen["calls"] == 2, "the read was entered again"
    assert result.interrupted_calls == []
    assert result.ok


def test_a_third_party_edit_is_treated_as_changed(workspace, journal, monkeypatch):
    """The hash says the bytes moved. It does not say who moved them - and
    both readings have the same answer: do not apply this edit blind."""
    state, _ = kill_mid_edit(workspace, journal, monkeypatch, "before")
    (workspace / TARGET).write_text("# someone else rewrote this\n", encoding="utf-8")

    result = resume_run(workspace, journal, state)

    assert result.interrupted_calls == [replay.CHANGED]
    assert (workspace / TARGET).read_text() == "# someone else rewrote this\n"


def test_an_interrupted_call_still_costs_a_tool_call(workspace, journal, monkeypatch):
    """A budget you can refill with kill -9 is not a budget (D39)."""
    state, _ = kill_mid_edit(workspace, journal, monkeypatch, "after")

    result = resume_run(workspace, journal, state)

    assert result.tool_calls == 1


# --------------------------------------------------------------------------
# Phase 3. D74 and D75: the failures that already work, and what they cost.
# --------------------------------------------------------------------------


def test_a_hallucinated_tool_name_costs_a_tool_call(workspace):
    model = ScriptedModel(
        wants(
            call("call_good", name="read_file", path=TARGET),
            raw_call("call_bad", "open_browser", '{"url": "x"}'),
        ),
        says(),
    )
    result = run_loop(
        [{"role": "user", "content": "go"}],
        root=workspace, model_fn=model, max_steps=5,
    )

    assert result.tool_calls == 2
    assert result.tool_errors == ["unknown_tool"]
    answers = [m for m in result.messages if m.get("role") == "tool"]
    assert len(answers) == 2
    assert "Available tools" in answers[1]["content"]


def test_five_malformed_calls_now_stop_the_run_and_are_still_counted(workspace):
    """D75's measurement, under Session 9's rule.

    This test asserted `result.ok` and the comment "nothing in the loop ends
    a run for repeating itself" until Session 9. That sentence was the point
    of D75 - take the measurement, leave the stopping to a session that can
    design it - and it is now false by decision rather than by accident.

    What did NOT change is everything around the stop: every call in the
    batch is still answered, the error codes are still the tool's own, and
    the repeat count is still reported.
    """
    bad = [raw_call(f"call_{n}", "read_file", "{not json") for n in range(5)]
    model = ScriptedModel(wants(*bad), says())

    result = run_loop(
        [{"role": "user", "content": "go"}],
        root=workspace, model_fn=model, max_steps=5,
    )

    assert result.stop_reason == "repetition"
    # Three ran - the third is the one that tripped the rule - and the two
    # behind it were refused rather than executed (D33: answered, not run).
    assert result.tool_errors == ["invalid_arguments"] * 3
    assert result.tool_calls == 3
    assert result.repeated_failures == 2
    assert result.longest_repeat == 3
    assert "read_file" in (result.repetition_stopped_on or "")
    # E1 still holds: five calls, five tool messages.
    answers = [m for m in result.messages if m.get("role") == "tool"]
    assert len(answers) == 5


def test_the_tool_call_limit_still_bounds_them_when_repetition_is_off(workspace):
    """Session 8's bound, with Session 9's rule disarmed.

    The name used to say "the only thing". It was true for one session, and
    keeping the test honest means keeping the case it covered: with the
    repetition rule off, the tool-call cap is what remains - which is also
    exactly the control arm Session 12 will compare against.
    """
    bad = [raw_call(f"call_{n}", "read_file", "{not json") for n in range(5)]
    model = ScriptedModel(wants(*bad))

    result = run_loop(
        [{"role": "user", "content": "go"}],
        root=workspace, model_fn=model,
        budget=Budget(
            max_steps=5,
            max_tool_calls=3,
            max_repeated_failures=None,
            max_repeated_calls=None,
        ),
    )

    assert result.stop_reason == "tool_limit"
    assert result.tool_calls == 3
    # Counted anyway. A control arm that cannot report the thing it is not
    # acting on is not a control arm.
    assert result.repeated_failures == 2


def test_a_different_failing_call_is_not_a_repeat(workspace):
    """Repeats are counted byte-identically. A model that varies its mistake
    is making progress of a kind; one that does not is stuck."""
    model = ScriptedModel(
        wants(
            raw_call("a", "read_file", '{"path": "missing_one.py"}'),
            raw_call("b", "read_file", '{"path": "missing_two.py"}'),
        ),
        says(),
    )
    result = run_loop(
        [{"role": "user", "content": "go"}],
        root=workspace, model_fn=model, max_steps=5,
    )

    assert result.tool_errors == ["not_found", "not_found"]
    assert result.repeated_failures == 0


def test_two_calls_sharing_an_id_are_both_answered_and_reported(workspace):
    model = ScriptedModel(
        wants(
            call("same", name="read_file", path=TARGET),
            call("same", name="read_file", path=TARGET),
        ),
        says(),
    )
    result = run_loop(
        [{"role": "user", "content": "go"}],
        root=workspace, model_fn=model, max_steps=5,
    )

    assert result.malformed_call_ids == ["same"]
    assert len([m for m in result.messages if m.get("role") == "tool"]) == 2


def test_a_call_with_no_id_is_answered_and_reported(workspace):
    model = ScriptedModel(
        wants(raw_call("", "read_file", json.dumps({"path": TARGET}))), says()
    )
    result = run_loop(
        [{"role": "user", "content": "go"}],
        root=workspace, model_fn=model, max_steps=5,
    )

    assert result.malformed_call_ids == [""]
    answer = [m for m in result.messages if m.get("role") == "tool"][0]
    assert answer["tool_call_id"] == ""


# --------------------------------------------------------------------------
# Step 10. The diagnostics, on the unhappy path.
#
# "Confirm the warning still fires when the run ALSO hits a limit." A
# diagnostic that only prints on the happy path is a diagnostic you do not
# have - and a limit stop is precisely when a user needs to know that two
# tool calls shared an id, or that four of their five calls were the same
# mistake resent.
# --------------------------------------------------------------------------


def report_of(result, capsys):
    from agent.cli import report_totals

    report_totals(result)
    return capsys.readouterr().err


def test_the_malformed_warning_fires_on_a_limit_stop(workspace, capsys):
    model = ScriptedModel(
        wants(
            call("same", name="read_file", path=TARGET),
            call("same", name="read_file", path=TARGET),
        )
    )
    result = run_loop(
        [{"role": "user", "content": "go"}],
        root=workspace, model_fn=model,
        budget=Budget(max_steps=1),
    )

    assert result.stopped_by_limit
    report = report_of(result, capsys)
    assert "missing or duplicated id" in report


def test_the_tool_error_tally_fires_on_a_limit_stop(workspace, capsys):
    bad = [raw_call(f"call_{n}", "read_file", "{not json") for n in range(5)]
    result = run_loop(
        [{"role": "user", "content": "go"}],
        root=workspace, model_fn=ScriptedModel(wants(*bad)),
        budget=Budget(max_steps=1),
    )

    report = report_of(result, capsys)
    assert "tool errors: invalid_arguments x3" in report
    assert "repeats: 2 failing, 0 succeeding, longest run 3" in report


def test_a_clean_run_says_nothing_about_errors(workspace, capsys):
    result = run_loop(
        [{"role": "user", "content": "go"}],
        root=workspace, max_steps=5,
        model_fn=ScriptedModel(wants(call("c1", name="read_file", path=TARGET)), says()),
    )

    report = report_of(result, capsys)
    assert "tool errors" not in report
    assert "WARNING" not in report


def test_the_resume_report_names_each_in_flight_call(workspace, journal, monkeypatch, capsys):
    from agent.cli import report_resume

    state, _ = kill_mid_edit(workspace, journal, monkeypatch, "after")
    report_resume(state, workspace)

    report = capsys.readouterr().err
    assert "in flight: replace_text" in report
    assert "already applied, not repeated" in report


def test_the_resume_report_names_an_unprovable_call(workspace, journal, monkeypatch, capsys):
    from agent.cli import report_resume

    state, _ = kill_mid_edit(workspace, journal, monkeypatch, "after")
    for intent in state.in_flight_intents:
        intent.pop("before")
    report_resume(state, workspace)

    assert "UNPROVABLE" in capsys.readouterr().err


# --------------------------------------------------------------------------
# D52 and D69 across a restart.
#
# Session 6 collected changed files in a dataclass belonging to one process.
# Phase 4 pointed a fixture verifier at a killed run and found the gap: the
# file was fixed and the run that came after it said it had changed nothing.
# --------------------------------------------------------------------------


def complete_edit(workspace, journal):
    """Run one edit to completion, then hand back the rebuilt state.

    The crash here is after the outcome is durable, which is the ordinary
    case: no call is in flight, and everything about the edit is on disk.
    """
    model = ScriptedModel(wants(call()))
    run_loop(
        [{"role": "user", "content": "fix the subtraction"}],
        root=workspace, journal=journal, model_fn=model,
        budget=Budget(max_steps=1),
    )
    return rebuild(journal.events())


def test_a_resumed_run_reports_what_the_dead_process_changed(workspace, journal):
    state = complete_edit(workspace, journal)
    assert state.changed_paths == [TARGET]
    assert state.in_flight_intents == [], "nothing was in flight; the edit finished"

    result = resume_run(workspace, journal, state)

    assert result.changed_paths == [TARGET]
    assert (workspace / TARGET).read_text() == AFTER


def test_a_write_in_a_dead_process_still_blocks_an_unsafe_retry(workspace, journal):
    """D69, across a restart.

    A read timeout may already have reached the server. The reason that
    matters is that a tool has written, and `kill -9` between the write and
    the retry does not unwrite it.
    """
    from agent.model import ModelError
    from agent.retry import RetryPolicy

    state = complete_edit(workspace, journal)
    sent = {"n": 0}

    def times_out(messages, *, tools=None):
        sent["n"] += 1
        raise ModelError("timeout", "read timeout", retryable=True, safe_to_retry=False)

    with pytest.raises(ModelError) as caught:
        run_loop(
            [], root=workspace, journal=journal, resume=state,
            model_fn=times_out, max_steps=5,
            retry=RetryPolicy(max_attempts=3, base_delay_s=0, jitter=False),
            sleep=lambda seconds: None,
        )

    assert sent["n"] == 1, "a request that may have arrived was not sent again"
    assert caught.value.gave_up_because == "unsafe_after_write"


def test_a_landed_edit_is_reported_even_when_the_budget_has_run_out(
    workspace, journal, monkeypatch
):
    """An interrupted call is answered before the gate, on purpose.

    The tool ran in a process that died. A limit reached since then cannot
    un-write the file, and "the tool-call limit stopped this" would be a
    false report about an edit that already landed.
    """
    state, seen = kill_mid_edit(workspace, journal, monkeypatch, "after")
    entered_once = seen["calls"]

    result = run_loop(
        [], root=workspace, journal=journal, resume=state,
        model_fn=ScriptedModel(says()),
        budget=Budget(max_steps=5, max_tool_calls=0),
    )

    assert seen["calls"] == entered_once
    assert result.changed_paths == [TARGET]
    assert result.interrupted_calls == [replay.CHANGED]
