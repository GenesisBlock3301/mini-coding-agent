"""list_folders: the same listing as list_files, with the files removed.

It buys no new capability - directories are the "/" lines in list_files - so
every test here is really asking one of two questions: does it agree with
list_files about what a directory IS, and does it refuse exactly what
list_files refuses? A tool that answers those differently is worse than no
tool, because the model would have to learn two boundaries.
"""

from __future__ import annotations

import os

import pytest

from agent.tools import (
    EMPTY_DIRECTORY,
    MAX_ENTRIES,
    NO_SUBDIRECTORIES,
    dispatch,
    list_files,
    list_folders,
)


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "app.py").write_text("x")
    (tmp_path / "README.md").write_text("y")
    return tmp_path


# ---- what it returns ----------------------------------------------------


def test_lists_directories_only(workspace):
    result = list_folders(".", root=workspace)

    assert result.ok
    names = result.content.splitlines()[:3]
    assert names == [".hidden/", "docs/", "src/"]
    assert "app.py" not in result.content
    assert "README.md" not in result.content


def test_the_footer_counts_directories_not_entries(workspace):
    assert "(3 directories)" in list_folders(".", root=workspace).content


def test_one_directory_is_singular(tmp_path):
    (tmp_path / "only").mkdir()
    assert "(1 directory)" in list_folders(".", root=tmp_path).content


def test_dotfolders_are_included(workspace):
    """Same rule as list_files: a filtered listing is a silent omission."""
    assert ".hidden/" in list_folders(".", root=workspace).content


def test_it_agrees_with_list_files_about_what_a_directory_is(workspace):
    from_files = {
        line for line in list_files(".", root=workspace).content.splitlines()
        if line.endswith("/")
    }
    from_folders = {
        line for line in list_folders(".", root=workspace).content.splitlines()
        if line.endswith("/")
    }
    assert from_files == from_folders


# ---- the empty cases, which are facts and not errors --------------------


def test_a_directory_with_no_subdirectories_is_ok_not_an_error(workspace):
    result = list_folders("src", root=workspace)

    assert result.ok
    assert result.error_code is None
    assert result.content == NO_SUBDIRECTORIES


def test_a_directory_holding_only_files_says_so(tmp_path):
    (tmp_path / "a.txt").write_text("1")
    (tmp_path / "b.txt").write_text("2")

    result = list_folders(".", root=tmp_path)

    assert result.ok
    assert result.content == NO_SUBDIRECTORIES
    assert result.content != EMPTY_DIRECTORY   # different fact, different words


def test_content_is_never_empty(tmp_path):
    """An empty string reads to a model as 'the tool broke'."""
    assert list_folders(".", root=tmp_path).content.strip() != ""


# ---- the boundary, which must match list_files exactly ------------------


@pytest.mark.parametrize(
    "path, code",
    [
        ("..", "outside_workspace"),
        ("a/../../..", "outside_workspace"),
        ("/etc", "absolute_path"),
    ],
)
def test_paths_outside_the_workspace_are_refused(workspace, path, code):
    result = list_folders(path, root=workspace)
    assert not result.ok
    assert result.error_code == code


def test_a_symlink_is_refused(workspace):
    os.symlink(workspace / "src", workspace / "link_to_src")
    result = list_folders("link_to_src", root=workspace)

    assert not result.ok
    assert result.error_code == "symlink"


def test_a_symlinked_directory_is_not_listed(workspace):
    """It would advertise a path the boundary then refuses to follow."""
    os.symlink(workspace / "src", workspace / "link_to_src")

    assert "link_to_src" not in list_folders(".", root=workspace).content


def test_a_missing_directory_is_a_tool_error(workspace):
    result = list_folders("nope", root=workspace)
    assert not result.ok
    assert result.error_code == "not_found"


def test_a_file_is_not_a_directory(workspace):
    result = list_folders("app.py", root=workspace)
    assert not result.ok
    assert result.error_code == "not_a_directory"


def test_the_two_tools_refuse_the_same_paths(workspace):
    for path in ("..", "/etc", "nope", "app.py"):
        assert (
            list_folders(path, root=workspace).error_code
            == list_files(path, root=workspace).error_code
        ), path


# ---- truncation ---------------------------------------------------------


def test_many_directories_are_truncated_and_say_so(tmp_path):
    for index in range(MAX_ENTRIES + 50):
        (tmp_path / f"d{index:04d}").mkdir()

    result = list_folders(".", root=tmp_path)

    assert result.ok
    assert result.truncated is True
    assert f"of {MAX_ENTRIES + 50} directories shown" in result.content


def test_a_listing_that_fits_is_not_flagged_truncated(workspace):
    assert list_folders(".", root=workspace).truncated is False


# ---- through dispatch, the way the model reaches it ---------------------


def test_dispatch_routes_the_new_name(workspace):
    result = dispatch("list_folders", '{"path": "."}', root=workspace)
    assert result.ok
    assert "src/" in result.content


def test_dispatch_still_rejects_an_argument_we_never_defined(workspace):
    result = dispatch("list_folders", '{"recursive": true}', root=workspace)

    assert not result.ok
    assert result.error_code == "invalid_arguments"
    assert "recursive" in result.content


def test_the_unknown_tool_message_now_names_three_tools(workspace):
    result = dispatch("list_dirs", "{}", root=workspace)

    assert result.error_code == "unknown_tool"
    for name in ("list_files", "list_folders", "read_file"):
        assert name in result.content
