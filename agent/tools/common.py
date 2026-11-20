"""Shared result types, constants, and validation helpers for tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent.policy import PolicyViolation, echo_path, resolve_in_workspace


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What every tool returns, on every outcome. (Step 8)

    Three outcomes - success, tool error, policy rejection - share one shape,
    so the agent loop never branches on type and never catches an exception
    raised by a tool. PolicyViolation stops here, in tools.py (step 12); it
    does not reach the loop.

    Two audiences, as in PolicyViolation:

    `content`    the ONLY field the model reads. On success it is the listing;
                 on failure it is the sentence explaining what to do instead.
                 One field rather than a separate `error` field, so the loop
                 never has to ask which field to show the model this turn.
                 Never empty - step 10 - because content="" reads to a model
                 as "the tool broke", not as "no files".

    `ok`         what the CODE branches on.
    `error_code` stable and machine-readable; None when ok. Session 11 evals
                 assert on it. Never reword it to improve a message - the
                 prose lives in `content`.
    `truncated`  set when output was cut. The flag is for the code; the model
                 only learns about it from the notice inside `content`
                 (step 13). Both, always, or the model concludes the directory
                 really does hold exactly 200 files.

    Frozen because results are recorded as traces in Session 10: a result the
    loop can edit after the fact is a trace that no longer describes what the
    model actually saw.
    """

    ok: bool
    content: str
    error_code: str | None = None
    truncated: bool = False
    # Session 6, D52. Set ONLY by a tool that changed the workspace, and only
    # after the change is visible on disk. The loop collects these so a run
    # that stops at a limit can still name what it already touched - the one
    # thing a user needs before they will run the agent a second time.
    # Workspace-relative, because it is shown to a human and host paths are
    # not theirs to see (policy.echo_path has the same rule).
    changed_path: str | None = None

EMPTY_DIRECTORY = "(empty directory)"
NO_SUBDIRECTORIES = "(no subdirectories here)"

# Step 13. Two caps, because either one alone leaks.
#
# MAX_ENTRIES alone: 200 names from `awkward/`, where one filename is 204
# characters, is still ~40KB of context.
# MAX_BYTES alone: a directory of 50,000 one-character names fits in 100KB
# and still buries the model in noise.
#
# Whichever trips first wins. These are the model's context budget expressed
# in two units, not a security boundary - nothing here depends on them.
MAX_ENTRIES = 200
MAX_BYTES = 8_000

# Session 3. A SEPARATE cap, deliberately. A listing and a file are different
# kinds of output: 8KB of names is a lot of names, 8KB of source is half a
# module. One constant serving both would be a coincidence, not a design.
MAX_READ_BYTES = 64_000
MAX_CREATE_BYTES = MAX_READ_BYTES
MAX_RUN_SECONDS = 30
MAX_RUN_OUTPUT_BYTES = 8_000

EMPTY_FILE = "(empty file)"

# `ls -F` has used this for symlinks for forty years, so it is the suffix a
# model has seen most. It is also not a legal character to end a POSIX
# filename with in practice, which keeps "is this a mark or a name?" from
# being a question anyone has to ask.
SYMLINK_MARK = "@"

# Shown only when a listing actually contains one. A standing sentence about
# symlinks in every listing of every directory is noise the model pays for on
# every step; a sentence that appears exactly when it is actionable is not.
SYMLINK_NOTE = (
    "Names ending @ are symlinks. They cannot be read, listed or edited - "
    "every path through one is refused. Use the real path instead."
)


def _tool_error(code: str, message: str) -> ToolResult:
    """A recoverable tool error. Shared by every tool. (Session 3, step 9)

    Two tools now build these, and Session 6 adds a third. One constructor
    means `ok=False` can never be forgotten and `content` can never be left
    empty - the two mistakes that look like success to the agent loop.
    """
    assert message.strip(), "a tool error with no message is indistinguishable from a crash"
    return ToolResult(ok=False, content=message, error_code=code)


def _fit_to_bytes(text: str, cap: int) -> tuple[str, bool]:
    """Cut `text` to at most `cap` UTF-8 bytes. Never splits a character.

    Returns (text, was_truncated). errors="ignore" drops the partial character
    the cut may have left behind - a cut is ours, not the file's, so it must
    not surface as a decoding problem.
    """
    raw = text.encode()
    if len(raw) <= cap:
        return text, False
    return raw[:cap].decode(errors="ignore"), True


def _decode_or_binary(
    raw: bytes,
    path: object,
    *,
    verb: str,
    forgive_cut_tail: bool = False,
) -> str | ToolResult:
    """Decode file bytes, or RETURN the binary_file refusal. (Session 6)

    Shared by read_file and replace_text. It was duplicated between them for
    exactly one commit, which is one too many: "what counts as binary" is a
    single judgement, and two copies of it can disagree. If they ever did,
    the agent would offer to edit a file it had refused to show - or refuse
    to show one it had just edited.

    D15 is why there are two detectors and not one: binary.bin has null bytes
    and decodes fine, latin1.txt has no null bytes and fails to decode. One
    fixture defeats each.

    `verb` tailors the sentence to the caller ("shown" / "edited") because
    the two tools suggest different next moves, while the error_code stays
    `binary_file` for both - the model reads the prose, the evals match the
    code.

    `forgive_cut_tail` is read_file's case alone. A cut at MAX_READ_BYTES can
    land mid-character, and that failure is OURS, not the file's - but only
    when it sits at the very end of what was kept. replace_text passes False
    because D46 already refused anything that was cut, so any decode failure
    it sees is genuinely the file's.
    """
    if b"\0" in raw:
        return _tool_error(
            "binary_file",
            f"{echo_path(path)} is a binary file (it contains null bytes) and "
            f"cannot be {verb} as text.",
        )

    try:
        return raw.decode()
    except UnicodeDecodeError as exc:
        if forgive_cut_tail and exc.start >= len(raw) - 3:
            return raw[: exc.start].decode()
        return _tool_error(
            "binary_file",
            f"{echo_path(path)} is not valid UTF-8 text and cannot be "
            f"{verb}. It may be binary or in another encoding.",
        )


def _rejected(violation: PolicyViolation) -> ToolResult:
    """Turn a PolicyViolation into a result. The ONLY place this happens. (Step 12)

    There will be three tools by Session 6. Three copies of this mapping means
    three chances for one of them to reword a code, swallow a message, or
    forget that `ok` is False - and the loop would keep running as if nothing
    had been refused.

    The violation's own fields carry straight through, unedited:
        code    -> error_code   the stable name tools.py and the evals match on
        message -> content      the sentence the model reads

    Rewriting the message here would be the bug worth naming: policy.py has
    already bounded it (echo_path) and already decided it leaks no host path.
    Anything added here is text that was never reviewed for either.
    """
    return ToolResult(ok=False, content=violation.message, error_code=violation.code)


