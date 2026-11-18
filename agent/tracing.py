"""A run you can read after it is over. (Session 10)

JSONL event traces: requests, tool calls, results, usage, stop reasons - and
the plan's criterion for this session already passed before a line of it was
written, because `journal.py` has recorded all five since Session 6. So this
module is not a logger. It is the answer to the question that accident poses:
**what is a trace that the journal is not?**

D89: a trace is a READING of a journal, and this module writes nothing.

    journal.jsonl  --->  tracing.py  --->  timeline (for a person)
      the only writer      no writes   --->  one row  (for Session 11)

The alternative was a second stream, and its failure mode is specific: two
files that can disagree, where a run that recovered correctly while its trace
says otherwise is a debugging session nobody wins. The cost of this choice is
that a reader can only ever show what was recorded, which is why Session 10
Phase 1 came first and put `duration_ms`, `model` and `tools` into the journal
where they belong.

The obligations are different and that is the whole reason for the split:

    journal   a recovery protocol. Written BEFORE the side effect it
              authorises, fsynced, never rewritten, and `rebuild()` depends
              on its exact shape. Every field added here is a field the
              replay path must tolerate forever.
    trace     an observation. Derived, discardable, free to change shape
              between versions, and not in any hot path.

D92: the journal holds every tool result IN FULL - which means the contents of
every file the agent read. That is correct for replay and it is the wrong
thing to paste into an issue. So content is bounded HERE, in the reader,
rather than withheld at write time, and the header says which form you are
looking at. `--full` opts back in.

D93, on secrets: the API key is safe by CONSTRUCTION, not by filtering. It
lives in an HTTP header, `model.py` never logs a request object wholesale, and
no record in any of the 132 journals contains it. A deny-list regex over
`sk-...` would be a worse version of that same guarantee - it would imply the
key might be there, and it would fail on the first provider that uses another
prefix. What this module bounds is the WORKSPACE, which is the real risk: the
user's source code, in a file that gets pasted into chat windows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from agent.journal import (
    MODEL_PENDING,
    MODEL_RESULT,
    MODEL_RETRY,
    RUN_FINISHED,
    RUN_STARTED,
    SCHEMA,
    TOOL_INTENT,
    TOOL_OUTCOME,
)

from agent.storage import JOURNALS_DIR, PATHS

RUNS_DIR = JOURNALS_DIR

# How much of a tool result the bounded form keeps. One line is enough to say
# what KIND of thing came back - a listing, a refusal, a file's first line -
# and not enough to reconstruct a file.
BOUNDED_CHARS = 120

# What a missing field means, by the generation that introduced it (D94).
# Printed by `--absences` so a reader of an old journal is told what the
# journal cannot say, rather than being shown a zero and believing it.
ABSENCES = {
    "replayed": "pre-resume run; cannot say whether a restart happened",
    "workspace": "pre-Session-7; cannot say which directory it edited",
    "changed_path": "pre-Session-6; cannot say what it changed",
    "before": "pre-Session-8; an interrupted edit is UNPROVABLE, not replayable",
    "repeat": "pre-Session-9; repetition was not counted",
    "context": "pre-Session-9; conversation size was never measured",
    "duration_ms": "pre-Session-10; model latency was not recorded",
    "model": "pre-Session-10; cannot say which model answered",
    "tools": "pre-Session-10; cannot say which tools were offered",
}


# --------------------------------------------------------------------------
# Loading. Tolerant by design.
# --------------------------------------------------------------------------


@dataclass
class Trace:
    """One journal, read. `damaged` is part of the reading, not an error.

    A reader that raises on a damaged file is a reader you cannot use on the
    only file you ever urgently need to read. The journal's own `heal()` is
    the repair path for a run that will be RESUMED; this is the path for one
    that is being looked at, and looking must never modify.
    """

    run_id: str
    path: Path
    events: list[dict[str, Any]] = field(default_factory=list)
    damaged: int = 0

    @property
    def schema(self) -> int:
        """0 for a journal written before the version existed (D94)."""
        return int(self._first(RUN_STARTED).get("schema", 0) or 0)

    def _first(self, kind: str) -> dict[str, Any]:
        for event in self.events:
            if event.get("t") == kind:
                return event
        return {}

    def _last(self, kind: str) -> dict[str, Any]:
        for event in reversed(self.events):
            if event.get("t") == kind:
                return event
        return {}

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("t") == kind]


def load(run: str | Path, *, runs_dir: Path = RUNS_DIR) -> Trace:
    """Read one journal by run id or path. Never raises on content.

    Lines that do not parse are counted, not fatal: a crash can tear the last
    record, and the 131 records before it are still the only account of what
    happened to someone's files.
    """
    path = PATHS.resolve_artifact(run)
    if not path.suffix:
        path = runs_dir / f"{run}.jsonl"

    trace = Trace(run_id=path.stem, path=path)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return trace

    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            trace.damaged += 1
            continue
        if isinstance(event, dict):
            trace.events.append(event)
        else:
            trace.damaged += 1
    return trace


def segments(trace: Trace) -> list[Trace]:
    """One Trace per RUN recorded in this journal. Split on `run_started`.

    A journal is not always one run. `cli.py` writes one run per file, but
    `chat.py` writes one run per TURN into a single session journal (D56), so
    a ten-question conversation is ten runs in one file.

    D100: a reader that assumes the cli.py shape does not fail loudly on a
    chat journal - it reports turn 2 as a RESUME of turn 1, prints the first
    turn's task beside the last turn's token count, and is believed. The split
    is the fix, and it belongs here because which-runs-are-in-this-file is a
    reading, not something the writer should have to declare.

    `damaged` goes to the LAST segment: a torn line is the tail of the file,
    which is the run that was in progress when the process died.
    """
    out: list[Trace] = []
    current: list[dict[str, Any]] = []

    for event in trace.events:
        if event.get("t") == RUN_STARTED and current:
            out.append(Trace(run_id=trace.run_id, path=trace.path, events=current))
            current = []
        current.append(event)

    if current:
        out.append(Trace(run_id=trace.run_id, path=trace.path, events=current))
    if out:
        out[-1].damaged = trace.damaged
    return out


# --------------------------------------------------------------------------
# The row a machine reads (step 5). This is Session 11's table column set.
# --------------------------------------------------------------------------


def summary(trace: Trace) -> dict[str, Any]:
    """One run, as one flat JSON object.

    Designed as a TABLE COLUMN SET, not as a dump: every key here is a column
    Session 11's report will group by or average, and anything that cannot be
    a column (the messages, the file contents) is deliberately absent.

    D99: these numbers must agree with `LoopResult` for the same run. They are
    derived from different places - this from disk, that from RAM - and if
    they disagree one of them is lying. `tests/test_tracing.py` asserts the
    agreement rather than assuming it.

    None means NOT RECORDED. Zero means recorded as zero. A reader that
    collapses the two turns "this journal predates latency" into "the model
    answered instantly".
    """
    started = trace._first(RUN_STARTED)
    finishes = trace.of(RUN_FINISHED)
    last = finishes[-1] if finishes else {}
    results = trace.of(MODEL_RESULT)
    outcomes = trace.of(TOOL_OUTCOME)

    # Per-process totals do not add up to a run: `elapsed_s` in each
    # run_finished is that PROCESS's wall clock (D39 scoped seconds to the
    # process on purpose). The honest whole-run number is the span of the
    # timestamps, which includes the time the run spent dead.
    span = None
    if trace.events:
        first_at = trace.events[0].get("at")
        last_at = trace.events[-1].get("at")
        if isinstance(first_at, (int, float)) and isinstance(last_at, (int, float)):
            span = round(last_at - first_at, 3)

    latencies = [r.get("duration_ms") for r in results]
    measured = [ms for ms in latencies if isinstance(ms, (int, float))]
    totals = last.get("totals") or {}
    errors: dict[str, int] = {}
    for outcome in outcomes:
        code = outcome.get("error_code")
        if code:
            errors[code] = errors.get(code, 0) + 1

    return {
        "run_id": trace.run_id,
        "schema": trace.schema,
        "task": started.get("task"),
        "workspace": (str(PATHS.resolve_artifact(started['workspace']))
                      if started.get('workspace') else None),
        "budget": started.get("budget"),
        "models": sorted({r.get("model") for r in results if r.get("model")}) or None,
        "tools_offered": _tools_offered(results),
        # A resumed run has one run_finished per process. `replayed` on the
        # record does NOT mark one: it is true only when a pre-generated
        # answer was replayed for free, which is a narrower thing than a
        # restart and reads as false on most resumes.
        "processes": len(finishes),
        "resumed": len(finishes) > 1,
        "finished": bool(finishes),
        "stop_reason": last.get("stop_reason"),
        "stop_reasons": [f.get("stop_reason") for f in finishes] or None,
        "steps": last.get("steps", len(results)),
        "requests": len(results),
        "requests_unanswered": max(0, len(trace.of(MODEL_PENDING)) - len(results)),
        "tool_calls": totals.get("tool_calls", len(trace.of(TOOL_INTENT))),
        "prompt_tokens": totals.get("prompt_tokens"),
        "completion_tokens": totals.get("completion_tokens"),
        "total_tokens": totals.get("total_tokens"),
        "cache_hit_tokens": totals.get("cache_hit_tokens"),
        "cost_usd": last.get("cost_usd"),
        "wall_s": span,
        "model_ms": round(sum(measured), 1) if measured else None,
        "retries": len(trace.of(MODEL_RETRY)),
        "changed_paths": [
            o.get("changed_path") for o in outcomes if o.get("changed_path")
        ],
        "errors": errors or None,
        "interrupted": [
            o.get("interrupted") for o in outcomes if o.get("interrupted")
        ] or None,
        "repeats": last.get("repeats"),
        "context": last.get("context"),
        "damaged_lines": trace.damaged,
    }


def _tools_offered(results: Iterable[dict[str, Any]]) -> Any:
    """What the model could call, and whether that changed mid-run.

    A list of names when every request offered the same menu, and the list of
    distinct digests when it did not - because a run whose tool menu changed
    halfway is two experiments, and Session 12 has to be able to see that
    before it averages them.
    """
    seen = [r.get("tools") for r in results if isinstance(r.get("tools"), dict)]
    if not seen:
        return None
    digests = {entry.get("digest") for entry in seen}
    names = seen[0].get("names")
    return names if len(digests) == 1 else {"changed": sorted(d for d in digests if d)}


# --------------------------------------------------------------------------
# The timeline a person reads (step 4).
# --------------------------------------------------------------------------


def _clip(text: Any, limit: int = BOUNDED_CHARS) -> str:
    """One line, bounded. The whole of D92's write-nothing-new promise."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _offset(event: dict[str, Any], origin: float | None) -> str:
    at = event.get("at")
    if origin is None or not isinstance(at, (int, float)):
        return "     ?"
    return f"{at - origin:6.2f}"


def timeline(trace: Trace, *, full: bool = False) -> list[str]:
    """One line per event, in order, with elapsed offsets.

    `model_retry` and the Session 8 interrupted-call verdicts are given their
    own markers because they are the two things a person opens a trace to
    find, and `grep` is not a user interface.
    """
    origin = None
    for event in trace.events:
        if isinstance(event.get("at"), (int, float)):
            origin = event["at"]
            break

    form = "verbatim" if full else "bounded"
    lines = [
        f"# {trace.run_id}  ({len(trace.events)} events, schema {trace.schema}, "
        f"tool output {form})"
    ]
    if trace.damaged:
        lines.append(
            f"# {trace.damaged} unparseable line(s) skipped - a torn tail is what "
            "a crash during a write looks like"
        )

    for event in trace.events:
        kind = event.get("t", "?")
        step = event.get("step")
        where = f"{_offset(event, origin)}s  step {step if step else '-':>2}  "

        if kind == RUN_STARTED:
            lines.append(
                f"{where}START      {_clip(event.get('task'))}"
            )
            lines.append(f"{'':>14}            workspace={event.get('workspace') or '?'}")
            lines.append(f"{'':>14}            budget={event.get('budget') or '?'}")
        elif kind == MODEL_PENDING:
            lines.append(f"{where}-> POST    (a request that may be billed)")
        elif kind == MODEL_RESULT:
            usage = event.get("usage") or {}
            ms = event.get("duration_ms")
            lines.append(
                f"{where}<- {event.get('model') or 'model'} "
                f"{f'{ms:.0f}ms' if isinstance(ms, (int, float)) else '?ms'} "
                f"in={usage.get('prompt_tokens', '?')} "
                f"(hit={usage.get('prompt_cache_hit_tokens', 0)}) "
                f"out={usage.get('completion_tokens', '?')} "
                f"finish={event.get('finish_reason')}"
            )
            said = (event.get("message") or {}).get("content")
            if said:
                lines.append(f"{'':>14}            said: {_clip(said)}")
        elif kind == MODEL_RETRY:
            lines.append(
                f"{where}!! RETRY   attempt {event.get('attempt')} "
                f"kind={event.get('kind')} retrying={event.get('retrying')} "
                f"delay={event.get('delay_s')}s "
                f"({event.get('reason')}; retryable={event.get('retryable')} "
                f"safe={event.get('safe_to_retry')})"
            )
        elif kind == TOOL_INTENT:
            before = event.get("before")
            mark = f"  before={_clip(before, 20)}" if before else ""
            lines.append(
                f"{where}   tool    {event.get('name')}"
                f"({_clip(event.get('arguments'), 80)}){mark}"
            )
        elif kind == TOOL_OUTCOME:
            lines.append(f"{where}   {_outcome_line(event, full=full)}")
        elif kind == RUN_FINISHED:
            lines.append(
                f"{where}END        stop={event.get('stop_reason')} "
                f"steps={event.get('steps')} "
                f"cost={event.get('cost_usd')} "
                f"{_clip(json.dumps(event.get('totals') or {}), 100)}"
            )
        else:
            lines.append(f"{where}?? {kind}  {_clip(json.dumps(event), 100)}")

    return lines


def _outcome_line(event: dict[str, Any], *, full: bool) -> str:
    """One tool result, bounded unless asked otherwise.

    The bounded form still answers the three questions a trace is actually
    asked - which file, how big, did it change - which is the test of whether
    the bound is in the right place. It cannot answer "why did the model pick
    that `old` string", and that is what `--full` is for, against a journal
    you are not about to paste anywhere.
    """
    content = ((event.get("message") or {}).get("content")) or ""
    verdict = "ok " if event.get("ok") else f"ERR {event.get('error_code')}"
    changed = event.get("changed_path")
    marks = []
    if changed:
        marks.append(f"changed={changed}")
    if event.get("interrupted"):
        marks.append(f"INTERRUPTED={event['interrupted']} (not repeated)")
    if event.get("refused"):
        marks.append("refused by a limit, never ran")
    if event.get("repeat", 0) and event["repeat"] > 1:
        marks.append(f"repeat #{event['repeat']}")
    suffix = ("  " + "  ".join(marks)) if marks else ""

    if full:
        return f"{verdict}{suffix}\n{content}"
    return f"{verdict}{suffix}  [{len(content)} chars] {_clip(content)}"


# --------------------------------------------------------------------------
# D97: one entry point, two audiences.
# --------------------------------------------------------------------------

USAGE = (
    "usage: python -m agent.tracing <run_id> [--full] [--json] [--absences]\n"
    "       python -m agent.tracing --list [N]\n"
    "\n"
    "  (default)   a timeline a person reads, tool output bounded\n"
    "  --full      tool output verbatim. The journal's own content - do not\n"
    "              paste it anywhere you would not paste the workspace\n"
    "  --json      the one-row summary Session 11's suite consumes\n"
    "  --absences  what this journal's generation cannot tell you\n"
    "  --list      the most recent runs, newest last"
)


def absences(trace: Trace) -> list[str]:
    """What this journal cannot say, as opposed to what it says is zero."""
    present = {key for event in trace.events for key in event}
    for event in trace.events:
        if event.get("t") == RUN_FINISHED:
            present.update(event.get("totals") or {})
    missing = [
        f"  {name:<13} {why}" for name, why in ABSENCES.items() if name not in present
    ]
    if not missing:
        return [f"schema {trace.schema}: nothing missing (current is {SCHEMA})"]
    return [f"schema {trace.schema} (current is {SCHEMA}) does not record:", *missing]


def _list_runs(runs_dir: Path, limit: int) -> list[str]:
    paths = sorted(runs_dir.glob("*.jsonl"))[-limit:]
    rows = []
    for path in paths:
        row = summary(load(path))
        rows.append(
            f"{row['run_id']}  {str(row['stop_reason'] or 'unfinished'):<13} "
            f"steps={row['steps'] or 0:<3} tools={row['tool_calls'] or 0:<3} "
            f"{_clip(row['task'], 48)}"
        )
    return rows


def main(argv: list[str], *, runs_dir: Path = RUNS_DIR) -> int:
    flags = {a for a in argv[1:] if a.startswith("--")}
    positional = [a for a in argv[1:] if not a.startswith("--")]

    if "--list" in flags:
        limit = int(positional[0]) if positional and positional[0].isdigit() else 20
        print("\n".join(_list_runs(runs_dir, limit)))
        return 0

    if not positional:
        print(USAGE)
        return 2

    trace = load(positional[0], runs_dir=runs_dir)
    if not trace.events:
        print(f"error: no readable journal at {trace.path}")
        return 2

    if "--json" in flags:
        print(json.dumps(summary(trace), indent=2))
    elif "--absences" in flags:
        print("\n".join(absences(trace)))
    else:
        print("\n".join(timeline(trace, full="--full" in flags)))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
