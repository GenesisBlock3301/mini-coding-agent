"""The run journal: the only durable state the agent has.

Owns: appending one JSON record per event, reading them back, and rebuilding
the exact message history a crashed run had in memory.

    .runtime/journals/20261105T143012-a19f3c.jsonl

The rule the whole thing rests on: **a record is written BEFORE the side
effect it authorises.** `model_result` lands before any tool runs, so a resume
replays the plan the model actually made instead of asking for a new one.
`tool_intent` lands before dispatch, so "did it run?" has an answer.

Why JSONL and not a database: append-only means a crash mid-write corrupts at
most the last line, and a partial last line is detectable and skippable. A
rewritten file can be corrupted anywhere.

Every append is flushed AND fsynced. Without the fsync the record sits in the
OS page cache, which is exactly the thing a power cut takes with it. The cost
is ~1ms against a ~1300ms model call.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Session 10, D94. The shape of the records below, stamped once per run in
# `run_started`. A journal without it is generation 0 - which is 30 of the 132
# files in `.runtime/journals/` today, and every one of them still rebuilds.
#
# The absence table. Six field-level generations exist on disk, discovered by
# grouping every record by its exact key set rather than by reading this
# file's history. Each absence has a MEANING, and that is the whole argument
# for writing them down: Session 8 already turned one of them into a decision
# (no fingerprint -> UNKNOWN -> refuse to replay), which worked because the
# absence was given a meaning rather than a default.
#
#   run_finished without `replayed`      <= 20261105  a run from before resume
#   tool_outcome without `changed_path`  <= 20261114  cannot say what it edited
#   run_started without `workspace`      <= 20261114  cannot say WHERE it ran
#   tool_intent without `before`         <= 20261115  replay refuses: UNKNOWN
#   tool_outcome without `repeat`        <= 20261116  repetition was not counted
#   run_finished without `context`       <= 20261116  no size was measured
#
# A reader may fill none of these in. "Not recorded" and "recorded as zero"
# are different claims about a run and must not be printed the same way.
SCHEMA = 1

RUN_STARTED = "run_started"
# Session 8. Written AFTER an attempt failed and BEFORE the sleep, so a
# journal cut short mid-backoff still names the attempt that was paid for.
MODEL_RETRY = "model_retry"
MODEL_PENDING = "model_pending"
MODEL_RESULT = "model_result"
TOOL_INTENT = "tool_intent"
TOOL_OUTCOME = "tool_outcome"
RUN_FINISHED = "run_finished"


class JournalCorrupt(Exception):
    """A record that cannot be parsed and is not the torn tail of a crash.

    An unparseable LAST line is expected: the process died between write and
    fsync, and `heal` removes it. An unparseable line anywhere else cannot be
    explained by a crash, because records are only ever appended. Skipping it
    would hand `rebuild` a history with a hole in it and let a resume carry on
    from a conversation that never happened - so this is raised instead.
    """


def new_run_id() -> str:
    """Sortable by time, unique by luck. `ls .runtime/journals/` is then chronological."""
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


@dataclass
class Journal:
    """An append-only file. No committed record is ever rewritten.

    One exception, and it is the whole of `heal`: a record that was still
    being written when the process died is not a committed record, and it is
    removed before anything is appended after it. Everything that survived a
    full fsync is immutable.
    """

    path: Path
    # Healing is needed at most once per instance: after the first append the
    # file demonstrably ends in a newline, because we just wrote one.
    _healed: bool = False

    @classmethod
    def create(cls, directory: Path, run_id: str) -> Journal:
        directory.mkdir(parents=True, exist_ok=True)
        return cls(directory / f"{run_id}.jsonl")

    @classmethod
    def open(cls, directory: Path, run_id: str) -> Journal:
        path = directory / f"{run_id}.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"no journal for run {run_id!r} at {path}")
        return cls(path)

    def heal(self) -> int:
        """Remove a torn final record. Returns the number of bytes discarded.

        A crash between `write` and `fsync` can leave a partial last line, and
        a partial line has NO trailing newline. Append mode then writes the
        next record onto that same line, producing one line holding the tail
        of a dead record glued to the head of a live one. Neither parses, and
        `events` would drop BOTH - losing a record that was written correctly
        and fsynced. That is silent data loss, and it is the reason this
        method exists rather than being left to the reader to tolerate.

        The fix is the one Redis uses for a truncated AOF: cut back to the
        last complete record before writing anything new.

        Reading the whole file to find the last newline is fine at this scale
        - `events` already does - and a backwards chunked scan would be the
        change to make if journals ever got large.

        Idempotent: a file that already ends in a newline, an empty file, and
        a file that does not exist are all no-ops returning 0.
        """
        if not self.path.is_file():
            return 0

        size = self.path.stat().st_size
        if size == 0:
            return 0

        with self.path.open("rb+") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) == b"\n":
                return 0

            handle.seek(0)
            # +1 keeps the newline that terminates the last good record.
            # rfind returning -1 (no newline at all) gives keep=0, which is
            # correct: the file holds nothing but a torn first record.
            keep = handle.read().rfind(b"\n") + 1
            handle.truncate(keep)
            handle.flush()
            os.fsync(handle.fileno())

        return size - keep

    def append(self, event_type: str, **payload: Any) -> None:
        """One record, durable before this returns.

        Opened per call rather than held open: there is no handle to leak, no
        close() to forget, and no half-written buffer owned by a process that
        is about to stop existing.

        Heals first, once. Doing it here rather than in `open` means reading a
        journal never modifies it, and the repair happens exactly when the
        file is about to be written anyway.
        """
        if not self._healed:
            self.heal()
            self._healed = True

        record = {"t": event_type, "at": time.time(), **payload}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def events(self) -> list[dict[str, Any]]:
        """Every record, in order. A torn FINAL line is dropped, not fatal.

        That torn line is the crash itself: the process died between write and
        fsync. Dropping it is correct - the event it describes never completed.

        An unparseable line anywhere else raises JournalCorrupt. Records are
        only ever appended, so nothing a crash can do explains a bad line in
        the middle; it means real damage. Skipping it would silently hand
        `rebuild` a history with a hole in it, and a resumed run would carry
        on from a conversation that never took place. Refusing is the smaller
        failure, and it names the line so the file can be inspected by hand.

        Reading does not modify the file - `heal` is called by `append`, not
        here - so inspecting a crashed journal leaves it exactly as the crash
        left it.
        """
        lines = self.path.read_text(encoding="utf-8").splitlines()
        events: list[dict[str, Any]] = []

        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as err:
                if number == len(lines):
                    continue
                raise JournalCorrupt(
                    f"{self.path}: line {number} of {len(lines)} is not valid "
                    f"JSON and is not the final line ({err.msg}). The run "
                    "cannot be rebuilt from this journal."
                ) from err

        return events


@dataclass
class ResumeState:
    """What a journal says about where a dead run got to."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    task: str | None = None
    steps_done: int = 0
    unanswered_calls: list[dict[str, Any]] = field(default_factory=list)
    in_flight_call_ids: list[str] = field(default_factory=list)
    # Session 8, D73. The whole `tool_intent` record for each of those ids -
    # name, arguments, and the `before` fingerprint written with it. The ids
    # alone were enough while every tool was a read; deciding whether an
    # interrupted WRITE may be repeated needs to know which file it named and
    # what that file contained when the call started (agent/replay.py).
    in_flight_intents: list[dict[str, Any]] = field(default_factory=list)
    # Session 8. Files the RUN has already changed, dead process included.
    # Session 6's D52 made this a per-process list, which was true while a
    # crash ended the story. It no longer does: a resumed run that reports
    # "changed 0 files" after its first process edited one is making the
    # claim the whole of Phase 2 exists to stop - and `writes_done` in the
    # retry policy (D69) reads the same list to decide whether a request
    # that may already have arrived can be sent again.
    changed_paths: list[str] = field(default_factory=list)
    # Session 9. Every call the dead process ANSWERED, as
    # {name, arguments, ok} - enough to rebuild the repetition counts
    # without a new event type. The journal already held all three; what it
    # lacked was a reader that paired the intent with its outcome, because
    # until today nothing needed to know how often a call had been made.
    calls_done: list[dict[str, Any]] = field(default_factory=list)
    answer_text: str | None = None
    answer_ready: bool = False
    finished: bool = False
    model_call_was_in_flight: bool = False
    replayed_tools: int = 0
    # What the dead process already spent. A budget that does not carry these
    # across a restart is a budget you can reset with `kill -9` (Session 5,
    # D39). Seconds are deliberately NOT here: the wall clock protects this
    # process's responsiveness, tokens and tool calls protect the run.
    tool_calls_done: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0

    @property
    def needs_model_call(self) -> bool:
        return not self.answer_ready and not self.unanswered_calls


def _apply_model_result(state: ResumeState, event: dict[str, Any]) -> list[dict[str, Any]]:
    """Fold one model_result into the state. Returns the tool calls it asked for.

    Split out of `rebuild` so the event loop there stays a flat table of
    "which kind is this" rather than a table with two long paragraphs in it.
    """
    state.model_call_was_in_flight = False
    message = event.get("message") or {}
    state.messages.append(message)
    state.steps_done += 1

    usage = event.get("usage") or {}
    state.prompt_tokens += usage.get("prompt_tokens", 0)
    state.completion_tokens += usage.get("completion_tokens", 0)
    state.cache_hit_tokens += usage.get("prompt_cache_hit_tokens", 0)
    state.cache_miss_tokens += usage.get("prompt_cache_miss_tokens", 0)

    tool_calls = list(message.get("tool_calls") or [])
    # A turn with no tool calls IS the answer. Nothing more is owed.
    state.answer_ready = not tool_calls
    state.answer_text = message.get("content") if state.answer_ready else None
    return tool_calls


def _apply_tool_outcome(state: ResumeState, event: dict[str, Any]) -> tuple[str, bool]:
    """Fold one tool_outcome in. Returns (call_id, ok) - the id it answered,
    and whether the tool reported success, which Session 9 needs to rebuild
    the repetition counts."""
    state.messages.append(event.get("message") or {})
    state.replayed_tools += 1
    changed = event.get("changed_path")
    if changed:
        state.changed_paths.append(changed)
    # Refused calls consumed no tool budget - they never ran - but every
    # executed one did, and that spend belongs to the run.
    if not event.get("refused"):
        state.tool_calls_done += 1
    return event.get("call_id", ""), bool(event.get("ok"))


def rebuild(events: list[dict[str, Any]]) -> ResumeState:
    """Replay the journal into the history the dead process had in RAM.

    Returns what is still owed:
      answer_ready            the final answer was already generated and paid
                              for - resuming costs zero API calls
      unanswered_calls        tool calls with no recorded outcome
      in_flight_call_ids      a subset: dispatch was entered but never
                              returned. Safe to re-run only if the tool is
                              idempotent or atomic - and replace_text is
                              atomic and NOT idempotent, which is why
                              in_flight_intents carries the whole record and
                              agent/replay.py decides per call (Session 8).
      in_flight_intents       those calls' intent records, fingerprint
                              included.
      model_call_was_in_flight  a POST may have been billed with no response.
                              Unrecoverable - but now it is known, not silent.
      token totals            what was already paid for, so the resumed run's
                              ledger starts where the dead one stopped.
      changed_paths           files the dead process successfully changed.
      calls_done              every answered call's name/arguments/ok, which
                              is what the repetition counter is rebuilt from
                              (Session 9).

    A tool_outcome with no preceding tool_intent is not corruption: it is a
    call that was ANSWERED without ever being run, because a budget died
    before its turn (Session 5, D33). It counts as replayed history and is
    never re-executed - its id is already answered.
    """
    state = ResumeState()
    outcomes: dict[str, bool] = {}
    intents: list[dict[str, Any]] = []
    last_tool_calls: list[dict[str, Any]] = []

    for event in events:
        kind = event.get("t")
        if kind == RUN_STARTED:
            state.messages = list(event.get("messages") or [])
            state.task = event.get("task")
        elif kind == MODEL_PENDING:
            state.model_call_was_in_flight = True
        elif kind == MODEL_RESULT:
            last_tool_calls = _apply_model_result(state, event)
        elif kind == TOOL_INTENT:
            intents.append(event)
        elif kind == TOOL_OUTCOME:
            call_id, ok = _apply_tool_outcome(state, event)
            outcomes[call_id] = ok
        elif kind == RUN_FINISHED:
            state.finished = True

    state.unanswered_calls = [
        call for call in last_tool_calls if call.get("id", "") not in outcomes
    ]
    state.in_flight_intents = [
        intent for intent in intents if intent.get("call_id", "") not in outcomes
    ]
    state.in_flight_call_ids = [
        intent.get("call_id", "") for intent in state.in_flight_intents
    ]
    # Session 9. An intent WITH an outcome is a call that completed, and its
    # name and arguments are exactly what the repetition rule counts. An
    # intent without one is in flight and is deliberately absent: it was
    # entered, not answered, and agent/replay.py decides what becomes of it.
    #
    # Keyed by call id, LAST intent wins, because a resumed run that re-ran
    # an interrupted call wrote a second intent for the same id. One id is
    # one call: counting the crash as a repeat would mean `kill -9` inflates
    # a repetition counter, which is the mirror image of the bug D39 fixed
    # for tokens. (Found by scripts/repeat_audit.py against this project's
    # own Session 8 journals, not by thinking about it.)
    answered: dict[str, dict[str, Any]] = {}
    for intent in intents:
        call_id = intent.get("call_id", "")
        if call_id in outcomes:
            answered[call_id] = intent
    state.calls_done = [
        {
            "name": intent.get("name") or "",
            "arguments": intent.get("arguments"),
            "ok": outcomes.get(call_id, False),
        }
        for call_id, intent in answered.items()
    ]
    return state
