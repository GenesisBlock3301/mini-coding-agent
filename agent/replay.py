"""What a resumed run is allowed to re-run. (Session 8)

Pure policy, in the shape `retry.py` already uses: this module answers one
question and returns a value. The mechanism - answering the call, writing the
journal, counting the budget - stays in `loop.py`.

    plans_for(resume_state, root=workspace) -> {call_id: ReplayPlan}

The question it answers is Session 6's step 9, left open on purpose:

    A journal holding one `tool_intent` and no `tool_outcome` means the tool
    was entered and never answered. If the tool was `read_file`, re-running it
    costs a millisecond. If it was `replace_text`, the SAME journal describes
    two different worlds - the write never started, or the write completed and
    the process died before the outcome was recorded - and the file on disk is
    different in each.

`scripts/crash_demo.py --edit` has been printing that pair of worlds since
Session 6, with the comment that the journals are identical and the files are
not. D73 closes it the way that session's step 9 proposed: record a
fingerprint of the file WITH the intent, and the question becomes answerable
after the fact.

The rule is one line, and every special case is a value rather than a branch:

    the file is byte-identical to what the intent recorded  ->  replay
    it is not                                               ->  do not replay
    nothing was recorded                                    ->  do not replay

A missing file, an unreadable one and a path outside the workspace are all
fingerprints too (`absent`, `unreadable`, `outside`). They compare equal to
themselves, so "nothing changed, replay it" covers them without knowing what
they mean - and replaying them is free, because `replace_text` refuses each
one before it opens anything.

What this does NOT claim: that the agent is what changed the file. Another
process can edit a workspace while a run is dead. `changed` means the bytes
moved, which is the only thing a hash can tell you - and it is enough, because
both readings have the same answer: do not apply this edit again blind.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.journal import ResumeState
from agent.policy import PolicyViolation, resolve_in_workspace
from agent.tools import WRITES

# The three verdicts. Only REPLAY re-enters the tool.
REPLAY = "replay"
CHANGED = "changed"
UNKNOWN = "unknown"

# The error code on the tool message when a call cannot be repeated safely.
# Session 4's vocabulary: the model reads `ERROR <code>: <sentence>` and has to
# be able to act on it. "interrupted" is a fact about the run, not about the
# arguments - which is exactly why it must not be reported as `no_match`.
INTERRUPTED_TOOL_ERROR = "interrupted"

# Fingerprints that are not hashes. Each one means "there was nothing here to
# hash, for this reason" - and each compares equal to itself, which is all the
# rule needs.
ABSENT = "absent"
UNREADABLE = "unreadable"
OUTSIDE = "outside"
NO_PATH = "no-path"


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def fingerprint(target: Path) -> str:
    """The bytes of one file, as a comparable string.

    Read, not stat: size and mtime are both forgeable by an edit that happens
    to produce the same length, and mtime resolution on some filesystems is
    coarser than the window this is being used to close.
    """
    try:
        return digest(target.read_bytes())
    except FileNotFoundError:
        return ABSENT
    except OSError:
        return UNREADABLE


def _path_argument(arguments: Any) -> str | None:
    """The `path` a tool call names, or None if it did not name one."""
    if not isinstance(arguments, str):
        return None
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    path = parsed.get("path")
    return path if isinstance(path, str) else None


def before_fingerprint(name: str, arguments: Any, *, root: Path) -> str | None:
    """What the file looked like before this call ran, for the journal.

    None for a tool that cannot write: the field is then absent from the
    record, and its absence says "there was nothing to protect here" rather
    than "we forgot". A journal written before this session has no field on
    ANY intent, which is the UNKNOWN verdict - correctly, because it is.

    One extra read of one file per edit. The edit is about to read the same
    file anyway; this is the cheaper half of an operation that already has to
    open it.
    """
    if name not in WRITES:
        return None
    path = _path_argument(arguments)
    if path is None:
        # Unparseable or path-less arguments: dispatch will reject them
        # without touching the disk, so a replay is free. Recorded as a value
        # rather than left out, so it stays distinguishable from a read.
        return NO_PATH
    try:
        target = resolve_in_workspace(path, root=root)
    except PolicyViolation:
        return OUTSIDE
    return fingerprint(target)


@dataclass(frozen=True)
class ReplayPlan:
    """One in-flight call, and what to do about it."""

    verdict: str
    call_id: str
    name: str
    path: str | None
    before: str | None
    now: str | None

    @property
    def replays(self) -> bool:
        return self.verdict == REPLAY

    @property
    def where(self) -> str:
        return self.path or "the file it named"

    @property
    def message(self) -> str:
        """What the model is told instead of a tool result.

        Both sentences say what was NOT done and what to do next. A model told
        only "interrupted" asks again; a model told "read the file" reads it.
        """
        if self.verdict == CHANGED:
            return (
                f"This call was interrupted by a crash, and {self.where} has "
                "changed since it started: the edit is treated as applied and "
                "was NOT repeated. Nothing was written just now. Read the file "
                "if you need to confirm what it contains before continuing."
            )
        return (
            f"This call was interrupted by a crash and cannot be repeated "
            f"safely: nothing recorded what {self.where} contained before it "
            "ran, so there is no way to tell whether the edit already applied. "
            "Nothing was written. Read the file and decide what, if anything, "
            "still needs changing."
        )

    def line(self) -> str:
        """One line for a human reading a resume."""
        if self.verdict == REPLAY:
            why = "unchanged since the intent" if self.before else "cannot write"
            return f"{self.name} -> re-running ({why})"
        if self.verdict == CHANGED:
            return f"{self.name} on {self.where} -> already applied, not repeated"
        return f"{self.name} on {self.where} -> UNPROVABLE, refused and reported"


def decide(intent: dict[str, Any], *, root: Path) -> ReplayPlan:
    """One in-flight `tool_intent`, read against the workspace as it is now."""
    name = intent.get("name") or ""
    arguments = intent.get("arguments")
    call_id = intent.get("call_id") or ""
    path = _path_argument(arguments)
    before = intent.get("before")

    # A read that half-ran read half a file into a process that no longer
    # exists. There is nothing to be careful about.
    if name not in WRITES:
        return ReplayPlan(REPLAY, call_id, name, path, before, None)

    if not isinstance(before, str):
        return ReplayPlan(UNKNOWN, call_id, name, path, None, None)

    now = (
        before_fingerprint(name, arguments, root=root)
        if before not in (NO_PATH, OUTSIDE)
        else before
    )
    verdict = REPLAY if now == before else CHANGED
    return ReplayPlan(verdict, call_id, name, path, before, now)


def plans_for(state: ResumeState, *, root: Path) -> dict[str, ReplayPlan]:
    """Every in-flight call, decided. Keyed by call id, which is how the loop
    finds them again when the batch comes back round."""
    return {
        plan.call_id: plan
        for plan in (decide(intent, root=root) for intent in state.in_flight_intents)
    }
