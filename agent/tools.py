"""Tool schemas and implementations. (Session 2+)

list_files, read_file, replace_text. Each returns a structured result —
including structured errors the model can act on.
"""

from __future__ import annotations

import json
import os
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

EMPTY_DIRECTORY = "(empty directory)"

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

EMPTY_FILE = "(empty file)"


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


def list_files(path: str | os.PathLike[str], *, root: Path) -> ToolResult:
    """List one directory inside the workspace. (Step 9)

    D9: one level only. The model walks a tree by calling again, which keeps
    output naturally bounded and gives truncation (step 13) a single story.
    D8: dotfiles are included. A filtered listing is a silent omission, and a
    model cannot tell "not shown" from "not there" - the same failure as
    truncating without saying so.
    D10: plain lines, not JSON. Fewer tokens, nothing to parse, and Session 11
    can diff the string directly.

    Order is DIRECTORIES first, then files, each sorted by name. Sorting is
    explicit and byte-wise: os.scandir yields whatever order the filesystem
    feels like, and locale-aware collation would sort `awkward/` differently on
    macOS than on Linux CI. Session 11 diffs this output, so the order is part
    of the contract, not a detail.

    Symlinks are classified with follow_symlinks=False, so a link to a
    directory is listed as a plain entry rather than with a trailing `/`.
    Marking it as a directory would invite the model to list it, and policy
    would then reject it as `symlink` - the listing would be advertising a
    path the boundary refuses.

    An empty directory is a FACT, not an error: ok=True with a sentence
    saying so. content is never "" - a model reading an empty string cannot
    tell "this directory has no files" from "the tool broke", and those two
    call for opposite next moves. (Step 10)

    Tool errors - not_found, not_a_directory - are RETURNED, not raised.
    They are recoverable: the model reads the message and tries something else
    on its next turn, which is the whole reason ToolResult exists. (Step 11)

    Existence is tested only by letting os.scandir fail, which puts it AFTER
    resolve_in_workspace. That order is the tripwire from section E1: check
    existence first and a real file outside the workspace reports not_found
    while a fabricated one reports outside_workspace, which hands an attacker
    the filesystem-enumeration oracle that steps 3-7 exist to close. No test
    in test_policy.py would catch it, because the bug would live here.

    A PolicyViolation is converted here, never raised at the caller (step 12).
    From the loop's point of view a refusal and a missing directory look the
    same: a result with ok=False and a sentence to act on.

    Output is capped two ways (step 13). Truncation is always announced in
    `content` as well as flagged on the result: silence would let the model
    conclude the directory holds exactly MAX_ENTRIES files.
    """
    try:
        target = resolve_in_workspace(path, root=root)
    except PolicyViolation as violation:
        return _rejected(violation)

    dirs: list[str] = []
    files: list[str] = []
    try:
        # No exists() check before this. scandir failing IS the existence
        # test, and it runs after the boundary - see the docstring.
        with os.scandir(target) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    dirs.append(entry.name)
                else:
                    files.append(entry.name)
    except FileNotFoundError:
        return _tool_error(
            "not_found",
            f"{echo_path(path)}: no such directory in the workspace. List its "
            "parent to see what is actually there, e.g. list_files('.').",
        )
    except NotADirectoryError:
        return _tool_error(
            "not_a_directory",
            f"{echo_path(path)} is a file, not a directory. List the directory "
            "that contains it, or read it with read_file.",
        )
    except PermissionError:
        # Not in the session's table, but the third thing scandir raises in
        # practice. Returning it keeps the "no raw exception escapes" promise
        # from step 7 true on this side of the boundary too.
        return _tool_error(
            "permission_denied",
            f"{echo_path(path)}: permission denied. Try a different directory "
            "inside the workspace.",
        )

    # Sort the NAMES, then decorate. Appending "/" before sorting would let the
    # separator itself take part in the comparison.
    lines = [f"{name}/" for name in sorted(dirs)] + sorted(files)

    if not lines:
        # No footer here. "(0 entries)" says nothing the sentence does not,
        # and the blank line it sat above read like output that got cut off.
        return ToolResult(ok=True, content=EMPTY_DIRECTORY)

    total = len(lines)
    kept = lines[:MAX_ENTRIES]

    # The byte cap is applied to the NAMES, before the footer is composed, so
    # the notice itself can never be the thing that gets cut off. A truncation
    # notice that is itself truncated is worse than none: the model sees a
    # short listing and no reason to doubt it.
    budget = MAX_BYTES
    within_budget: list[str] = []
    for line in kept:
        budget -= len(line.encode()) + 1  # +1 for the newline
        if budget < 0:
            break
        within_budget.append(line)
    kept = within_budget

    shown = len(kept)
    if shown < total:
        # The count is honest about what was HIDDEN. A model told only
        # "truncated" cannot tell 5 missing entries from 4,800, and will
        # happily conclude the directory holds exactly `shown` files.
        # `total` is safe to reveal: this directory is already proven inside
        # the workspace, so the number leaks nothing the listing did not.
        footer = (
            f"({shown} of {total} entries shown; output truncated. "
            "List a subdirectory to narrow it down.)"
        )
    else:
        footer = f"({total} entries)" if total != 1 else "(1 entry)"

    content = "\n".join([*kept, "", footer])

    # truncated is for the CODE. The model only ever learns from the footer
    # above - which is why both are set in the same branch, from the same
    # comparison. Setting one without the other is the step 13 bug.
    return ToolResult(ok=True, content=content, truncated=shown < total)


def read_file(path: str | os.PathLike[str], *, root: Path) -> ToolResult:
    """Read one text file inside the workspace. (Session 3)

    policy.py is reused UNCHANGED. If this function had needed one new line
    there, the Session 2 boundary would have been in the wrong place: the rule
    is about paths, and a path does not care which tool asked.

    The decisions, with the reasoning that is easy to lose later:

    D12 - the size cap is enforced by reading MAX_READ_BYTES + 1 bytes, not by
          stat() first. stat-then-read is two syscalls with a gap in between,
          and the number validated in the first is not necessarily the number
          of bytes returned by the second. One read answers both "is it too
          big?" and "what is in it?".
    D13 - oversized files are TRUNCATED, not refused. A head of a 2MB file
          usually answers the question; a refusal never does. Same contract as
          list_files: truncated=True on the result, and a notice in `content`.
    D14 - undecodable bytes are refused as `binary_file` rather than repaired
          with errors="replace". Silent mojibake is worse than a clear refusal:
          the model cannot tell a replaced character from a real one, and in
          Session 6 it would propose an edit against text that never existed.
    D15 - binary is detected two ways, because one fixture defeats each:
          binary.bin has null bytes, latin1.txt has none and fails to decode.
    D16 - no line numbers. `content` is the file's bytes, decoded and nothing
          else, so Session 6's replace_text can match against exactly what the
          model was shown. Numbering would make every match a stripping bug.
    D17 - whole file only. No offset/limit yet; Session 9 can add it when
          context growth makes it necessary, and not before.
    D18 - an empty file returns "(empty file)", mirroring "(empty directory)".
          One wording per concept, across every tool.
    D19 - line endings pass through untouched. open() in binary mode, decode
          by hand: text mode would silently rewrite CRLF, and Session 6 would
          then write back a file that differs from the one it read.
    D20 - nothing. A file that addresses the model is returned verbatim. There
          is no sanitizing step here, because there is nothing to sanitize:
          policy.py never opens a file, so no sentence inside one can widen
          what the agent may touch. See docs/session-03.md E1.

    Error codes it can return, beyond the policy ones:
        not_found, not_a_file, permission_denied, binary_file, unreadable
    """
    try:
        target = resolve_in_workspace(path, root=root)
    except PolicyViolation as violation:
        return _rejected(violation)

    try:
        # Binary mode. Text mode would apply universal newline translation and
        # silently break D19, and it would raise UnicodeDecodeError from a
        # place where the partial-character case below cannot be told apart
        # from a genuinely binary file.
        with open(target, "rb") as handle:
            # +1 is the whole size check: if we got more than the cap, there
            # was more to get. No stat, no second syscall, no gap.
            raw = handle.read(MAX_READ_BYTES + 1)
    except FileNotFoundError:
        return _tool_error(
            "not_found",
            f"{echo_path(path)}: no such file in the workspace. List the "
            "directory first to see what is actually there.",
        )
    except IsADirectoryError:
        return _tool_error(
            "not_a_file",
            f"{echo_path(path)} is a directory, not a file. Use list_files to "
            "see what is inside it.",
        )
    except PermissionError:
        return _tool_error(
            "permission_denied",
            f"{echo_path(path)}: permission denied. The file exists but cannot "
            "be read; try a different file.",
        )
    except OSError as exc:
        # Everything the filesystem can still throw: ENAMETOOLONG survivors,
        # device files, broken mounts. Step 7's promise - no raw exception
        # escapes - has to hold on this side of the boundary too.
        return _tool_error(
            "unreadable",
            f"{echo_path(path)} could not be read ({type(exc).__name__}). "
            "Try a different file.",
        )

    if not raw:
        # D18. Same shape as an empty directory: a fact, not an error.
        return ToolResult(ok=True, content=EMPTY_FILE)

    truncated = len(raw) > MAX_READ_BYTES
    raw = raw[:MAX_READ_BYTES]

    # D15, first detector. A null byte is not valid in text and some binaries
    # decode as UTF-8 by accident, so this runs before the decode attempt.
    if b"\0" in raw:
        return _tool_error(
            "binary_file",
            f"{echo_path(path)} is a binary file (it contains null bytes) and "
            "cannot be shown as text.",
        )

    # D15, second detector.
    try:
        text = raw.decode()
    except UnicodeDecodeError as exc:
        # A cut at MAX_READ_BYTES can land in the middle of a multi-byte
        # character. That failure is OURS, not the file's, so it must not be
        # reported as binary - but only when it sits at the very end of what
        # we kept. Anything earlier is a genuinely undecodable file.
        if truncated and exc.start >= len(raw) - 3:
            text = raw[: exc.start].decode()
        else:
            return _tool_error(
                "binary_file",
                f"{echo_path(path)} is not valid UTF-8 text and cannot be "
                "shown. It may be binary or in another encoding.",
            )

    if not truncated:
        return ToolResult(ok=True, content=text)

    # The notice does not name the file's total size: that would need a stat,
    # and a second syscall could report a number that no longer matches what
    # was read. list_files can name its total because counting was free here
    # it is not, and an honest "larger" beats a precise-looking lie.
    notice = (
        f"\n\n(truncated: first {len(raw)} bytes shown, the file is larger. "
        "This is the beginning of the file, not all of it.)"
    )
    return ToolResult(ok=True, content=text + notice, truncated=True)


# --------------------------------------------------------------------------
# The menu the model sees, and the table the dispatcher uses.
#
# Pulled forward from Session 4 (steps 1-3) so the tools can be exercised from
# the CLI before the loop exists. Session 4 decides where this really belongs
# (D21/D22); until then it lives beside the functions it describes.
# --------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List the entries of one directory inside the workspace. "
                "Does not recurse. Paths are relative to the workspace root; "
                "absolute paths and paths outside the workspace are refused."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative directory, e.g. 'src' or '.'",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read one UTF-8 text file inside the workspace. Long files are "
                "truncated. Paths are relative to the workspace root; absolute "
                "paths and paths outside the workspace are refused."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file, e.g. 'src/app.py'",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
]

TOOLS = {
    "list_files": list_files,
    "read_file": read_file,
}


def dispatch(name: str, arguments: str, *, root: Path) -> ToolResult:
    """Run one tool call from the model. Returns a ToolResult for everything.

    The caller here is a language model, which emits plausible text rather than
    valid function calls. Four things arrive broken in practice, and each must
    come back as a result the model can act on - never as an exception:

        an unknown tool name        -> unknown_tool
        arguments that are not JSON -> invalid_arguments
        a missing or non-string path -> invalid_arguments
        an argument we never defined -> invalid_arguments

    The unknown_tool message names the tools that DO exist. A model told only
    "unknown tool" guesses again; a model shown the menu picks from it.
    """
    if name not in TOOLS:
        available = ", ".join(sorted(TOOLS))
        return _tool_error(
            "unknown_tool",
            f"No tool named {name!r}. Available tools: {available}.",
        )

    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError as exc:
        return _tool_error(
            "invalid_arguments",
            f"The arguments for {name} were not valid JSON ({exc.msg}). "
            'Send an object like {"path": "src"}.',
        )

    if not isinstance(parsed, dict):
        return _tool_error(
            "invalid_arguments",
            f"The arguments for {name} must be a JSON object, not "
            f"{type(parsed).__name__}.",
        )

    unexpected = set(parsed) - {"path"}
    if unexpected:
        return _tool_error(
            "invalid_arguments",
            f"{name} does not take {', '.join(sorted(unexpected))}. "
            "Its only argument is 'path'.",
        )

    path = parsed.get("path")
    if not isinstance(path, str):
        got = "nothing" if path is None else type(path).__name__
        return _tool_error(
            "invalid_arguments",
            f"{name} needs a string 'path' argument; got {got}. "
            'For example {"path": "src"}.',
        )

    return TOOLS[name](path, root=root)
