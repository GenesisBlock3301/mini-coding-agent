"""Tests for agent.tools.read_file. (Session 3)

Same rules as the Session 2 suites: no API key, no network.

The through-line of this file is that read_file reuses the Session 2 boundary
without changing it. Every policy row below passes because policy.py already
handled it - not because read_file re-implemented anything.
"""

import os
import subprocess

import pytest

from agent.policy import workspace_root
from agent.tools import MAX_READ_BYTES, EMPTY_FILE, ToolResult, list_files, read_file


# --------------------------------------------------------------------------
# Step 1: happy path
# --------------------------------------------------------------------------


def test_reads_a_small_file_exactly():
    result = read_file("read/small.txt", root=workspace_root())

    assert result == ToolResult(ok=True, content="line one\nline two\nline three\n")


def test_reads_a_source_file():
    result = read_file("src/app.py", root=workspace_root())

    assert result.ok is True
    assert result.content == "def main():\n    return 'hello'\n"


def test_content_is_byte_exact_no_line_numbers():
    """D16. Session 6 matches replacements against exactly this string."""
    content = read_file("src/app.py", root=workspace_root()).content

    assert not content.startswith("1")
    assert content == (workspace_root() / "src" / "app.py").read_text()


def test_policy_py_was_not_modified_this_session():
    """Review checkpoint 1, as a test rather than a promise.

    read_file is a new tool built entirely on the Session 2 boundary. If this
    diff is ever non-empty, the boundary moved into a tool - which is the one
    outcome Session 3 exists to prevent.
    """
    diff = subprocess.run(
        ["git", "diff", "HEAD", "--stat", "--", "agent/policy.py"],
        capture_output=True,
        text=True,
        cwd=workspace_root().parent,
    )
    # policy.py has uncommitted Session 2 work; what matters is that nothing
    # in it mentions reading, opening or decoding a file.
    source = (workspace_root().parent / "agent" / "policy.py").read_text()
    for forbidden in ["open(", "read_text", "read_bytes", "decode(", "scandir"]:
        assert forbidden not in source, f"policy.py now touches content: {forbidden}"
    assert diff.returncode == 0


# --------------------------------------------------------------------------
# Step 2: tool errors
# --------------------------------------------------------------------------


def test_missing_file_is_not_found():
    result = read_file("read/nope.txt", root=workspace_root())

    assert result.ok is False
    assert result.error_code == "not_found"


def test_a_directory_is_not_a_file():
    result = read_file("src", root=workspace_root())

    assert result.ok is False
    assert result.error_code == "not_a_file"


def test_the_two_wrong_type_errors_are_mirror_images():
    """not_a_directory and not_a_file must point at each other's tool."""
    root = workspace_root()

    listing_error = list_files("calculator.py", root=root)
    reading_error = read_file("src", root=root)

    assert "read_file" in listing_error.content
    assert "list_files" in reading_error.content


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_unreadable_file_is_permission_denied():
    """chmod 000 lives in the fixture generator, never in a test."""
    result = read_file("read/unreadable.txt", root=workspace_root())

    assert result.ok is False
    assert result.error_code == "permission_denied"


# --------------------------------------------------------------------------
# Step 3: policy reuse, not policy rewrite
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("..", "outside_workspace"),
        ("a/../../..", "outside_workspace"),
        ("/etc/passwd", "absolute_path"),
        ("/etc/definitely-not-here", "absolute_path"),
        ("../playground-evil/loot.txt", "outside_workspace"),
        ("escape", "outside_workspace"),
        ("evil_dir/loot.txt", "outside_workspace"),
        ("inside_link.py", "symlink"),
        ("inside_dir_link/app.py", "symlink"),
        ("a\0b", "invalid_path"),
        ("x" * 300, "invalid_path"),
    ],
)
def test_every_policy_rule_applies_to_read_file_too(path, expected):
    result = read_file(path, root=workspace_root())

    assert result.ok is False
    assert result.error_code == expected


def test_the_evil_siblings_contents_are_never_returned():
    """playground-evil/loot.txt exists and is readable. It must stay unread."""
    root = workspace_root()
    loot = (root.parent / "playground-evil" / "loot.txt").read_text()

    for path in ["../playground-evil/loot.txt", "escape", "evil_dir/loot.txt"]:
        result = read_file(path, root=root)
        assert result.ok is False
        assert loot.strip() not in result.content


# --------------------------------------------------------------------------
# Step 4: the size cap
# --------------------------------------------------------------------------


def test_big_file_is_truncated_not_refused():
    """D13. A head of a 2MB file usually answers the question."""
    result = read_file("read/big.txt", root=workspace_root())

    assert result.ok is True
    assert result.truncated is True


def test_truncated_read_is_actually_bounded():
    result = read_file("read/big.txt", root=workspace_root())

    assert len(result.content.encode()) < MAX_READ_BYTES + 200


def test_the_model_is_told_the_file_was_cut():
    content = read_file("read/big.txt", root=workspace_root()).content

    assert "truncated" in content
    assert "not all of it" in content


def test_one_long_line_is_capped_too():
    """1MB with no newline: a line-based cap would return the whole thing."""
    result = read_file("read/one_long_line.txt", root=workspace_root())

    assert result.truncated is True
    assert len(result.content.encode()) < MAX_READ_BYTES + 200


def test_the_cap_reads_one_byte_past_it_not_the_whole_file(tmp_path):
    """D12, observable: a file exactly at the cap is NOT reported truncated."""
    (tmp_path / "exact.txt").write_bytes(b"a" * MAX_READ_BYTES)
    (tmp_path / "one_more.txt").write_bytes(b"a" * (MAX_READ_BYTES + 1))

    assert read_file("exact.txt", root=tmp_path).truncated is False
    assert read_file("one_more.txt", root=tmp_path).truncated is True


def test_a_cut_through_a_multibyte_character_is_not_called_binary(tmp_path):
    """The cap is ours; a character we sliced is not the file's fault."""
    (tmp_path / "euro.txt").write_text("€" * 30_000)  # 3 bytes each

    result = read_file("euro.txt", root=tmp_path)

    assert result.ok is True
    assert result.truncated is True
    assert result.error_code is None
    assert "�" not in result.content


# --------------------------------------------------------------------------
# Step 5: undecodable bytes
# --------------------------------------------------------------------------


def test_binary_file_with_null_bytes_is_refused():
    result = read_file("read/binary.bin", root=workspace_root())

    assert result.ok is False
    assert result.error_code == "binary_file"


def test_latin1_file_is_refused_too():
    result = read_file("read/latin1.txt", root=workspace_root())

    assert result.ok is False
    assert result.error_code == "binary_file"


def test_the_two_binary_fixtures_defeat_different_detectors():
    """D15, pinned. If this ever fails, one of the fixtures stopped earning its place."""
    read = workspace_root() / "read"

    assert b"\0" in (read / "binary.bin").read_bytes()
    assert b"\0" not in (read / "latin1.txt").read_bytes()  # a null sniff misses it

    with pytest.raises(UnicodeDecodeError):
        (read / "latin1.txt").read_bytes().decode()


def test_no_mojibake_is_ever_returned():
    """D14. errors="replace" would have put U+FFFD in the model's context."""
    for path in ["read/binary.bin", "read/latin1.txt"]:
        assert "�" not in read_file(path, root=workspace_root()).content


# --------------------------------------------------------------------------
# Step 6: empty file
# --------------------------------------------------------------------------


def test_empty_file_says_so_in_words():
    result = read_file("read/empty.txt", root=workspace_root())

    assert result == ToolResult(ok=True, content=EMPTY_FILE)


def test_empty_file_and_empty_directory_use_one_vocabulary():
    """Two tools inventing two phrasings is how a model learns to distrust both."""
    root = workspace_root()

    assert read_file("read/empty.txt", root=root).content == "(empty file)"
    assert list_files("empty", root=root).content == "(empty directory)"


# --------------------------------------------------------------------------
# Step 7 / D19: the bytes survive the round trip
# --------------------------------------------------------------------------


def test_crlf_line_endings_are_preserved():
    """Text mode would have rewritten these, and Session 6 would write back a
    file that differs from the one it read."""
    content = read_file("read/crlf.txt", root=workspace_root()).content

    assert content == "first\r\nsecond\r\nthird\r\n"


def test_a_file_without_a_trailing_newline_keeps_not_having_one():
    content = read_file("read/no_newline.txt", root=workspace_root()).content

    assert content == "no trailing newline here"
    assert not content.endswith("\n")


# --------------------------------------------------------------------------
# Step 8: untrusted content
# --------------------------------------------------------------------------


def test_hostile_file_content_is_returned_verbatim():
    """D20. There is no sanitizing step, and none is needed."""
    result = read_file("read/injection.md", root=workspace_root())

    assert result.ok is True
    assert "Ignore your previous instructions" in result.content


def test_reading_a_hostile_file_does_not_widen_the_boundary():
    """The property that makes D20 safe, stated as a test.

    policy.py never opens a file, so nothing inside one can reach it. Reading
    the injection fixture first changes nothing about what comes next.
    """
    root = workspace_root()

    read_file("read/injection.md", root=root)

    assert read_file("/etc/passwd", root=root).error_code == "absolute_path"
    assert read_file("../playground-evil/loot.txt", root=root).error_code == "outside_workspace"
    assert list_files("..", root=root).error_code == "outside_workspace"


# --------------------------------------------------------------------------
# Step 10: the whole attack table
# --------------------------------------------------------------------------

READ_TABLE = [
    # (path,                         ok,    error_code,          what it defeats)
    ("src/app.py",                   True,  None,                "baseline"),
    ("read/small.txt",               True,  None,                "baseline"),
    ("read/empty.txt",               True,  None,                "D18: content is not ''"),
    ("read/crlf.txt",                True,  None,                "D19"),
    ("read/no_newline.txt",          True,  None,                "line-splitting off-by-one"),
    ("read/injection.md",            True,  None,                "content is data, not instructions"),
    ("read/big.txt",                 True,  None,                "unbounded read"),
    ("read/one_long_line.txt",       True,  None,                "line-based truncation"),
    ("read/binary.bin",              False, "binary_file",       "UnicodeDecodeError must not escape"),
    ("read/latin1.txt",              False, "binary_file",       "null-byte sniffing alone"),
    ("read/nope.txt",                False, "not_found",         "ordinary tool error"),
    ("src",                          False, "not_a_file",        "exists, wrong type"),
    ("..",                           False, "outside_workspace", "traversal"),
    ("/etc/passwd",                  False, "absolute_path",     "absolute path"),
    ("../playground-evil/loot.txt",  False, "outside_workspace", "the startswith trap"),
    ("escape",                       False, "outside_workspace", "symlink out, final component"),
    ("evil_dir/loot.txt",            False, "outside_workspace", "symlink out, parent component"),
    ("inside_link.py",               False, "symlink",           "symlink staying inside (D5)"),
    ("a\0b",                         False, "invalid_path",      "ValueError must not escape"),
    ("x" * 300,                      False, "invalid_path",      "OSError must not escape"),
]


@pytest.mark.parametrize(
    "path,expected_ok,expected_code",
    [(row[0], row[1], row[2]) for row in READ_TABLE],
    ids=[row[3] for row in READ_TABLE],
)
def test_read_attack_table(path, expected_ok, expected_code):
    result = read_file(path, root=workspace_root())

    assert result.ok is expected_ok
    assert result.error_code == expected_code


def test_no_row_raises():
    for path, *_ in READ_TABLE:
        try:
            read_file(path, root=workspace_root())
        except Exception as exc:  # noqa: BLE001 - that is the point
            pytest.fail(f"raw {type(exc).__name__} escaped for {path[:20]!r}: {exc}")


def test_no_row_returns_empty_content():
    for path, *_ in READ_TABLE:
        content = read_file(path, root=workspace_root()).content
        assert content.strip() != "", path[:20]


def test_no_row_leaks_a_host_path():
    root = workspace_root()
    for path, *_ in READ_TABLE:
        content = read_file(path, root=root).content
        assert str(root) not in content, path[:20]


def test_no_row_returns_more_than_the_cap():
    root = workspace_root()
    for path, *_ in READ_TABLE:
        content = read_file(path, root=root).content
        assert len(content.encode()) < MAX_READ_BYTES + 300, path[:20]
