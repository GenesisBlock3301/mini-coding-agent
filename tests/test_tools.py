"""Tests for agent.tools.

Like test_policy.py: no API key, no network. The listing format is a contract -
Session 11's evals diff this exact text - so these assert whole strings rather
than membership.
"""

import pytest

from agent.policy import workspace_root
from agent.tools import MAX_BYTES, MAX_ENTRIES, ToolResult, list_files


# --------------------------------------------------------------------------
# Step 9: list_files - happy path
# --------------------------------------------------------------------------


def test_exact_listing_of_a_fixture_directory():
    root = workspace_root()
    result = list_files("src", root=root)

    assert result == ToolResult(
        ok=True,
        content="app.py\nutils.py\n\n(2 entries)",
    )


def test_directories_come_first_then_files_each_alphabetical(tmp_path):
    """The whole ordering contract in one string.

    Built in tmp_path rather than playground/ so the expected text cannot drift
    when a fixture is added.
    """
    (tmp_path / "b_dir").mkdir()
    (tmp_path / "a_dir").mkdir()
    (tmp_path / "z.py").touch()
    (tmp_path / "a.py").touch()
    (tmp_path / ".dotfile").touch()

    result = list_files(".", root=tmp_path)

    assert result.content == "a_dir/\nb_dir/\n.dotfile\na.py\nz.py\n\n(5 entries)"


def test_dotfiles_are_included():
    """D8. The awkward/ fixture exists to force this."""
    result = list_files("awkward", root=workspace_root())
    assert ".hidden" in result.content


def test_awkward_names_do_not_break_the_listing():
    """Spaces, a leading dash, unicode and a 204-char name, in byte order."""
    result = list_files("awkward", root=workspace_root())
    names = result.content.splitlines()

    assert names[0] == "-leading-dash.txt"
    assert names[1] == ".hidden"
    assert names[2] == "has space.txt"
    assert names[3] == "no_extension"
    assert names[4] == "unicode_éü中文.txt"
    assert names[5] == "x" * 200 + ".txt"
    assert names[-1] == "(6 entries)"


def test_sort_order_does_not_depend_on_the_filesystem(tmp_path):
    """Created in one order, listed in another."""
    for name in ["zebra.py", "Apple.py", "mango.py", "_under.py"]:
        (tmp_path / name).touch()

    assert list_files(".", root=tmp_path).content.splitlines()[:4] == [
        "Apple.py",
        "_under.py",
        "mango.py",
        "zebra.py",
    ]


def test_a_symlinked_directory_is_not_marked_as_one(tmp_path):
    """inside_dir_link/ would be a listing that advertises a rejected path.

    policy.py refuses to traverse any symlink, so marking a link with a
    trailing `/` would invite the model to list something it cannot have.
    """
    (tmp_path / "real_dir").mkdir()
    (tmp_path / "link_to_dir").symlink_to(tmp_path / "real_dir")

    lines = list_files(".", root=tmp_path).content.splitlines()

    assert "real_dir/" in lines
    assert "link_to_dir" in lines
    assert "link_to_dir/" not in lines


def test_the_root_itself_can_be_listed():
    result = list_files(".", root=workspace_root())
    assert result.ok
    assert "src/" in result.content
    assert ".gitkeep" in result.content


def test_result_shape_on_success():
    result = list_files("src", root=workspace_root())
    assert result.ok is True
    assert result.error_code is None
    assert result.truncated is False


def test_entry_count_is_singular_for_one_entry(tmp_path):
    (tmp_path / "only.py").touch()
    assert list_files(".", root=tmp_path).content == "only.py\n\n(1 entry)"


# --------------------------------------------------------------------------
# Step 10: empty directories
# --------------------------------------------------------------------------


def test_empty_directory_says_so_in_words():
    """D: an empty directory is a fact, not an error."""
    result = list_files("empty", root=workspace_root())

    assert result == ToolResult(ok=True, content="(empty directory)")


def test_empty_listing_content_is_never_blank(tmp_path):
    """The actual failure being prevented.

    content="" reads to a model as "the tool broke" rather than "no files",
    and those two call for opposite next moves.
    """
    (tmp_path / "hollow").mkdir()

    content = list_files("hollow", root=tmp_path).content

    assert content.strip() != ""
    assert "empty" in content.lower()


def test_empty_directory_has_no_entry_count(tmp_path):
    """"(0 entries)" adds nothing the sentence does not already say."""
    (tmp_path / "hollow").mkdir()
    assert "entries" not in list_files("hollow", root=tmp_path).content


def test_a_directory_holding_only_a_dotfile_is_not_empty(tmp_path):
    """D8 and step 10 meeting: hidden entries must not fake emptiness."""
    (tmp_path / "dotty").mkdir()
    (tmp_path / "dotty" / ".hidden").touch()

    assert list_files("dotty", root=tmp_path).content == ".hidden\n\n(1 entry)"


# --------------------------------------------------------------------------
# Step 11: tool errors
# --------------------------------------------------------------------------


def test_missing_directory_returns_not_found():
    result = list_files("nope", root=workspace_root())

    assert result.ok is False
    assert result.error_code == "not_found"


def test_a_real_file_returns_not_a_directory():
    result = list_files("calculator.py", root=workspace_root())

    assert result.ok is False
    assert result.error_code == "not_a_directory"


def test_tool_errors_return_rather_than_raise():
    """The point of step 11. A raw OSError would crash the agent loop."""
    for path in ["nope", "calculator.py", "src/app.py", "a/b/c/d"]:
        assert list_files(path, root=workspace_root()).ok is False


def test_not_found_message_tells_the_model_what_to_do_next():
    """These are recoverable errors. The message is the recovery path."""
    content = list_files("nope", root=workspace_root()).content

    assert "nope" in content
    assert "list_files" in content


def test_not_a_directory_message_names_the_actual_problem():
    content = list_files("calculator.py", root=workspace_root()).content

    assert "calculator.py" in content
    assert "not a directory" in content.lower()


def test_error_messages_do_not_leak_host_paths():
    """Same rule as policy.py: the model sees its own path, never the resolved one."""
    root = workspace_root()

    for path in ["nope", "calculator.py"]:
        assert str(root) not in list_files(path, root=root).content


def test_happy_path_still_works_after_step_11():
    assert list_files("src", root=workspace_root()).ok is True


# --------------------------------------------------------------------------
# Step 12: policy violations become results


def test_existence_is_checked_after_the_boundary_not_before():
    """Section E1's tripwire, tested in tools.py where the bug would live.

    A real file outside the workspace and a fabricated one must give the same
    answer. If someone adds an exists() check before resolve_in_workspace, the
    real one starts reporting not_found and the enumeration oracle is back -
    and no test in test_policy.py would notice.
    """
    root = workspace_root()

    real = list_files("../playground-evil", root=root)
    fake = list_files("../zzz-no-such-sibling-9a7f", root=root)

    assert real.error_code == fake.error_code
    assert real.error_code != "not_found"


def test_long_path_is_truncated_in_tool_errors():
    """echo_path is shared with policy.py so truncation is identical on both sides."""
    content = list_files("q" * 5000, root=workspace_root()).content

    assert len(content) < 250
    assert "5000 chars" in content


@pytest.mark.parametrize(
    "path,expected",
    [
        ("..", "outside_workspace"),
        ("a/../../..", "outside_workspace"),
        ("../playground-evil", "outside_workspace"),
        ("/etc/passwd", "absolute_path"),
        ("/etc/definitely-not-here", "absolute_path"),
        ("escape", "outside_workspace"),
        ("evil_dir/loot.txt", "outside_workspace"),
        ("inside_link.py", "symlink"),
        ("inside_dir_link/app.py", "symlink"),
        ("a\0b", "invalid_path"),
        ("x" * 300, "invalid_path"),
    ],
)
def test_every_policy_violation_returns_instead_of_raising(path, expected):
    """Steps 3-7, now seen from the loop's side of the boundary."""
    result = list_files(path, root=workspace_root())

    assert result.ok is False
    assert result.error_code == expected


def test_nothing_raises_out_of_list_files():
    """The contract the agent loop depends on in Session 4."""
    paths = ["..", "/etc/passwd", "escape", "a\0b", "x" * 5000, "nope",
             "calculator.py", "src", ".", "", "many"]

    for path in paths:
        try:
            list_files(path, root=workspace_root())
        except Exception as exc:  # noqa: BLE001 - that is the point
            pytest.fail(f"raw {type(exc).__name__} escaped for {path[:20]!r}: {exc}")


def test_the_violation_message_is_passed_through_unedited():
    """tools.py must not reword what policy.py already bounded and reviewed."""
    from agent.policy import PolicyViolation, resolve_in_workspace

    root = workspace_root()
    try:
        resolve_in_workspace("..", root=root)
    except PolicyViolation as violation:
        expected = violation.message

    assert list_files("..", root=root).content == expected


def test_a_rejection_is_not_reported_as_a_tool_error():
    """outside_workspace and not_found must stay distinguishable to the model."""
    root = workspace_root()

    assert list_files("..", root=root).error_code == "outside_workspace"
    assert list_files("nope", root=root).error_code == "not_found"


# --------------------------------------------------------------------------
# Step 13: output limits
# --------------------------------------------------------------------------


def test_large_directory_is_truncated():
    """The many/ fixture: 5000 files, 75KB of context if left alone."""
    result = list_files("many", root=workspace_root())

    assert result.ok is True
    assert result.truncated is True


def test_the_model_is_told_about_the_truncation():
    """The flag is for the code; this string is the model's only clue."""
    content = list_files("many", root=workspace_root()).content

    assert "truncated" in content
    assert "5000" in content


def test_truncated_output_is_actually_small():
    result = list_files("many", root=workspace_root())

    assert len(result.content) < 10_000
    assert len(result.content.splitlines()) <= MAX_ENTRIES + 2


def test_entry_cap_is_respected(tmp_path):
    for i in range(MAX_ENTRIES + 50):
        (tmp_path / f"f_{i:04d}.txt").touch()

    result = list_files(".", root=tmp_path)
    names = [n for n in result.content.splitlines() if n.startswith("f_")]

    assert len(names) == MAX_ENTRIES
    assert names[0] == "f_0000.txt"  # deterministic cut: alphabetical


def test_byte_cap_trips_before_the_entry_cap(tmp_path):
    """Few entries, enormous names: the entry cap alone would not save us.

    50 names of ~207 bytes is ~10KB - over MAX_BYTES, but well under the
    200-entry cap. Only the byte cap can catch this.
    """
    for i in range(50):
        (tmp_path / (f"{i:02d}_" + "x" * 200 + ".txt")).touch()

    result = list_files(".", root=tmp_path)

    assert result.truncated is True
    assert len(result.content.encode()) < MAX_BYTES + 200
    assert len([n for n in result.content.splitlines() if n]) < 50


def test_flag_and_notice_always_agree(tmp_path):
    """The step 13 bug is setting one without the other."""
    for count in [1, 5, MAX_ENTRIES - 1, MAX_ENTRIES, MAX_ENTRIES + 1]:
        d = tmp_path / f"d{count}"
        d.mkdir()
        for i in range(count):
            (d / f"f_{i:04d}.txt").touch()

        result = list_files(f"d{count}", root=tmp_path)

        assert result.truncated == ("truncated" in result.content), count


def test_small_directories_are_untouched():
    result = list_files("src", root=workspace_root())

    assert result.truncated is False
    assert result.content == "app.py\nutils.py\n\n(2 entries)"


def test_the_notice_itself_is_never_cut_off(tmp_path):
    """A truncated truncation notice is worse than none."""
    for i in range(400):
        (tmp_path / (f"{i:03d}_" + "y" * 120)).touch()

    content = list_files(".", root=tmp_path).content

    assert content.rstrip().endswith(")")
    assert "entries shown" in content


# --------------------------------------------------------------------------
# Step 14: the whole attack table, in one place
# --------------------------------------------------------------------------
#
# These rows ARE section C of docs/session-02.md, transcribed literally. When
# the two disagree, one of them is a bug - that is the point of keeping the
# shape identical rather than folding these into the tests above.
#
# Assertions are on `error_code`, never on message prose: codes are the stable
# contract that Session 11's evals match on, while messages stay free to
# improve. The two rows whose whole point IS the string say so explicitly.

ATTACK_TABLE = [
    # (path,                      ok,     error_code,          what it defeats)
    (".",                         True,   None,                "baseline"),
    ("src",                       True,   None,                "baseline"),
    ("..",                        False,  "outside_workspace", "lexical traversal"),
    ("a/../../..",                False,  "outside_workspace", "traversal mid-path"),
    ("/etc/passwd",               False,  "absolute_path",     "absolute ignores the root"),
    ("../playground-evil",        False,  "outside_workspace", "the startswith trap"),
    ("escape",                    False,  "outside_workspace", "symlink out, final component"),
    ("evil_dir/loot.txt",         False,  "outside_workspace", "symlink out, parent component"),
    ("inside_link.py",            False,  "symlink",           "symlink staying inside (D5)"),
    ("inside_dir_link/app.py",    False,  "symlink",           "symlink parent, stays inside"),
    ("/etc/definitely-not-here",  False,  "absolute_path",     "error-message oracle"),
    ("nope",                      False,  "not_found",         "ordinary tool error"),
    ("calculator.py",             False,  "not_a_directory",   "exists, wrong type"),
    ("empty",                     True,   None,                "non-empty content"),
    ("many",                      True,   None,                "truncation notice"),
    ("",                          True,   None,                "D7: alias to '.'"),
    ("a\0b",                      False,  "invalid_path",      "ValueError must not escape"),
    ("x" * 300,                   False,  "invalid_path",      "OSError must not escape"),
]


@pytest.mark.parametrize(
    "path,expected_ok,expected_code",
    [(row[0], row[1], row[2]) for row in ATTACK_TABLE],
    ids=[row[3] for row in ATTACK_TABLE],
)
def test_attack_table(path, expected_ok, expected_code):
    result = list_files(path, root=workspace_root())

    assert result.ok is expected_ok
    assert result.error_code == expected_code


def test_every_row_returns_a_result():
    """No row may raise. This is the contract the Session 4 loop relies on."""
    for path, *_ in ATTACK_TABLE:
        try:
            list_files(path, root=workspace_root())
        except Exception as exc:  # noqa: BLE001 - that is the point
            pytest.fail(f"raw {type(exc).__name__} escaped for {path[:20]!r}: {exc}")


def test_no_result_content_is_ever_empty():
    """content="" reads to a model as "the tool broke", on every row."""
    for path, *_ in ATTACK_TABLE:
        content = list_files(path, root=workspace_root()).content
        assert content.strip() != "", path[:20]


def test_no_result_leaks_a_host_path():
    """Not one row may put the resolved path into the model's context."""
    root = workspace_root()
    for path, *_ in ATTACK_TABLE:
        content = list_files(path, root=root).content
        assert str(root) not in content, path[:20]
        assert "/private/etc" not in content, path[:20]


def test_the_two_rows_that_are_about_the_string():
    """empty and many are the rows where `content` IS the assertion."""
    root = workspace_root()

    assert list_files("empty", root=root).content == "(empty directory)"

    many = list_files("many", root=root)
    assert many.truncated is True
    assert "truncated" in many.content
    assert "5000" in many.content
