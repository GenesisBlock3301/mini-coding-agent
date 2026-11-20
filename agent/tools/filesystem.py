"""Workspace directory and file tools."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from agent.policy import PolicyViolation, echo_path, resolve_in_workspace
from .common import (
    EMPTY_DIRECTORY,
    EMPTY_FILE,
    MAX_BYTES,
    MAX_CREATE_BYTES,
    MAX_ENTRIES,
    MAX_READ_BYTES,
    NO_SUBDIRECTORIES,
    SYMLINK_MARK,
    SYMLINK_NOTE,
    ToolResult,
    _decode_or_binary,
    _fit_to_bytes,
    _rejected,
    _tool_error,
)

def _scan(
    target: Path, path: str | os.PathLike[str]
) -> tuple[list[str], list[str]] | ToolResult:
    """One directory, split into (directories, files). Errors are RETURNED.

    Shared by list_files and list_folders so the three ways scandir fails are
    classified in exactly one place. Two tools with two copies of this would
    drift the first time one of them learned a new error.

    is_dir(follow_symlinks=False) is the load-bearing argument: a symlink
    pointing at a directory counts as a FILE here. Classifying it as a
    directory would invite a model to list it, and policy would then refuse
    it as `symlink` - a listing that advertises a path the boundary rejects.

    Session 6 revisit. That rule was only half the problem. Counting a
    symlink as a file kept the model from LISTING it and then invited it to
    READ it instead - the same wasted call, one tool along, and the same
    `symlink` refusal at the end of it. Worse, "give me the files here"
    truthfully answered with a directory in the list, because nothing in the
    listing said otherwise.

    Symlinks now carry a SYMLINK_MARK suffix. Every path through a symlink is
    refused by policy (D5), so the honest thing is to show them - a listing
    that silently omitted them would stop describing the directory - while
    saying plainly that they are not usable. is_symlink() inspects the final
    component only and never follows it, so this costs nothing and crosses no
    boundary.
    """
    dirs: list[str] = []
    files: list[str] = []
    try:
        # No exists() check before this. scandir failing IS the existence
        # test, and it runs after the boundary - see list_files' docstring.
        with os.scandir(target) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    dirs.append(entry.name)
                elif entry.is_symlink():
                    # Never reaches `dirs`: is_dir(follow_symlinks=False) is
                    # False for every symlink, whatever it points at. So one
                    # mark covers links to files and links to directories
                    # alike - and the model does not need to know which,
                    # because both are refused.
                    files.append(entry.name + SYMLINK_MARK)
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
    return dirs, files


def _render(
    lines: list[str], *, empty: str, noun: str = "entries", one: str = "1 entry"
) -> ToolResult:
    """Cap a listing and footer it honestly. Shared so the caps cannot drift."""
    if not lines:
        # No footer here. "(0 entries)" says nothing the sentence does not,
        # and the blank line it sat above read like output that got cut off.
        return ToolResult(ok=True, content=empty)

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
            f"({shown} of {total} {noun} shown; output truncated. "
            "List a subdirectory to narrow it down.)"
        )
    else:
        footer = f"({total} {noun})" if total != 1 else f"({one})"

    # After the byte budget, deliberately: the note describes what is SHOWN,
    # and a note about entries that were truncated away would be a lie. It is
    # also outside the budget on purpose - cutting the explanation of the
    # marks while keeping the marks is the worst of both.
    if any(line.endswith(SYMLINK_MARK) for line in kept):
        footer = f"{footer}\n{SYMLINK_NOTE}"

    content = "\n".join([*kept, "", footer])

    # truncated is for the CODE. The model only ever learns from the footer
    # above - which is why both are set in the same branch, from the same
    # comparison. Setting one without the other is the step 13 bug.
    return ToolResult(ok=True, content=content, truncated=shown < total)


def list_folders(path: str | os.PathLike[str], *, root: Path) -> ToolResult:
    """The subdirectories of one directory, and nothing else.

    list_files already answers this - directories are the lines ending in "/"
    - so this tool buys no new capability. What it buys is TOKENS and
    attention: `playground/many` holds 5000 files and 0 subdirectories, and
    asking list_files about it returns 200 truncated filenames the model then
    has to read, reason about and ignore.

    The cost is a third schema on every request forever, and one more way for
    the model to pick wrong. That trade is only worth it where listings are
    large; on a small directory, prefer list_files and read the slashes.

    Symlinked directories are NOT included, for the same reason list_files
    does not mark them: policy refuses to follow them, so listing one here
    would advertise a path that cannot be used.
    """
    try:
        target = resolve_in_workspace(path, root=root)
    except PolicyViolation as violation:
        return _rejected(violation)

    scanned = _scan(target, path)
    if isinstance(scanned, ToolResult):
        return scanned
    dirs, _files = scanned

    # The trailing "/" is kept even though every line is a directory. One
    # convention across both tools beats a shorter string here: a model that
    # has learned "/ means directory" from list_files reads this correctly
    # without being told anything new.
    return _render(
        [f"{name}/" for name in sorted(dirs)],
        empty=NO_SUBDIRECTORIES,
        noun="directories",
        one="1 directory",
    )


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

    scanned = _scan(target, path)
    if isinstance(scanned, ToolResult):
        return scanned
    dirs, files = scanned

    # Sort the NAMES, then decorate. Appending "/" before sorting would let the
    # separator itself take part in the comparison.
    lines = [f"{name}/" for name in sorted(dirs)] + sorted(files)
    return _render(lines, empty=EMPTY_DIRECTORY)


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

    # D15. Both detectors live in _decode_or_binary; the cut-tail forgiveness
    # applies only when this read was actually truncated.
    decoded = _decode_or_binary(
        raw, path, verb="shown", forgive_cut_tail=truncated
    )
    if isinstance(decoded, ToolResult):
        return decoded
    text = decoded

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
# Session 6. replace_text - the first tool that changes anything.
#
# E1, the new invariant: A TOOL THAT DOES NOT SUCCEED CHANGES NOTHING ON DISK.
# "Nothing" means nothing the user can observe: not a truncated file, not a
# changed mtime on a file we decided not to edit, and not a stray temp file
# left behind in their source tree. A refusal that leaves litter is a refusal
# they have to clean up, and they will not trust the next one.
# --------------------------------------------------------------------------

# Named, not anonymous, and prefixed with a dot: if a crash ever does leave one
# behind, the user can see what made it and delete it without guessing.
EDIT_TMP_PREFIX = ".agent-edit-"


def _read_for_edit(target: Path, path: object) -> bytes | ToolResult:
    """Open a file for editing, or RETURN the refusal. (Session 6)

    Split out of replace_text for the same reason _decode_or_binary was: the
    body of that function is a sequence of decisions, and four exception
    handlers in the middle of it bury the one line that matters.

    The excepts mirror read_file's exactly, with the messages changed to say
    what an EDIT does about it. FileNotFoundError is D49: not created.
    """
    try:
        with open(target, "rb") as handle:
            # +1 is the whole size check, as in read_file (D12). One read
            # answers both "is it too big?" and "what is in it?".
            return handle.read(MAX_READ_BYTES + 1)
    except FileNotFoundError:
        return _tool_error(
            "not_found",
            f"{echo_path(path)}: no such file in the workspace. replace_text "
            "edits existing files only; list the directory to see what is "
            "actually there.",
        )
    except IsADirectoryError:
        return _tool_error(
            "not_a_file",
            f"{echo_path(path)} is a directory, not a file.",
        )
    except PermissionError:
        return _tool_error(
            "permission_denied",
            f"{echo_path(path)}: permission denied. The file exists but "
            "cannot be read, so it cannot be edited.",
        )
    except OSError as exc:
        return _tool_error(
            "unreadable",
            f"{echo_path(path)} could not be read ({type(exc).__name__}). "
            "The file was not changed.",
        )


def _sync_directory(directory: Path) -> None:
    """fsync a directory entry, best effort. (D42)

    The rename is VISIBLE as soon as os.replace returns; it is DURABLE only
    once the directory holding it is synced. Best effort because a filesystem
    that refuses to sync a directory is not a reason to report a failed edit
    that in fact succeeded - the bound being claimed is "never half-written",
    not "survives any power cut on any filesystem".
    """
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _write_atomically(target: Path, data: bytes, path: object) -> ToolResult | None:
    """Replace `target`'s contents with `data`, all or nothing. Returns a
    ToolResult only on FAILURE, so the caller reads as "if failure: return it".

    D42. Truncate-in-place is two operations with a window between them, and
    a crash in that window leaves a file that is neither the old one nor the
    new one. Write-then-rename closes that window because os.replace is
    atomic - but ONLY WITHIN ONE FILESYSTEM, which is why the temp file is
    created in the target's own directory and never in /tmp.
    """
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=EDIT_TMP_PREFIX, dir=target.parent)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        # mkstemp creates 0600. Without this, every edited file would come
        # back with its permissions quietly narrowed - a change to the
        # workspace that nobody asked for and no diff would show.
        shutil.copymode(target, tmp_path)

        os.replace(tmp_path, target)
        tmp_path = None  # it is the real file now; the cleanup must not run

        _sync_directory(target.parent)
        return None
    except OSError as exc:
        return _tool_error(
            "write_failed",
            f"{echo_path(path)} could not be written ({type(exc).__name__}). "
            "The file is unchanged.",
        )
    finally:
        # E1's second half. A refusal - or a disk that filled up mid-write -
        # must not leave a .agent-edit-* file sitting in the user's source
        # tree. Runs on every path out except the successful one, where
        # tmp_path has been set to None.
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def replace_text(
    path: str | os.PathLike[str], old: str, new: str, *, root: Path
) -> ToolResult:
    """Replace exactly one occurrence of `old` with `new` in one workspace file.

    Zero matches and two matches are both refusals, and in both cases the file
    on disk is byte-identical afterwards. That is the session's milestone, and
    it is the smaller half of the work: everything else here exists so that the
    refusal is actually free.

    policy.py is reused UNCHANGED, exactly as read_file reuses it. If a WRITE
    had needed one new line there, Session 2's boundary would have been in the
    wrong place - the rule is about paths, and a path does not care which tool
    asked. (A symlink you merely read is a different proposition from one you
    write through; D5 refuses both, so the question does not arise here.)

    The decisions:

    D43 - `old` matches EXACT BYTES. No whitespace normalisation, no fuzzy
          threshold. read_file showed the model the file verbatim (D16, D19)
          precisely so an exact match is possible, and a fuzzy matcher would
          introduce the one failure mode strictly worse than a refusal: an
          edit applied to text the model never saw. It would also hide the
          measurement Session 11 exists to take.
    D45 - counting is `str.count`, i.e. NON-OVERLAPPING. old="aa" against
          "aaa" is ONE match, and this tool will happily apply it. The
          overlapping reading (two matches -> refuse) is defensible, but it
          would refuse edits that are in fact unambiguous to apply, and the
          non-overlapping count is the one that matches what `str.replace`
          will actually do. Count and apply must agree; that is the rule.
    D46 - a file larger than MAX_READ_BYTES is REFUSED, not edited. This is
          the sneaky one. read_file truncates at 64KB and says so, so the
          model has seen only the head of such a file. Its `old` may be
          unique in what it saw and ambiguous in the rest - and our count,
          run over the whole file, would disagree with the model's belief for
          a reason the model cannot see. Refusing is the only answer that
          keeps "exactly one match" meaning the same thing to both of us.
    D44 - staleness is handled by READING AT WRITE TIME. The bytes counted are
          the bytes replaced, from one read, with no stat() and no second
          open in between. The model does not carry a hash. See "the race"
          below for what this does NOT protect against.
    D48 - an empty `old` and `old == new` are both rejected as
          invalid_arguments, BEFORE the path is resolved or anything is
          opened. An empty `old` matches at every position in every file, so
          it has no unique match by construction; `old == new` is a write
          whose only effect is a changed mtime, which violates E1's second
          half. Same ordering rule as Session 2 step 3: a rejection that
          cannot depend on the target must not look at the target.
    D49 - replace_text NEVER creates a file. A missing file is `not_found`,
          and the model is told to list the directory. Creating on a typo'd
          path is how an agent quietly litters a repo with files nobody asked
          for, and a create is a different operation with a different failure
          table - it belongs in its own tool.
    D47 - success names the file and the sizes, not a diff. The model already
          knows what it asked for; what it cannot know is whether the write
          landed and how big the file is now.
    D14 - carried forward: a binary file is refused before any write.

    The race (D44, stated rather than solved): another process can write this
    file between our read and our rename. The window is microseconds instead
    of seconds, which is a narrowing, not a closing. There is no lock. A
    narrowed window you can describe beats a closed one you cannot - and the
    plan already warns that path checks are not a sandbox against a hostile
    process that shares the workspace.

    The durability bound (D42): after os.replace returns, a reader either sees
    the whole old file or the whole new one - never a mixture, and never a
    truncated file. That is the bound being claimed. The directory entry is
    fsynced too, so the rename survives a power loss; what is NOT claimed is
    anything about a filesystem that reorders renames, or about two files
    being replaced together. One file is atomic. A pair of files is two
    renames and nothing makes the pair atomic.

    Error codes, beyond the policy ones:
        not_found, not_a_file, permission_denied, unreadable, binary_file,
        too_large_to_edit, no_match, multiple_matches, write_failed
    """
    # ---- syntactic rejections, before the target is even resolved (D48) ----
    if old == "":
        return _tool_error(
            "invalid_arguments",
            "replace_text needs a non-empty 'old'. An empty string matches at "
            "every position in the file, so it can never match exactly once. "
            "Send the exact text you want replaced.",
        )

    if old == new:
        return _tool_error(
            "invalid_arguments",
            "replace_text was given the same text for 'old' and 'new', which "
            "would rewrite the file without changing it. If you meant to "
            "change something, send the new text in 'new'.",
        )

    try:
        target = resolve_in_workspace(path, root=root)
    except PolicyViolation as violation:
        return _rejected(violation)

    # ---- read: one open, +1 byte, exactly as read_file does (D12, D44) ----
    read = _read_for_edit(target, path)
    if isinstance(read, ToolResult):
        return read
    raw = read

    # ---- D46. Refuse what the model has only partly seen. ----
    if len(raw) > MAX_READ_BYTES:
        return _tool_error(
            "too_large_to_edit",
            f"{echo_path(path)} is larger than the {MAX_READ_BYTES}-byte read "
            "limit, so you have not been shown all of it. replace_text will "
            "not edit a file it cannot show you in full, because a match that "
            "is unique in the part you saw may not be unique in the rest.",
        )

    # ---- D14/D15. Binary is refused before anything is written. ----
    #
    # D19 lives here too: the bytes were read in binary mode and are decoded
    # by hand, the mirror image of read_file. Text mode would translate CRLF
    # on the way in AND on the way out, so the first Windows-authored file
    # this agent edited would come back with every line ending changed and a
    # diff nobody could review.
    #
    # forgive_cut_tail=False: D46 already refused anything that was cut, so a
    # decode failure here is the file's, not ours.
    decoded = _decode_or_binary(raw, path, verb="edited")
    if isinstance(decoded, ToolResult):
        return decoded
    text = decoded

    # ---- the milestone (D45) ----
    count = text.count(old)

    if count == 0:
        # Deliberately NOT the same sentence as the multi-match case. These are
        # different instructions: this one means "the text you are looking at
        # is not there". A model given one sentence for both retries the same
        # way twice.
        return _tool_error(
            "no_match",
            f"{echo_path(path)}: the text in 'old' does not appear in this "
            "file, so nothing was changed. Read the file again and copy the "
            "exact text, including indentation and line endings.",
        )

    if count > 1:
        return _tool_error(
            "multiple_matches",
            f"{echo_path(path)}: the text in 'old' appears {count} times, so "
            "the edit is ambiguous and nothing was changed. Include more of "
            "the surrounding lines in 'old' until it identifies one place.",
        )

    # ---- permission, checked before we build anything (E1) ----
    # os.replace only needs a writable DIRECTORY, so a read-only file would
    # otherwise be silently replaced. A file the user marked read-only is a
    # file they are saying not to edit; honour that, and say so before any
    # temp file exists to clean up.
    if not os.access(target, os.W_OK):
        return _tool_error(
            "permission_denied",
            f"{echo_path(path)}: the file is not writable, so it was not "
            "changed.",
        )

    data = text.replace(old, new, 1).encode()

    # ---- D42. The write that cannot half-land. ----
    #
    # Truncate-in-place is two operations with a window between them, and a
    # crash in that window leaves a file that is neither the old one nor the
    # new one. Write-then-rename closes that window because os.replace is
    # atomic - but ONLY WITHIN ONE FILESYSTEM, which is why the temp file is
    # created in the target's own directory and never in /tmp.
    failure = _write_atomically(target, data, path)
    if failure is not None:
        return failure

    # echo_path() is deliberately NOT used here. It quotes and elides for
    # prose, which is right for `content` and wrong for a field that code
    # consumes: the CLI would print 'src/app.py' with the quotes baked in, and
    # a truncated long path would name a file that does not exist. Derived
    # from the resolved target so it is normalised ("./a/../b" -> "b") rather
    # than whatever spelling the model happened to send.
    try:
        shown = str(target.relative_to(Path(root).resolve()))
    except ValueError:  # pragma: no cover - resolve_in_workspace guarantees it
        shown = str(path)

    return ToolResult(
        ok=True,
        content=(
            f"{echo_path(path)}: replaced 1 occurrence. "
            f"The file was {len(raw)} bytes and is now {len(data)} bytes."
        ),
        changed_path=shown,
    )


def write_file(path: str, content: str, *, root: Path) -> ToolResult:
    """Create one UTF-8 file atomically, without replacing an existing path."""
    if len(content) > MAX_CREATE_BYTES:
        return _tool_error(
            "too_large_to_create",
            f"write_file content exceeds the {MAX_CREATE_BYTES}-byte limit.",
        )
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        return _tool_error("invalid_arguments", "write_file needs valid UTF-8 text.")
    if b"\0" in data:
        return _tool_error("invalid_arguments", "write_file cannot create binary content containing null bytes.")
    if len(data) > MAX_CREATE_BYTES:
        return _tool_error(
            "too_large_to_create",
            f"write_file content exceeds the {MAX_CREATE_BYTES}-byte limit.",
        )
    try:
        target = resolve_in_workspace(path, root=root)
    except PolicyViolation as violation:
        return _rejected(violation)

    # This is only an early, helpful refusal. os.link below is the atomic
    # no-clobber check if another process creates the name in the meantime.
    if target.exists() or target.is_symlink():
        return _tool_error("already_exists", f"{echo_path(path)} already exists; write_file only creates new files.")
    if not target.parent.is_dir():
        return _tool_error("parent_not_found", f"{echo_path(path)} has no existing parent directory in the workspace.")

    tmp_path: str | None = None
    published = False
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=EDIT_TMP_PREFIX, dir=target.parent)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # A hard link publishes the fully written inode only when target is
        # absent. os.replace would overwrite a file created after our check.
        os.link(tmp_path, target)
        published = True
        _sync_directory(target.parent)
    except FileExistsError:
        return _tool_error("already_exists", f"{echo_path(path)} already exists; write_file did not replace it.")
    except FileNotFoundError:
        return _tool_error("parent_not_found", f"{echo_path(path)} has no existing parent directory in the workspace.")
    except PermissionError:
        return _tool_error("permission_denied", f"{echo_path(path)} could not be created because permission was denied.")
    except OSError as exc:
        return _tool_error("write_failed", f"{echo_path(path)} could not be created ({type(exc).__name__}).")
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    if not published:  # pragma: no cover - all failed paths return above
        raise AssertionError("write_file finished without publishing")
    shown = str(target.relative_to(Path(root).resolve()))
    return ToolResult(
        ok=True,
        content=f"{echo_path(path)}: created a new {len(data)}-byte file.",
        changed_path=shown,
    )


