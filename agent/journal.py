"""The run journal: the only durable state the agent has.

Owns: appending one JSON record per event, reading them back, and rebuilding
the exact message history a crashed run had in memory.

    runs/20261105T143012-a19f3c.jsonl

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

RUN_STARTED = "run_started"
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
    """Sortable by time, unique by luck. `ls runs/` is then chronological."""
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


def rebuild(events: list[dict[str, Any]]) -> ResumeState:
    """Replay the journal into the history the dead process had in RAM.

    Returns what is still owed:
      answer_ready            the final answer was already generated and paid
                              for - resuming costs zero API calls
      unanswered_calls        tool calls with no recorded outcome
      in_flight_call_ids      a subset: dispatch was entered but never
                              returned. Safe to re-run only if the tool is
                              idempotent or atomic.
      model_call_was_in_flight  a POST may have been billed with no response.
                              Unrecoverable - but now it is known, not silent.
      token totals            what was already paid for, so the resumed run's
                              ledger starts where the dead one stopped.

    A tool_outcome with no preceding tool_intent is not corruption: it is a
    call that was ANSWERED without ever being run, because a budget died
    before its turn (Session 5, D33). It counts as replayed history and is
    never re-executed - its id is already answered.
    """
    state = ResumeState()
    outcomes: set[str] = set()
    intents: list[str] = []
    last_tool_calls: list[dict[str, Any]] = []

    for event in events:
        kind = event.get("t")
        if kind == RUN_STARTED:
            state.messages = list(event.get("messages") or [])
            state.task = event.get("task")
        elif kind == MODEL_PENDING:
            state.model_call_was_in_flight = True
        elif kind == MODEL_RESULT:
            state.model_call_was_in_flight = False
            message = event.get("message") or {}
            state.messages.append(message)
            state.steps_done += 1
            usage = event.get("usage") or {}
            state.prompt_tokens += usage.get("prompt_tokens", 0)
            state.completion_tokens += usage.get("completion_tokens", 0)
            state.cache_hit_tokens += usage.get("prompt_cache_hit_tokens", 0)
            state.cache_miss_tokens += usage.get("prompt_cache_miss_tokens", 0)
            last_tool_calls = list(message.get("tool_calls") or [])
            # A turn with no tool calls IS the answer. Nothing more is owed.
            state.answer_ready = not last_tool_calls
            state.answer_text = message.get("content") if state.answer_ready else None
        elif kind == TOOL_INTENT:
            intents.append(event.get("call_id", ""))
        elif kind == TOOL_OUTCOME:
            call_id = event.get("call_id", "")
            outcomes.add(call_id)
            state.messages.append(event.get("message") or {})
            state.replayed_tools += 1
            # Refused calls consumed no tool budget - they never ran - but
            # every executed one did, and that spend belongs to the run.
            if not event.get("refused"):
                state.tool_calls_done += 1
        elif kind == RUN_FINISHED:
            state.finished = True

    state.unanswered_calls = [
        call for call in last_tool_calls if call.get("id", "") not in outcomes
    ]
    state.in_flight_call_ids = [
        call_id for call_id in intents if call_id not in outcomes
    ]
    return state
