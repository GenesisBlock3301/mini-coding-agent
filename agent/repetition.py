"""Doing the same thing twice is evidence. Doing it N times is a stop. (Session 9)

Pure policy, in the shape `retry.py` and `replay.py` already use: this module
counts and returns a verdict. The mechanism - answering the call, refusing the
rest of the batch, ending the run - stays in `loop.py`.

    tally.record(name, arguments, ok=...) -> Repetition

Session 8 measured this and deliberately did not act on it (D75): a loop
detector built as a side effect of error handling is one nobody can configure.
`LoopResult.repeated_failures` was that measurement. This module is what it
was for.

The rule, in one line per outcome:

    the same call has now FAILED 3 times     ->  stop the run
    the same call has now SUCCEEDED 5 times  ->  stop the run
    anything else                            ->  carry on, and say so once

Why two thresholds rather than one (D78). The live run at the end of Session 8
read `stats.py`, was told its edit had already applied, and read `stats.py`
AGAIN before reporting. That is the single most correct thing an agent did in
this project, and the obvious formulation of today's rule - stop when a call
repeats - kills it. A repeated FAILURE is a model that has not understood the
error. A repeated SUCCESS is usually verification, so it gets a higher bar
rather than a free pass: re-reading one file forever is still a run going
nowhere, just more slowly and more politely.

What `the same call` means (D77). The tool name, plus the arguments with any
`path` resolved through `policy.resolve_in_workspace` - the SAME function the
tool itself will use a moment later. That is not a per-tool normalisation flag
(the smell Session 8 refused); it is the one canonicalisation the workspace
boundary already performs on every path that reaches a tool. It buys exactly
one thing, and the thing it buys is the one the doc predicted the model would
do within three steps of being told to try again:

    read_file {"path": "stats.py"}    ]
    read_file {"path": "./stats.py"}  ]  one call, counted twice

What it does NOT buy, and must not be mistaken for: sameness of INTENT.
`{"path": "a.py"}` and `{"path": "b.py"}` are two calls here even when the
model is equally stuck in both, and no counter in this repository can tell
"stuck" from "thorough". See docs/session-09.md E.

Counting is CUMULATIVE per signature (D80), not consecutive. Consecutive is
cheaper and forgiving, and it is blind to the exact shape this session exists
to catch - the agent that re-reads the same file between every edit, where a
different call always sits in between. Cumulative also catches the two-cycle
A/B/A/B for free, which is a cycle detector's job, without this being a cycle
detector: A's third occurrence is A's third occurrence however B is spaced.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from agent.policy import PolicyViolation, resolve_in_workspace

# The argument every tool names its target with. Not a guess: `tools.py`
# raises at IMPORT time if a writing tool takes anything else (D73), and the
# same convention is what `replay.py` fingerprints.
PATH_ARGUMENT = "path"

# D79. Failures are a misunderstanding and get a short rope; successes are
# usually verification and get a longer one. Both are configurable, and
# Session 12's comparison is the only honest way to pick the numbers - these
# are the starting point it will argue with, not a result.
DEFAULT_MAX_REPEATED_FAILURES = 3
DEFAULT_MAX_REPEATED_CALLS = 5

# One canonical call: (tool name, canonical arguments).
Signature = tuple[str, str]


def canonical_arguments(arguments: Any, *, root: Path) -> str:
    """The arguments as a comparable string, with `path` resolved.

    Unparseable arguments are returned as the raw string rather than being
    rejected. Two identical malformed argument strings ARE the same call made
    twice - that is `invalid_arguments` three times in a row, which is the
    most common stuck loop there is, and dropping it here would be dropping
    the case the session is about.

    A path that the workspace boundary refuses (absolute, outside, a symlink)
    keeps its raw spelling. The call never reaches a tool, so there is no
    canonical form to resolve it to - and the raw spelling still compares
    equal to itself, which is all the rule needs.
    """
    if not isinstance(arguments, str):
        arguments = ""
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return arguments.strip()
    if not isinstance(parsed, dict):
        return arguments.strip()

    canonical = dict(parsed)
    path = canonical.get(PATH_ARGUMENT)
    if isinstance(path, str):
        try:
            canonical[PATH_ARGUMENT] = str(resolve_in_workspace(path, root=root))
        except PolicyViolation:
            canonical[PATH_ARGUMENT] = path

    # sort_keys because a provider is free to emit the same object with its
    # keys in any order, and two orderings of one call are one call.
    return json.dumps(canonical, sort_keys=True, ensure_ascii=False)


def signature(name: str, arguments: Any, *, root: Path) -> Signature:
    """One call, canonicalised. The unit everything here counts."""
    return (name, canonical_arguments(arguments, root=root))


def is_repeat(previous: Iterable[Signature], candidate: Signature) -> bool:
    """Has this exact call been made before in this run?

    The predicate the session was specified in terms of. The loop uses
    `Tally.record`, which needs the count rather than the yes/no - but a rule
    you cannot state as one function is a rule you cannot test as one.
    """
    return candidate in set(previous)


@dataclass(frozen=True)
class Repetition:
    """One answered call, counted. The verdict, with its evidence attached."""

    signature: Signature
    name: str
    arguments: str
    ok: bool
    # How many times this signature has now been answered with THIS outcome.
    count: int
    # The threshold that applies to this outcome, or None when the rule is off.
    limit: int | None

    @property
    def stops(self) -> bool:
        """Has this call just spent the last of its rope?"""
        return self.limit is not None and self.count >= self.limit

    @property
    def warns(self) -> bool:
        """Is the NEXT one the last one? D82 - the model is told once."""
        return self.limit is not None and self.count == self.limit - 1

    @property
    def describe(self) -> str:
        """The call, as a human reading the report would name it.

        Built from the arguments the model actually sent, not the canonical
        form: a report that says `/private/tmp/ws-a81/stats.py` when the model
        said `./stats.py` is a report about a different program.
        """
        shown = self.arguments.strip()
        if len(shown) > 120:
            shown = shown[:117] + "..."
        return f"{self.name}({shown})" if shown else self.name

    @property
    def notice(self) -> str:
        """The sentence appended to the result of the second-to-last attempt.

        Session 4's `unknown_tool` reasoning, applied to a limit: a model told
        only "no" guesses again, and a model told WHICH call is about to be
        refused and WHY can do something else instead. It names the remedy
        (change the call or answer) because "stop repeating yourself" is not
        an instruction a model can act on.
        """
        outcome = "succeeded" if self.ok else "failed"
        return (
            f"\n\n[repetition: this exact call has now {outcome} {self.count} "
            f"time(s) in this run. One more identical call will not be answered "
            f"and the run will stop. Change the arguments or answer with what "
            f"you already know.]"
        )


@dataclass
class Tally:
    """Every call this run has answered, by canonical signature.

    Mutable, and owned by the run: this is a READING, like `Ledger`, not a
    rule like `Budget`. The two thresholds are copied in from the budget at
    construction so that a call site never has to pass them, and so that a
    run whose rule is off (`max_failures=None`) counts anyway. Counting
    without stopping is what Session 8 did, and it is still the behaviour
    Session 12 needs on the control arm.
    """

    root: Path
    max_failures: int | None = DEFAULT_MAX_REPEATED_FAILURES
    max_successes: int | None = DEFAULT_MAX_REPEATED_CALLS
    failures: Counter[Signature] = field(default_factory=Counter)
    successes: Counter[Signature] = field(default_factory=Counter)
    # Signatures the model has already been warned about, so D82's "once"
    # is enforced here rather than being a promise the loop keeps by hand.
    warned: set[Signature] = field(default_factory=set)

    def limit_for(self, ok: bool) -> int | None:
        return self.max_successes if ok else self.max_failures

    def count_for(self, sig: Signature, ok: bool) -> int:
        return self.successes[sig] if ok else self.failures[sig]

    def record(self, name: str, arguments: Any, *, ok: bool) -> Repetition:
        """Count one answered call and return what it means.

        Called AFTER dispatch, which is not an oversight: the outcome is half
        the signature's identity and nothing knows it beforehand. The cost is
        that the call which trips the rule has already run - it is answered in
        full, and the stop happens after the batch (D33). A rule that refused
        the call it was still measuring would be a rule that fires one call
        earlier than it can justify.
        """
        sig = signature(name, arguments, root=self.root)
        counter = self.successes if ok else self.failures
        counter[sig] += 1
        return Repetition(
            signature=sig,
            name=name,
            arguments=arguments if isinstance(arguments, str) else "",
            ok=ok,
            count=counter[sig],
            limit=self.limit_for(ok),
        )

    def should_warn(self, repetition: Repetition) -> bool:
        """True at most once per signature, and never on the stopping call."""
        if not repetition.warns or repetition.signature in self.warned:
            return False
        self.warned.add(repetition.signature)
        return True

    def seed(self, calls: Iterable[dict[str, Any]]) -> None:
        """Replay a journal's answered calls into the counts.

        A repetition counter that restarts on `--resume` is a counter you
        clear with `kill -9` - the identical argument D39 made for tokens and
        tool calls, and the identical answer. The journal already records
        every `tool_intent` with its name and arguments and every
        `tool_outcome` with its `ok`, so nothing new has to be written for
        this to survive a crash; it only has to be read back.
        """
        for call in calls:
            name = call.get("name") or ""
            if not name:
                continue
            sig = signature(name, call.get("arguments"), root=self.root)
            counter = self.successes if call.get("ok") else self.failures
            counter[sig] += 1

    @property
    def worst(self) -> int:
        """The longest run of one identical call, whatever its outcome.

        Phase 0's measurement, kept after the rule was built: a run that
        stopped for something else still has a number here, and a number that
        only exists when a rule fires cannot tell you the rule's threshold is
        wrong.
        """
        counts = list(self.failures.values()) + list(self.successes.values())
        return max(counts, default=0)

    @property
    def repeated_failures(self) -> int:
        """Failing calls that were a repeat of a call that had already failed.

        Session 8's number, preserved exactly: occurrences beyond the first,
        summed. What changed today is only that it now canonicalises paths
        before comparing, so `a.py` and `./a.py` stop hiding from it.
        """
        return sum(count - 1 for count in self.failures.values() if count > 1)

    @property
    def repeated_calls(self) -> int:
        """The same, for calls that succeeded. The number D78 argues about."""
        return sum(count - 1 for count in self.successes.values() if count > 1)
