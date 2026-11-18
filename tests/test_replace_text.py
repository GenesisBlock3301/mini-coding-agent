"""Tests for agent.tools.replace_text and the schema-driven argument gate. (Session 6)

Same rules as every suite before it: no API key, no network.

Two through-lines:

  1. A TOOL THAT DOES NOT SUCCEED CHANGES NOTHING ON DISK. Almost every test
     below asserts bytes-after as well as the result, because this is the first
     session where a wrong answer costs the user's file rather than tokens.
  2. policy.py is untouched. The boundary does not care that this is a write.

These tests build their own workspace in tmp_path rather than using the
committed playground: a test that leaves an edit behind is a test that was
writing somewhere it did not intend.
"""

import os
import subprocess
import tempfile

import pytest

from agent.tools import (
    EDIT_TMP_PREFIX,
    MAX_READ_BYTES,
    dispatch,
    list_files,
    read_file,
    replace_text,
)


@pytest.fixture
def workspace(tmp_path):
    """A disposable workspace. Every test gets a fresh one."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a + b\n"
    )
    (tmp_path / "once.txt").write_text("alpha\nbeta\ngamma\n")
    (tmp_path / "thrice.txt").write_text("x = 1\nx = 1\nx = 1\n")
    (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02binary")
    return tmp_path


def read_bytes(workspace, name):
    return (workspace / name).read_bytes()


def litter(workspace):
    """Any temp file this tool left behind, anywhere in the workspace."""
    return [p.name for p in workspace.rglob(f"{EDIT_TMP_PREFIX}*")]


# --------------------------------------------------------------------------
# The milestone: zero, one, many
# --------------------------------------------------------------------------


def test_exactly_one_match_is_replaced(workspace):
    result = replace_text("once.txt", "beta", "BETA", root=workspace)

    assert result.ok is True
    assert read_bytes(workspace, "once.txt") == b"alpha\nBETA\ngamma\n"


def test_zero_matches_refuses_and_changes_nothing(workspace):
    before = read_bytes(workspace, "once.txt")

    result = replace_text("once.txt", "delta", "DELTA", root=workspace)

    assert result.ok is False
    assert result.error_code == "no_match"
    assert read_bytes(workspace, "once.txt") == before


def test_multiple_matches_refuses_and_names_the_count(workspace):
    before = read_bytes(workspace, "thrice.txt")

    result = replace_text("thrice.txt", "x = 1", "x = 2", root=workspace)

    assert result.ok is False
    assert result.error_code == "multiple_matches"
    assert "3" in result.content
    assert read_bytes(workspace, "thrice.txt") == before


def test_zero_and_many_give_DIFFERENT_instructions(workspace):
    """They are different mistakes. One sentence for both is one retry twice."""
    none = replace_text("once.txt", "delta", "D", root=workspace)
    many = replace_text("thrice.txt", "x = 1", "y", root=workspace)

    assert none.content != many.content
    assert "again" in none.content          # go and re-read
    assert "surrounding" in many.content    # widen the anchor


def test_the_failing_call_leaves_no_temp_file(workspace):
    """E1's second half: a refusal the user has to clean up is not free."""
    replace_text("once.txt", "delta", "DELTA", root=workspace)
    replace_text("thrice.txt", "x = 1", "x = 2", root=workspace)

    assert litter(workspace) == []


def test_a_refusal_does_not_even_touch_the_mtime(workspace):
    before = (workspace / "once.txt").stat().st_mtime_ns

    replace_text("once.txt", "nowhere", "x", root=workspace)

    assert (workspace / "once.txt").stat().st_mtime_ns == before


# --------------------------------------------------------------------------
# D45 - counting, and D48 - the degenerate inputs
# --------------------------------------------------------------------------


def test_overlapping_occurrences_count_as_one(workspace):
    """D45. 'aa' in 'aaa' is ONE non-overlapping match, and str.replace agrees.

    Count and apply must agree. That is the whole rule.
    """
    (workspace / "aaa.txt").write_text("aaa")

    result = replace_text("aaa.txt", "aa", "b", root=workspace)

    assert result.ok is True
    assert read_bytes(workspace, "aaa.txt") == b"ba"


def test_empty_old_is_rejected(workspace):
    before = read_bytes(workspace, "once.txt")

    result = replace_text("once.txt", "", "x", root=workspace)

    assert result.error_code == "invalid_arguments"
    assert read_bytes(workspace, "once.txt") == before


def test_empty_old_is_rejected_BEFORE_the_path_is_looked_at(workspace):
    """Session 2 step 3's ordering rule, carried forward.

    No rejection may depend on whether the target exists. A missing file still
    answers invalid_arguments, because the arguments were wrong either way.
    """
    result = replace_text("does/not/exist.txt", "", "x", root=workspace)

    assert result.error_code == "invalid_arguments"


def test_old_equal_to_new_is_rejected(workspace):
    """A write whose only effect is a changed mtime still changes the workspace."""
    before = (workspace / "once.txt").stat().st_mtime_ns

    result = replace_text("once.txt", "beta", "beta", root=workspace)

    assert result.error_code == "invalid_arguments"
    assert (workspace / "once.txt").stat().st_mtime_ns == before


def test_old_longer_than_the_file_is_simply_no_match(workspace):
    result = replace_text("once.txt", "z" * 500, "x", root=workspace)

    assert result.error_code == "no_match"


# --------------------------------------------------------------------------
# The file itself: missing, directory, binary, oversized, read-only
# --------------------------------------------------------------------------


def test_a_missing_file_is_not_created(workspace):
    """D49. replace_text edits; it does not create."""
    result = replace_text("new.txt", "a", "b", root=workspace)

    assert result.error_code == "not_found"
    assert not (workspace / "new.txt").exists()


def test_a_directory_is_not_a_file(workspace):
    result = replace_text("src", "a", "b", root=workspace)

    assert result.error_code == "not_a_file"


def test_a_binary_file_is_refused_unchanged(workspace):
    before = read_bytes(workspace, "binary.bin")

    result = replace_text("binary.bin", "binary", "text", root=workspace)

    assert result.error_code == "binary_file"
    assert read_bytes(workspace, "binary.bin") == before


def test_a_file_larger_than_the_read_cap_is_refused(workspace):
    """D46, the sneaky one.

    read_file showed the model the first 64KB. A match that is unique in what
    it saw may be ambiguous in the rest, and our count - run over the whole
    file - would disagree with the model's belief for a reason it cannot see.
    """
    big = "filler\n" * 200 + "needle\n"
    big += "x" * (MAX_READ_BYTES + 1 - len(big.encode()))
    (workspace / "big.txt").write_text(big)
    before = read_bytes(workspace, "big.txt")

    result = replace_text("big.txt", "needle", "found", root=workspace)

    assert result.error_code == "too_large_to_edit"
    assert read_bytes(workspace, "big.txt") == before


def test_a_file_exactly_at_the_cap_is_still_editable(workspace):
    """The boundary is >, not >=. Off by one here refuses a legal edit."""
    body = "needle" + "x" * (MAX_READ_BYTES - len("needle"))
    (workspace / "edge.txt").write_text(body)

    result = replace_text("edge.txt", "needle", "FOUND", root=workspace)

    assert result.ok is True


def test_a_read_only_file_is_refused_with_the_original_intact(workspace):
    """os.replace only needs a writable DIRECTORY.

    Without an explicit check, a file the user marked read-only would be
    replaced anyway - the rename does not care about the old file's mode.
    """
    target = workspace / "once.txt"
    before = target.read_bytes()
    target.chmod(0o444)
    try:
        result = replace_text("once.txt", "beta", "BETA", root=workspace)

        assert result.error_code == "permission_denied"
        assert target.read_bytes() == before
        assert litter(workspace) == []
    finally:
        target.chmod(0o644)


# --------------------------------------------------------------------------
# The Session 2 boundary, against a write this time
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, code",
    [
        ("../escape.txt", "outside_workspace"),
        ("/etc/hosts", "absolute_path"),
        ("src/../../escape.txt", "outside_workspace"),
        ("bad\0name.txt", "invalid_path"),
    ],
)
def test_the_boundary_does_not_care_that_this_is_a_write(workspace, path, code):
    result = replace_text(path, "a", "b", root=workspace)

    assert result.ok is False
    assert result.error_code == code


def test_a_symlink_is_refused(workspace):
    (workspace / "link.txt").symlink_to(workspace / "once.txt")
    before = read_bytes(workspace, "once.txt")

    result = replace_text("link.txt", "beta", "BETA", root=workspace)

    assert result.error_code == "symlink"
    assert read_bytes(workspace, "once.txt") == before


def test_policy_py_was_not_modified_this_session():
    """The review checkpoint, as a test.

    If a WRITE needed one new line in policy.py, Session 2's boundary was in
    the wrong place.
    """
    import shutil
    if not shutil.which("git"):
        pytest.skip("Git history is unavailable in a source-only image")
    diff = subprocess.run(
        ["git", "diff", "HEAD", "--", "agent/policy.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert diff.stdout == ""


# --------------------------------------------------------------------------
# D19 - bytes in, bytes out
# --------------------------------------------------------------------------


def test_crlf_line_endings_survive_an_edit(workspace):
    (workspace / "win.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")

    result = replace_text("win.txt", "two", "TWO", root=workspace)

    assert result.ok is True
    assert read_bytes(workspace, "win.txt") == b"one\r\nTWO\r\nthree\r\n"


def test_a_missing_trailing_newline_is_not_added(workspace):
    (workspace / "bare.txt").write_bytes(b"no newline here")

    replace_text("bare.txt", "no", "NO", root=workspace)

    assert read_bytes(workspace, "bare.txt") == b"NO newline here"


def test_a_byte_order_mark_is_preserved(workspace):
    (workspace / "bom.txt").write_bytes(b"\xef\xbb\xbfalpha beta\n")

    replace_text("bom.txt", "beta", "BETA", root=workspace)

    assert read_bytes(workspace, "bom.txt") == b"\xef\xbb\xbfalpha BETA\n"


def test_non_ascii_text_outside_the_match_is_untouched(workspace):
    (workspace / "utf8.txt").write_bytes("héllo — wörld\ntarget\n".encode())

    replace_text("utf8.txt", "target", "TARGET", root=workspace)

    assert read_bytes(workspace, "utf8.txt") == "héllo — wörld\nTARGET\n".encode()


def test_file_permissions_are_preserved_through_the_rename(workspace):
    """mkstemp creates 0600. Without copymode every edit narrows permissions."""
    target = workspace / "once.txt"
    target.chmod(0o640)

    replace_text("once.txt", "beta", "BETA", root=workspace)

    assert target.stat().st_mode & 0o777 == 0o640


# --------------------------------------------------------------------------
# What read_file showed is what replace_text matches (D16 + D19 together)
# --------------------------------------------------------------------------


def test_text_copied_verbatim_out_of_read_file_matches(workspace):
    """The two tools are one contract. If read_file ever added line numbers or
    normalised a newline, this is the test that would catch it."""
    shown = read_file("src/calc.py", root=workspace).content
    anchor = shown.split("def sub")[1]

    result = replace_text("src/calc.py", "def sub" + anchor, "", root=workspace)

    assert result.ok is True


# --------------------------------------------------------------------------
# Through dispatch: the schema-driven argument gate (D41)
# --------------------------------------------------------------------------


def test_the_old_tools_are_unchanged_by_the_new_validator(workspace):
    assert dispatch("list_files", '{"path": "."}', root=workspace).ok
    assert dispatch("read_file", '{"path": "once.txt"}', root=workspace).ok

    unknown = dispatch("list_files", '{"recursive": true}', root=workspace)
    assert unknown.error_code == "invalid_arguments"
    assert "recursive" in unknown.content
    assert "Its only argument is 'path'." in unknown.content


def test_replace_text_is_reachable_through_dispatch(workspace):
    result = dispatch(
        "replace_text",
        '{"path": "once.txt", "old": "beta", "new": "BETA"}',
        root=workspace,
    )

    assert result.ok is True
    assert read_bytes(workspace, "once.txt") == b"alpha\nBETA\ngamma\n"


def test_a_missing_argument_is_named(workspace):
    result = dispatch(
        "replace_text", '{"path": "once.txt", "old": "beta"}', root=workspace
    )

    assert result.error_code == "invalid_arguments"
    assert "'new'" in result.content


def test_a_wrong_typed_argument_is_named(workspace):
    result = dispatch(
        "replace_text",
        '{"path": "once.txt", "old": 7, "new": "x"}',
        root=workspace,
    )

    assert result.error_code == "invalid_arguments"
    assert "'old'" in result.content
    assert "int" in result.content


def test_an_invented_argument_is_named_and_the_real_ones_listed(workspace):
    result = dispatch(
        "replace_text",
        '{"path": "once.txt", "old": "a", "new": "b", "regex": true}',
        root=workspace,
    )

    assert result.error_code == "invalid_arguments"
    assert "regex" in result.content
    assert "'path', 'old', 'new'" in result.content


def test_invented_argument_wins_over_missing_argument(workspace):
    """Determinism, not preference. {"regex": true} is wrong in two ways; the
    model's actual mistake is that it invented something."""
    result = dispatch("replace_text", '{"regex": true}', root=workspace)

    assert "regex" in result.content


def test_the_unknown_tool_message_now_names_four_tools(workspace):
    result = dispatch("edit_file", "{}", root=workspace)

    assert result.error_code == "unknown_tool"
    for name in ("list_files", "list_folders", "read_file", "replace_text"):
        assert name in result.content


# --------------------------------------------------------------------------
# Step 8 - two edits to one file in one batch
# --------------------------------------------------------------------------


def test_two_edits_to_one_file_apply_in_order(workspace):
    """Nothing in loop.py changed for this. run_pending already runs calls in
    order and answers every id; the second edit is matched against the file as
    it stands AFTER the first."""
    first = dispatch(
        "replace_text",
        '{"path": "once.txt", "old": "alpha", "new": "ALPHA"}',
        root=workspace,
    )
    second = dispatch(
        "replace_text",
        '{"path": "once.txt", "old": "ALPHA\\nbeta", "new": "ALPHA\\nBETA"}',
        root=workspace,
    )

    assert first.ok and second.ok
    assert read_bytes(workspace, "once.txt") == b"ALPHA\nBETA\ngamma\n"


def test_the_second_edit_sees_the_first(workspace):
    """The mirror: an anchor built on the PRE-edit text no longer matches."""
    dispatch(
        "replace_text",
        '{"path": "once.txt", "old": "alpha", "new": "ALPHA"}',
        root=workspace,
    )
    stale = dispatch(
        "replace_text",
        '{"path": "once.txt", "old": "alpha\\nbeta", "new": "x"}',
        root=workspace,
    )

    assert stale.error_code == "no_match"


# --------------------------------------------------------------------------
# D52 - the result says what it changed
# --------------------------------------------------------------------------


def test_a_successful_edit_reports_the_path_it_changed(workspace):
    result = replace_text("src/calc.py", "a + b\n\n\ndef sub", "a + b\n\n\ndef SUB", root=workspace)

    assert result.changed_path == "src/calc.py"


def test_nothing_else_ever_sets_changed_path(workspace):
    """Only a tool that actually changed the workspace may set it."""
    assert replace_text("once.txt", "nope", "x", root=workspace).changed_path is None
    assert list_files(".", root=workspace).changed_path is None
    assert read_file("once.txt", root=workspace).changed_path is None


# --------------------------------------------------------------------------
# D42 - the write that cannot half-land
#
# These are the tests the session exists for. Everything above passes just as
# well with a naive open(target, "w") that truncates and rewrites; only a
# forced failure inside the write can tell the two designs apart.
# --------------------------------------------------------------------------


def test_a_crash_before_the_rename_leaves_the_original_intact(workspace, monkeypatch):
    """The window truncate-in-place leaves open, closed.

    Truncating and rewriting is two operations, and a crash between them
    leaves a file that is neither the old one nor the new one. Here the new
    bytes are fully written and the rename is then made to fail - the exact
    moment a naive implementation would have already destroyed the original.
    """
    before = read_bytes(workspace, "once.txt")

    def boom(*args, **kwargs):
        raise OSError("simulated crash between write and rename")

    monkeypatch.setattr(os, "replace", boom)

    result = replace_text("once.txt", "beta", "BETA", root=workspace)

    assert result.ok is False
    assert result.error_code == "write_failed"
    assert read_bytes(workspace, "once.txt") == before
    assert litter(workspace) == []


def test_a_disk_full_mid_write_leaves_the_original_intact(workspace, monkeypatch):
    """The temp-file shape gets this one for free: the failure happens in a
    file nothing is reading yet."""
    before = read_bytes(workspace, "once.txt")

    def exploding_fdopen(fd, *args, **kwargs):
        os.close(fd)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fdopen", exploding_fdopen)

    result = replace_text("once.txt", "beta", "BETA", root=workspace)

    assert result.error_code == "write_failed"
    assert read_bytes(workspace, "once.txt") == before
    assert litter(workspace) == []


def test_the_temp_file_is_created_beside_the_target_not_in_tmp(workspace):
    """os.replace is atomic only WITHIN one filesystem.

    A temp file in /tmp would make the rename a cross-device copy - which is
    not atomic, and on a different mount would fail outright. This asserts the
    directory the temp file is born in, because that is the part of D42 that
    silently stops being true if someone "tidies up" the tempfile call.
    """
    seen = {}
    real_mkstemp = tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        seen["dir"] = kwargs.get("dir")
        return real_mkstemp(*args, **kwargs)

    import agent.tools

    original = agent.tools.tempfile.mkstemp
    agent.tools.tempfile.mkstemp = recording_mkstemp
    try:
        replace_text("src/calc.py", "def add", "def ADD", root=workspace)
    finally:
        agent.tools.tempfile.mkstemp = original

    assert seen["dir"] == workspace / "src"


def test_the_new_bytes_on_disk_are_exactly_what_was_computed(workspace):
    """The ordinary path, asserted in bytes rather than decoded text.

    A test that compares decoded strings cannot see the bug D19 exists to
    prevent - decode("x\\r\\n") == decode("x\\n") is False, but a careless
    normalisation would still survive a line-by-line comparison.
    """
    (workspace / "mixed.txt").write_bytes(b"a\r\nTARGET\nb\r\n")

    replace_text("mixed.txt", "TARGET", "done", root=workspace)

    assert read_bytes(workspace, "mixed.txt") == b"a\r\ndone\nb\r\n"


# --------------------------------------------------------------------------
# The prompt went stale once. Make that a test failure rather than a habit.
#
# Between Session 5 and Session 6 TOOLS_SYSTEM_PROMPT still said "two tools:
# list_files and read_file" - list_folders had been added and replace_text was
# about to be. Nothing caught it, because a stale prompt breaks nothing: the
# model is handed the real list in the `tools` array and the agent keeps
# working, just worse. A prompt that CONTRADICTS the tool list is the hardest
# kind of bug to see, so it gets the same treatment as _check_registries().
# --------------------------------------------------------------------------


def test_the_system_prompt_names_every_tool():
    from agent.cli import TOOLS_SYSTEM_PROMPT
    from agent.tools import TOOLS

    missing = [name for name in TOOLS if name not in TOOLS_SYSTEM_PROMPT]

    assert missing == [], f"the --tools prompt does not mention {missing}"


def test_the_system_prompt_counts_the_tools_correctly():
    """The word, not just the names. "two tools: ..." followed by four names
    is the exact shape the stale prompt had."""
    from agent.cli import TOOLS_SYSTEM_PROMPT
    from agent.tools import TOOLS

    words = {
        1: "one", 2: "two", 3: "three", 4: "four",
        5: "five", 6: "six", 7: "seven", 8: "eight",
    }
    expected = words[len(TOOLS)]

    assert f"{expected} tools" in TOOLS_SYSTEM_PROMPT, (
        f"the prompt should say '{expected} tools'; there are {len(TOOLS)}"
    )


def test_the_prompt_does_not_restate_what_the_schema_already_says():
    """D41's drift problem, one layer up.

    Per-tool rules live in the tool's schema description, where they travel
    with the tool. If a rule is written in both places, one of them will
    eventually be wrong - and the model will have been told both.
    """
    from agent.cli import TOOLS_SYSTEM_PROMPT

    assert "exactly once" not in TOOLS_SYSTEM_PROMPT
    assert "cannot create" not in TOOLS_SYSTEM_PROMPT
