"""Tests for agent.tracing - reading a run after it is over.

Session 10. Every test here writes a journal with a scripted model and then
reads it back, because that round trip is the only thing that proves the
reader and the writer agree about what a record means.

The rule the whole file is organised around: **a reader that raises on a
damaged file is a reader you cannot use on the only file you ever urgently
need to read.** Several tests therefore assert that nothing raises, which
looks weak and is the point.
"""

import json

import pytest

from agent.budget import Budget
from agent.journal import SCHEMA, Journal, new_run_id, rebuild
from agent.loop import run_loop
from agent.model import ModelResponse
from agent.tools import TOOL_SCHEMAS
from agent.tracing import absences, load, segments, summary, timeline

TARGET = "stats.py"
BODY = "def tail(values, n):\n    return values[-n + 1:]\n"


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
        text="working on it", model="scripted-7b", finish_reason="tool_calls",
        prompt_tokens=100, completion_tokens=20, total_tokens=120,
        duration_ms=123.4, tool_calls=list(calls),
    )


def says(text="done") -> ModelResponse:
    return ModelResponse(
        text=text, model="scripted-7b", finish_reason="stop",
        prompt_tokens=150, completion_tokens=30, total_tokens=180,
        duration_ms=77.7, tool_calls=[],
    )


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / TARGET).write_text(BODY, encoding="utf-8")
    return tmp_path


@pytest.fixture
def runs(tmp_path):
    return tmp_path / "runs"


def journalled(workspace, runs, model, *, budget=None, tools=TOOL_SCHEMAS):
    """Run once, journalled, and hand back (LoopResult, Trace)."""
    journal = Journal.create(runs, new_run_id())
    result = run_loop(
        [{"role": "user", "content": "fix the window"}],
        root=workspace, model_fn=model, journal=journal, tools=tools,
        budget=budget or Budget(max_steps=6),
    )
    return result, load(journal.path)


# --------------------------------------------------------------------------
# Phase 1 - the record the journal was missing
# --------------------------------------------------------------------------


def test_the_journal_now_records_latency_model_and_the_tool_menu(workspace, runs):
    """D90. All three lived only in ModelResponse, and Session 8 settled what
    happens to things that live in RAM."""
    _, trace = journalled(workspace, runs, ScriptedModel(says()))

    result_record = trace.of("model_result")[0]
    assert result_record["duration_ms"] == pytest.approx(77.7)
    assert result_record["model"] == "scripted-7b"
    assert result_record["tools"]["names"] == [
        "list_files", "list_folders", "read_file", "replace_text",
    ]
    assert len(result_record["tools"]["digest"]) == 12


def test_no_tools_offered_is_recorded_as_none_not_as_an_empty_menu(workspace, runs):
    """`--tools` versus not is a different agent (Session 12's biggest
    confounder). None here means "none were offered", which must stay
    distinguishable from a generation-0 journal that has no key at all."""
    _, trace = journalled(workspace, runs, ScriptedModel(says()), tools=None)

    assert trace.of("model_result")[0]["tools"] is None


def test_the_new_fields_do_not_change_what_rebuild_sees(workspace, runs):
    """Adding fields to an append-only format is the safe direction. This is
    the test that keeps it safe: replay must be blind to them."""
    _, trace = journalled(
        workspace, runs,
        ScriptedModel(wants(call("c1", "read_file", path=TARGET)), says()),
    )
    state = rebuild(trace.events)

    assert state.answer_ready
    assert state.steps_done == 2
    assert [m["role"] for m in state.messages] == [
        "user", "assistant", "tool", "assistant",
    ]


def test_a_run_stamps_its_schema_version(workspace, runs):
    """D94: one field per RUN, not per record."""
    _, trace = journalled(workspace, runs, ScriptedModel(says()))

    assert trace.schema == SCHEMA
    assert trace.of("run_started")[0]["schema"] == SCHEMA


def test_a_journal_without_the_version_is_generation_zero(runs):
    """30 of the 132 journals on disk have no version, and every one of them
    still rebuilds. Absence is a generation, not a parse error."""
    runs.mkdir(parents=True)
    path = runs / "old.jsonl"
    path.write_text(
        json.dumps({"t": "run_started", "at": 1.0, "task": "old"}) + "\n",
        encoding="utf-8",
    )

    assert load(path).schema == 0


# --------------------------------------------------------------------------
# Phase 2, step 5 - the row a machine reads
# --------------------------------------------------------------------------


def test_the_row_agrees_with_the_loop_result(workspace, runs):
    """D99, and the reason it is a test rather than an assumption.

    These numbers come from two different places - one from disk, one from
    the dataclass in RAM - and Session 11 is about to depend on the first
    while debugging against the second. If they disagree, one of them is
    lying and you need to know which BEFORE the eval suite is built on it.
    """
    model = ScriptedModel(
        wants(call("c1", "read_file", path=TARGET)),
        wants(call("c2", "replace_text", path=TARGET,
                   old="values[-n + 1:]", new="values[-n:]")),
        says("fixed it"),
    )
    result, trace = journalled(workspace, runs, model)
    row = summary(trace)

    assert row["stop_reason"] == result.stop_reason
    assert row["tool_calls"] == result.tool_calls
    assert row["prompt_tokens"] == result.prompt_tokens
    assert row["completion_tokens"] == result.completion_tokens
    assert row["total_tokens"] == result.total_tokens
    assert row["changed_paths"] == result.changed_paths
    assert row["steps"] == result.ledger.steps
    assert row["models"] == ["scripted-7b"]


def test_not_recorded_and_recorded_as_zero_are_different_answers(runs):
    """A reader that collapses the two turns "this journal predates latency"
    into "the model answered instantly"."""
    runs.mkdir(parents=True)
    path = runs / "old.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(e) for e in [
                {"t": "run_started", "at": 1.0, "task": "old"},
                {"t": "model_result", "at": 2.0, "step": 1, "message": {}, "usage": {}},
            ]
        ) + "\n",
        encoding="utf-8",
    )
    row = summary(load(path))

    assert row["model_ms"] is None
    assert row["models"] is None
    assert row["cost_usd"] is None


def test_a_resumed_run_is_visible_even_though_replayed_says_false(workspace, runs):
    """`replayed` is true only when a pre-generated ANSWER was replayed, so it
    reads false on most resumes - including the Session 8 live run, whose two
    run_finished records both say false. The honest signal is that there are
    two of them: one per process."""
    journal = Journal.create(runs, new_run_id())
    run_loop(
        [{"role": "user", "content": "go"}], root=workspace, journal=journal,
        model_fn=ScriptedModel(wants(call("c1", "read_file", path=TARGET))),
        budget=Budget(max_steps=1),
    )
    run_loop(
        [], root=workspace, journal=journal, resume=rebuild(journal.events()),
        model_fn=ScriptedModel(says()), budget=Budget(max_steps=4),
    )
    row = summary(load(journal.path))

    assert row["processes"] == 2
    assert row["resumed"] is True
    assert row["stop_reasons"] == ["step_limit", "answer"]


def test_the_whole_run_wall_clock_is_not_the_sum_of_its_processes(workspace, runs):
    """Each run_finished carries its OWN process's elapsed_s (D39 scoped
    seconds to the process deliberately). The span of the timestamps is the
    only number that includes the time the run spent dead."""
    _, trace = journalled(workspace, runs, ScriptedModel(says()))
    row = summary(trace)

    assert row["wall_s"] >= 0.0


# --------------------------------------------------------------------------
# Phase 2, step 4 - the timeline, and the files it must survive
# --------------------------------------------------------------------------


def test_nothing_raises_on_a_crashed_journal(workspace, runs):
    """No run_finished at all: the process died and never came back."""
    journal = Journal.create(runs, new_run_id())
    with pytest.raises(AssertionError):
        run_loop(
            [{"role": "user", "content": "go"}], root=workspace, journal=journal,
            model_fn=ScriptedModel(wants(call("c1", "read_file", path=TARGET))),
            budget=Budget(max_steps=4),
        )

    trace = load(journal.path)
    assert timeline(trace)
    row = summary(trace)
    assert row["finished"] is False
    assert row["stop_reason"] is None


def test_nothing_raises_on_a_torn_tail(workspace, runs):
    """What a crash during a write looks like. The 131 records before it are
    still the only account of what happened to someone's files."""
    _, trace = journalled(workspace, runs, ScriptedModel(says()))
    trace.path.write_text(
        trace.path.read_text(encoding="utf-8") + '{"t": "run_fini',
        encoding="utf-8",
    )

    retorn = load(trace.path)
    assert retorn.damaged == 1
    assert any("torn tail" in line for line in timeline(retorn))
    assert summary(retorn)["damaged_lines"] == 1


def test_nothing_raises_on_an_unknown_event_type(runs):
    """A reader older than the journal it is given. Printing the record is
    the only honest thing to do with a type you do not know."""
    runs.mkdir(parents=True)
    path = runs / "future.jsonl"
    path.write_text(
        json.dumps({"t": "quantum_entanglement", "at": 1.0, "spooky": True}) + "\n",
        encoding="utf-8",
    )

    lines = timeline(load(path))
    assert any("quantum_entanglement" in line for line in lines)


def test_nothing_raises_on_an_empty_or_missing_file(runs):
    runs.mkdir(parents=True)
    (runs / "empty.jsonl").write_text("", encoding="utf-8")

    assert timeline(load(runs / "empty.jsonl"))
    assert load(runs / "nope.jsonl").events == []


def test_a_retry_is_visible_without_grep(workspace, runs):
    """Session 8's retries and Session 9's repeats are the two things a
    person opens a trace to find."""
    journal = Journal.create(runs, new_run_id())
    journal.append(
        "model_retry", step=1, attempt=1, kind="server", retrying=True,
        delay_s=0.5, reason="retryable", retryable=True, safe_to_retry=True,
    )

    lines = timeline(load(journal.path))
    assert any("!! RETRY" in line and "attempt 1" in line for line in lines)


def test_an_interrupted_edit_is_visible_in_the_timeline(workspace, runs):
    """The Session 10 review checkpoint: answer Session 8's self-test #8 -
    did the edit land? - from the trace alone."""
    journal = Journal.create(runs, new_run_id())
    journal.append(
        "tool_outcome", step=4, call_id="c1", ok=True, error_code=None,
        changed_path=TARGET, message={"role": "tool", "content": "applied"},
        interrupted="changed", before="sha256:aaa", now="sha256:bbb",
    )

    lines = timeline(load(journal.path))
    assert any("INTERRUPTED=changed" in line and "not repeated" in line
               for line in lines)


# --------------------------------------------------------------------------
# Phase 3 - safe to share
# --------------------------------------------------------------------------


def test_tool_output_is_bounded_by_default(workspace, runs):
    """D92. The journal keeps the file (replay needs it); the reader is where
    the bound belongs, because the artefact people paste is this one."""
    (workspace / "big.py").write_text("SECRET_LOOKING_VALUE = 1\n" * 500, "utf-8")
    _, trace = journalled(
        workspace, runs,
        ScriptedModel(wants(call("c1", "read_file", path="big.py")), says()),
    )

    bounded = "\n".join(timeline(trace))
    assert "tool output bounded" in bounded
    assert bounded.count("SECRET_LOOKING_VALUE") <= 5
    assert len(bounded) < 2_000


def test_full_opts_back_in_and_says_so(workspace, runs):
    (workspace / "big.py").write_text("SECRET_LOOKING_VALUE = 1\n" * 500, "utf-8")
    _, trace = journalled(
        workspace, runs,
        ScriptedModel(wants(call("c1", "read_file", path="big.py")), says()),
    )

    verbatim = "\n".join(timeline(trace, full=True))
    assert "tool output verbatim" in verbatim
    assert verbatim.count("SECRET_LOOKING_VALUE") > 100


def test_the_bounded_form_still_answers_the_questions_a_trace_is_asked(
    workspace, runs
):
    """Which file, how big, did it change. If the bound cannot answer those,
    the bound is in the wrong place."""
    _, trace = journalled(
        workspace, runs,
        ScriptedModel(
            wants(call("c1", "replace_text", path=TARGET,
                       old="values[-n + 1:]", new="values[-n:]")),
            says(),
        ),
    )

    lines = "\n".join(timeline(trace))
    assert TARGET in lines
    assert "changed=" in lines
    assert "chars]" in lines


def test_the_api_key_is_absent_by_construction_not_by_filtering(
    workspace, runs, monkeypatch
):
    """D93. The key lives in an HTTP header and no request object is ever
    logged wholesale, so no record can contain it. A deny-list regex over
    `sk-...` would be a worse version of this guarantee: it implies the key
    might be there, and it fails on the first provider with another prefix.
    """
    monkeypatch.setenv("DEEPSEEK_APIKEY", "sk-should-never-be-written-anywhere")
    _, trace = journalled(
        workspace, runs,
        ScriptedModel(wants(call("c1", "read_file", path=TARGET)), says()),
    )

    assert "sk-should-never-be-written-anywhere" not in trace.path.read_text("utf-8")


def test_absences_say_what_a_journal_cannot_tell_you(runs):
    runs.mkdir(parents=True)
    path = runs / "old.jsonl"
    path.write_text(
        json.dumps({"t": "run_started", "at": 1.0, "task": "old"}) + "\n", "utf-8"
    )

    report = "\n".join(absences(load(path)))
    assert "schema 0" in report
    assert "cannot say which model answered" in report


# --------------------------------------------------------------------------
# D100 - a journal is not always one run
# --------------------------------------------------------------------------


def test_one_journal_can_hold_many_runs(workspace, runs):
    """chat.py's shape: one journal per SESSION, one run per TURN (D56).

    cli.py writes the other shape, one run per file, and every reader here
    was written against that one.
    """
    journal = Journal.create(runs, new_run_id())
    for question in ("first", "second", "third"):
        run_loop(
            [{"role": "user", "content": question}],
            root=workspace,
            model_fn=ScriptedModel(says()),
            journal=journal,
            tools=TOOL_SCHEMAS,
            budget=Budget(max_steps=6),
        )

    turns = segments(load(journal.path))

    assert len(turns) == 3
    assert [t._first("run_started")["task"] for t in turns] == [
        "first", "second", "third",
    ]
    assert [len(t.of("model_result")) for t in turns] == [1, 1, 1]


def test_a_single_run_journal_is_one_segment(workspace, runs):
    _, trace = journalled(workspace, runs, ScriptedModel(says()))

    assert len(segments(trace)) == 1
    assert segments(trace)[0].events == trace.events


def test_a_torn_tail_belongs_to_the_run_that_was_in_progress(runs):
    """A damaged line is the end of the file, so it is the last run's."""
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / "two.jsonl"
    path.write_text(
        json.dumps({"t": "run_started", "at": 1.0, "task": "one"}) + "\n"
        + json.dumps({"t": "run_finished", "at": 2.0, "stop_reason": "answer"}) + "\n"
        + json.dumps({"t": "run_started", "at": 3.0, "task": "two"}) + "\n"
        + '{"t": "model_res',
        encoding="utf-8",
    )

    turns = segments(load(path))

    assert [t.damaged for t in turns] == [0, 1]


def test_the_row_still_reads_a_session_journal_as_one_run(workspace, runs):
    """Known and deliberate, recorded so the next change is a decision.

    `summary()` predates D100 and still folds a whole journal into one row:
    the FIRST task beside the LAST turn's totals, and `resumed` true for a
    conversation that never restarted. `segments()` is the material for
    fixing it; this test pins what it does today so the fix cannot be silent.
    """
    journal = Journal.create(runs, new_run_id())
    for question in ("first", "second"):
        run_loop(
            [{"role": "user", "content": question}],
            root=workspace,
            model_fn=ScriptedModel(says()),
            journal=journal,
            tools=TOOL_SCHEMAS,
            budget=Budget(max_steps=6),
        )

    row = summary(load(journal.path))

    assert row["task"] == "first"
    assert row["processes"] == 2
    assert row["resumed"] is True        # no restart happened
    assert row["requests"] == 2          # session-wide
    assert row["total_tokens"] == 180    # one turn's
