"""Would today's rule have stopped yesterday's runs? (Session 9, Phase 3 review)

    python scripts/repeat_audit.py [--failures N] [--calls N]

Reads every journal in `.runtime/journals/`, replays the calls it recorded through
`agent.repetition`, and prints the runs the new rule would have ended early.
No model, no network, no workspace: a journal records the tool name and the
argument string with every `tool_intent`, which is exactly what the rule
counts.

The number this exists to produce is in the last line:

    runs that ANSWERED and would have been stopped anyway

If that is not zero, the threshold is wrong and this script just saved a live
session. A loop detector is only worth having if it is silent on the runs that
worked - every stop it produces on a correct run is a capability removed from
the agent, paid for in a case nobody will remember to test again.

Session 8's `fault_matrix.py` is the same idea pointed at crashes: take the
artifacts a previous session already produced, and grade a new rule against
them instead of against your intuition.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.journal import JournalCorrupt, rebuild  # noqa: E402
from agent.repetition import (  # noqa: E402
    DEFAULT_MAX_REPEATED_CALLS,
    DEFAULT_MAX_REPEATED_FAILURES,
    Tally,
)

from agent.storage import JOURNALS_DIR

RUNS = JOURNALS_DIR


def _events(path: Path) -> list[dict]:
    """One journal's events, or an empty list if it cannot be read.

    A corrupt journal is not interesting here. The audit is a measurement of
    a RULE, and a file the rebuilder refuses is a measurement of the file.
    """
    import json

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            # The torn tail of a crash. Session 8 taught the journal to heal
            # this; here it is simply the end of the readable part.
            break
    return events


def audit(path: Path, *, max_failures: int, max_calls: int) -> dict | None:
    """Replay one journal through the rule. None when there is nothing to say."""
    events = _events(path)
    if not events:
        return None
    try:
        state = rebuild(events)
    except JournalCorrupt:
        return None
    if not state.calls_done:
        return None

    started = next((e for e in events if e.get("t") == "run_started"), {})
    finished = [e for e in events if e.get("t") == "run_finished"]
    # A journal can hold several run_finished records: one per process, when
    # the run was resumed. The LAST one is how the run actually ended.
    stop_reason = finished[-1].get("stop_reason") if finished else "crashed"

    root = Path(started.get("workspace") or ".")
    tally = Tally(root=root, max_failures=max_failures, max_successes=max_calls)
    stopped_at: int | None = None
    stopped_on: str | None = None

    for index, done in enumerate(state.calls_done, start=1):
        verdict = tally.record(done["name"], done.get("arguments"), ok=done["ok"])
        if verdict.stops and stopped_at is None:
            stopped_at = index
            stopped_on = verdict.describe

    return {
        "run": path.stem,
        "calls": len(state.calls_done),
        "stop_reason": stop_reason,
        "longest": tally.worst,
        "stopped_at": stopped_at,
        "stopped_on": stopped_on,
    }


def main(argv: list[str]) -> int:
    max_failures = DEFAULT_MAX_REPEATED_FAILURES
    max_calls = DEFAULT_MAX_REPEATED_CALLS
    for argument in argv[1:]:
        if argument.startswith("--failures="):
            max_failures = int(argument.split("=", 1)[1])
        elif argument.startswith("--calls="):
            max_calls = int(argument.split("=", 1)[1])

    rows = [
        row
        for row in (
            audit(p, max_failures=max_failures, max_calls=max_calls)
            for p in sorted(RUNS.glob("*.jsonl"))
        )
        if row is not None
    ]

    print("=" * 78)
    print(
        f"REPEAT AUDIT  -  {len(rows)} journals with tool calls, "
        f"rule: {max_failures} failures / {max_calls} successes"
    )
    print("=" * 78)
    print(f"{'run':24} {'calls':>5} {'longest':>7} {'ended':14} would stop at")
    print("-" * 78)

    would_stop = [row for row in rows if row["stopped_at"] is not None]
    for row in rows:
        at = (
            f"call {row['stopped_at']}: {row['stopped_on']}"
            if row["stopped_at"]
            else "-"
        )
        print(
            f"{row['run']:24} {row['calls']:>5} {row['longest']:>7} "
            f"{row['stop_reason']:14} {at}"
        )

    answered = [row for row in would_stop if row["stop_reason"] == "answer"]
    print()
    print(f"  {len(rows)} journals replayed, {sum(r['calls'] for r in rows)} calls")
    print(f"  {len(would_stop)} run(s) would have been stopped by the rule")
    print(f"  {len(answered)} of those ANSWERED and would have been stopped anyway")
    if answered:
        print()
        print("  The rule is wrong, or the threshold is. These runs worked:")
        for row in answered:
            print(f"    {row['run']}  {row['stopped_on']}")
        return 1
    print()
    print("  No correct run is banned by this rule at these thresholds.")
    print("  That is the whole claim, and it is measured rather than hoped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
