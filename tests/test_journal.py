"""The journal, and what a crash can do to it.

Every test here is deterministic. None of them kills a process: a real crash
in the microsecond window between `write` and `fsync` is not reproducible on
demand, so the window's RESULT is constructed directly - a file whose last
line has no newline - and the recovery is tested against that.

scripts/crash_demo.py does the real thing and is how you convince yourself
these fixtures describe reality. It is not a substitute for these tests,
because it cannot fail reliably.
"""

from __future__ import annotations

import json

import pytest

from agent.journal import (
    MODEL_RESULT,
    RUN_STARTED,
    Journal,
    JournalCorrupt,
    new_run_id,
    rebuild,
)

GOOD = '{"t": "run_started", "at": 1, "task": "read app.py", "messages": []}\n'
TORN = '{"t": "tool_inte'


def journal_with(tmp_path, text: str) -> Journal:
    path = tmp_path / "run.jsonl"
    path.write_text(text, encoding="utf-8")
    return Journal(path)


# ---- heal: the shapes a file can be in ---------------------------------


def test_heal_is_a_noop_on_a_clean_file(tmp_path):
    journal = journal_with(tmp_path, GOOD)
    assert journal.heal() == 0
    assert journal.path.read_text() == GOOD


def test_heal_is_a_noop_on_an_empty_file(tmp_path):
    journal = journal_with(tmp_path, "")
    assert journal.heal() == 0


def test_heal_is_a_noop_when_the_file_does_not_exist(tmp_path):
    assert Journal(tmp_path / "nothing.jsonl").heal() == 0


def test_heal_removes_a_torn_final_line(tmp_path):
    journal = journal_with(tmp_path, GOOD + TORN)
    assert journal.heal() == len(TORN)
    assert journal.path.read_text() == GOOD


def test_heal_empties_a_file_that_is_nothing_but_a_torn_record(tmp_path):
    journal = journal_with(tmp_path, TORN)
    assert journal.heal() == len(TORN)
    assert journal.path.read_text() == ""


def test_heal_is_idempotent(tmp_path):
    journal = journal_with(tmp_path, GOOD + TORN)
    first = journal.heal()
    assert first > 0
    assert journal.heal() == 0
    assert journal.heal() == 0
    assert journal.path.read_text() == GOOD


# ---- the regression this fix exists for --------------------------------


def test_a_record_appended_after_a_crash_survives(tmp_path):
    """The bug: without heal, the torn tail swallows the next good record.

    Append mode writes onto the unterminated line, so one line holds the end
    of a dead record and the start of a live one. Neither parses and `events`
    drops both - losing a record that was written correctly and fsynced.
    """
    journal = journal_with(tmp_path, GOOD + TORN)

    journal.append(MODEL_RESULT, step=2)

    kinds = [event["t"] for event in journal.events()]
    assert kinds == [RUN_STARTED, MODEL_RESULT]


def test_append_heals_only_once(tmp_path):
    journal = journal_with(tmp_path, GOOD + TORN)

    journal.append(MODEL_RESULT, step=2)
    journal.append(MODEL_RESULT, step=3)
    journal.append(MODEL_RESULT, step=4)

    assert len(journal.events()) == 4
    assert journal._healed is True


def test_every_line_stays_independently_parseable_after_a_repair(tmp_path):
    journal = journal_with(tmp_path, GOOD + TORN)
    journal.append(MODEL_RESULT, step=2)

    for line in journal.path.read_text().splitlines():
        json.loads(line)


# ---- repeated crash, repeated resume -----------------------------------


def test_crash_resume_crash_resume_keeps_every_committed_record(tmp_path):
    """Three crashes, three resumes. Nothing fsynced is ever lost."""
    journal = journal_with(tmp_path, GOOD)

    for step in (2, 3, 4):
        # A resumed process appends...
        journal.append(MODEL_RESULT, step=step)
        # ...and then dies mid-write, leaving a torn tail behind it.
        with journal.path.open("a", encoding="utf-8") as handle:
            handle.write(TORN)
        # The next process is a different object with its own _healed flag.
        journal = Journal(journal.path)

    journal.append(MODEL_RESULT, step=5)

    steps = [event.get("step") for event in journal.events() if "step" in event]
    assert steps == [2, 3, 4, 5]


def test_resume_after_a_crash_rebuilds_the_history(tmp_path):
    """heal + rebuild together: the point of the whole mechanism."""
    journal = Journal(tmp_path / "run.jsonl")
    journal.append(RUN_STARTED, task="read app.py", messages=[{"role": "user"}])
    journal.append(
        MODEL_RESULT,
        step=1,
        message={"role": "assistant", "content": "hi", "tool_calls": None},
        usage={"prompt_tokens": 10, "completion_tokens": 2},
    )
    with journal.path.open("a", encoding="utf-8") as handle:
        handle.write(TORN)

    reopened = Journal(journal.path)
    assert reopened.heal() == len(TORN)

    state = rebuild(reopened.events())
    assert state.answer_ready is True
    assert state.steps_done == 1
    assert state.prompt_tokens == 10
    assert len(state.messages) == 2


# ---- events: tolerant of a crash, intolerant of damage -----------------


def test_events_tolerates_a_torn_final_line_without_healing(tmp_path):
    journal = journal_with(tmp_path, GOOD + TORN)

    assert [event["t"] for event in journal.events()] == [RUN_STARTED]
    # Reading must not modify the file: inspecting a crashed run leaves it
    # exactly as the crash left it.
    assert journal.path.read_text() == GOOD + TORN


def test_events_refuses_a_bad_line_in_the_middle(tmp_path):
    journal = journal_with(tmp_path, GOOD + "{not json}\n" + GOOD)

    with pytest.raises(JournalCorrupt) as caught:
        journal.events()

    message = str(caught.value)
    assert "line 2 of 3" in message
    assert str(journal.path) in message


def test_events_ignores_blank_lines(tmp_path):
    journal = journal_with(tmp_path, GOOD + "\n" + GOOD)
    assert len(journal.events()) == 2


# ---- the invariant the whole design rests on ---------------------------


def test_each_append_is_one_terminated_line(tmp_path):
    journal = Journal(tmp_path / "run.jsonl")

    journal.append(RUN_STARTED, task="t")
    journal.append(MODEL_RESULT, step=1)

    text = journal.path.read_text()
    assert text.endswith("\n")
    assert len(text.splitlines()) == 2


def test_run_ids_are_unique_and_timestamp_prefixed():
    """`ls runs/` is chronological because the prefix sorts lexicographically.

    Not a test that 20 ids sort in order - they are generated inside one
    second and share a prefix, so that would only be testing uuid4. The
    contract is: unique, and prefixed with a sortable timestamp.
    """
    ids = [new_run_id() for _ in range(50)]

    assert len(set(ids)) == 50
    for run_id in ids:
        stamp, _, suffix = run_id.partition("-")
        assert len(stamp) == len("20261113T090529")
        assert stamp[8] == "T"
        assert stamp[:8].isdigit() and stamp[9:].isdigit()
        assert len(suffix) == 6
