"""Tests for agent.policy.

Runs with no API key and no network. That is the point: when an eval fails in
Session 11, these tests are how you tell a broken tool from a bad model choice.
"""

from pathlib import Path

import pathlib

import pytest

from agent.policy import (
    PROJECT_ROOT,
    PolicyViolation,
    resolve_in_workspace,
    workspace_root,
)


def test_root_is_absolute():
    assert workspace_root().is_absolute()


def test_repeated_calls_agree():
    assert workspace_root() == workspace_root()


def test_env_var_is_honored(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    assert workspace_root() == tmp_path.resolve()


def test_override_beats_env_var(tmp_path, monkeypatch):
    """Session 13 gives each concurrent run its own root via this argument."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "from_env"))
    explicit = tmp_path / "from_arg"
    assert workspace_root(explicit) == explicit.resolve()


def test_symlinked_root_is_resolved(tmp_path, monkeypatch):
    """D2: the root is resolved, not just normalized.

    Without this, a workspace reached through a symlink has a different string
    than the paths resolved inside it, and every containment check in steps 4-7
    fails on paths that are plainly inside. This is the macOS /var -> /private/var
    trap, written portably.
    """
    real = tmp_path / "real_workspace"
    real.mkdir()
    link = tmp_path / "link_to_workspace"
    link.symlink_to(real)

    monkeypatch.setenv("WORKSPACE_DIR", str(link))

    assert workspace_root() == real.resolve()
    assert workspace_root() != link  # the un-resolved path is NOT what you get


def test_default_is_playground(monkeypatch):
    monkeypatch.delenv("WORKSPACE_DIR", raising=False)
    assert workspace_root().name == "playground"


def test_default_does_not_depend_on_cwd(tmp_path, monkeypatch):
    """The default workspace is anchored to the project, not the caller's cwd.

    Before the fix this returned <cwd>/playground, so running the CLI from ~
    silently pointed the agent at a nonexistent ~/playground.
    """
    monkeypatch.delenv("WORKSPACE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert workspace_root() == PROJECT_ROOT / "playground"


def test_explicit_relative_path_still_follows_cwd(tmp_path, monkeypatch):
    """Deliberate asymmetry: only the DEFAULT is anchored to the project."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "custom").mkdir()
    assert workspace_root("custom") == (tmp_path / "custom").resolve()


# --------------------------------------------------------------------------
# Step 2: resolve_in_workspace - happy path only
# --------------------------------------------------------------------------


def test_dot_resolves_to_the_root_itself():
    root = workspace_root()
    assert resolve_in_workspace(".", root=root) == root


def test_subdirectory_resolves_under_root():
    root = workspace_root()
    assert resolve_in_workspace("src", root=root) == root / "src"


def test_nested_file_resolves_under_root():
    root = workspace_root()
    assert resolve_in_workspace("src/app.py", root=root) == root / "src" / "app.py"


def test_result_is_absolute():
    assert resolve_in_workspace("src", root=workspace_root()).is_absolute()


def test_resolution_is_relative_to_root_not_cwd(tmp_path, monkeypatch):
    """The whole point of the `root` argument.

    If this resolved against the process cwd, `pytest` run from a different
    directory would silently validate a different file.
    """
    monkeypatch.chdir(tmp_path)
    root = workspace_root()
    assert resolve_in_workspace("src/app.py", root=root) == root / "src" / "app.py"


def test_missing_target_still_resolves():
    """Not an error here. `not_found` is a tool error, raised later."""
    root = workspace_root()
    assert resolve_in_workspace("nope/missing.txt", root=root) == root / "nope" / "missing.txt"


# --------------------------------------------------------------------------
# Step 3: reject absolute paths
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "/", "/Users/sifat/.ssh/id_rsa", "/etc/definitely-not-here"],
)
def test_absolute_paths_are_rejected(path):
    with pytest.raises(PolicyViolation) as exc:
        resolve_in_workspace(path, root=workspace_root())
    assert exc.value.code == "absolute_path"


def test_absolute_path_is_rejected_before_touching_the_filesystem(monkeypatch):
    """The rule must fire before the join, not after resolution.

    If anything stats or resolves the path first, /etc/passwd has already been
    touched. Path.resolve is patched to explode so the test fails loudly if the
    order is ever reversed.
    """

    def boom(self, *a, **kw):
        raise AssertionError("resolve() was called on an absolute path")

    monkeypatch.setattr("pathlib.Path.resolve", boom)

    with pytest.raises(PolicyViolation):
        resolve_in_workspace("/etc/passwd", root=PROJECT_ROOT / "playground")


def test_nonexistent_absolute_path_gives_nothing_away():
    """Same code for a real file and a missing one - no existence oracle."""
    root = workspace_root()
    with pytest.raises(PolicyViolation) as real:
        resolve_in_workspace("/etc/passwd", root=root)
    with pytest.raises(PolicyViolation) as fake:
        resolve_in_workspace("/etc/definitely-not-here", root=root)
    assert real.value.code == fake.value.code


def test_message_does_not_leak_the_resolved_path():
    with pytest.raises(PolicyViolation) as exc:
        resolve_in_workspace("/etc/passwd", root=workspace_root())
    assert "/private/etc" not in exc.value.message
    assert str(PROJECT_ROOT) not in exc.value.message


def test_relative_paths_still_work():
    """Every new rule is a chance to break the happy path."""
    root = workspace_root()
    assert resolve_in_workspace("src/app.py", root=root) == root / "src" / "app.py"


# --------------------------------------------------------------------------
# Step 4: containment
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["..", "../..", "a/../../..", "../playground-evil", "src/../../.."],
)
def test_traversal_is_rejected(path):
    with pytest.raises(PolicyViolation) as exc:
        resolve_in_workspace(path, root=workspace_root())
    assert exc.value.code == "outside_workspace"


def test_sibling_directory_defeats_a_startswith_check():
    """The reason the containment check is is_relative_to and not startswith.

    playground-evil lives OUTSIDE playground, but its absolute path begins with
    playground's absolute path as a string. The first assert documents that the
    naive check really would pass; the second pins that ours does not.
    """
    root = workspace_root()
    evil = (root.parent / "playground-evil").resolve()

    assert str(evil).startswith(str(root))          # the naive check: fooled
    assert not evil.is_relative_to(root)            # the real check: not fooled

    with pytest.raises(PolicyViolation) as exc:
        resolve_in_workspace("../playground-evil", root=root)
    assert exc.value.code == "outside_workspace"


def test_traversal_through_a_directory_that_does_not_exist():
    """`..` is collapsed lexically, so none of the path has to be real."""
    with pytest.raises(PolicyViolation) as exc:
        resolve_in_workspace("no_such_dir/../../..", root=workspace_root())
    assert exc.value.code == "outside_workspace"


def test_unresolved_root_still_works(tmp_path):
    """A caller passing an unresolved root must not break containment.

    On macOS tmp_path sits under /private/var while /var is a symlink to it,
    which is the realistic version of this mistake.
    """
    real = tmp_path / "ws"
    real.mkdir()
    (real / "file.txt").touch()
    link = tmp_path / "ws_link"
    link.symlink_to(real)

    assert resolve_in_workspace("file.txt", root=link) == (real / "file.txt").resolve()


def test_containment_message_does_not_leak_host_paths():
    with pytest.raises(PolicyViolation) as exc:
        resolve_in_workspace("../playground-evil", root=workspace_root())
    assert str(PROJECT_ROOT) not in exc.value.message


def test_inside_paths_still_work_after_step_4():
    root = workspace_root()
    assert resolve_in_workspace("src/app.py", root=root) == root / "src" / "app.py"
    assert resolve_in_workspace(".", root=root) == root


# --------------------------------------------------------------------------
# Step 5: symlinks
# --------------------------------------------------------------------------


def test_symlink_as_final_component_is_rejected():
    """A symlink pointing INSIDE the workspace. Containment allows it (it
    resolves to playground/src/app.py), so only this rule rejects it."""
    with pytest.raises(PolicyViolation) as exc:
        resolve_in_workspace("inside_link.py", root=workspace_root())
    assert exc.value.code == "symlink"


def test_symlink_as_parent_component_is_rejected():
    """The case Path.is_symlink() cannot see.

    inside_dir_link/app.py resolves to playground/src/app.py - inside - and
    is_symlink() on the full path is False because only the PARENT is a link.
    Walking the components is the only thing that catches it.
    """
    root = workspace_root()
    full = root / "inside_dir_link" / "app.py"
    assert full.resolve().is_relative_to(root)   # containment would allow it
    assert not full.is_symlink()                 # the naive check sees nothing
    assert full.parent.is_symlink()              # the parent is the link

    with pytest.raises(PolicyViolation) as exc:
        resolve_in_workspace("inside_dir_link/app.py", root=root)
    assert exc.value.code == "symlink"


@pytest.mark.parametrize("path", ["escape", "evil_dir/loot.txt"])
def test_outward_symlinks_are_rejected_by_containment_first(path):
    """Documents the ORDER, not just the rejection.

    These two resolve outside the workspace, so step 4 fires before step 5 ever
    runs and the code is outside_workspace rather than symlink. If the order
    changes, this test says so instead of silently passing.
    """
    with pytest.raises(PolicyViolation) as exc:
        resolve_in_workspace(path, root=workspace_root())
    assert exc.value.code == "outside_workspace"


def test_symlink_rule_does_not_depend_on_resolution(tmp_path):
    """Defence in depth: the walk uses lstat, not resolve().

    If step 4 were ever weakened to a lexical normpath, this rule would still
    reject a symlink that points out of the workspace.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope")
    (ws / "link").symlink_to(outside)

    with pytest.raises(PolicyViolation) as exc:
        resolve_in_workspace("link/secret.txt", root=ws)
    assert exc.value.code in {"symlink", "outside_workspace"}


def test_normal_paths_are_not_mistaken_for_symlinks():
    root = workspace_root()
    assert resolve_in_workspace("src/app.py", root=root) == root / "src" / "app.py"
    assert resolve_in_workspace(".", root=root) == root
    assert resolve_in_workspace("nope/missing.txt", root=root) == root / "nope" / "missing.txt"


# --------------------------------------------------------------------------
# Step 6: check order - error codes must not reveal existence
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "real,fabricated",
    [
        # absolute: one exists on every unix box, one certainly does not
        ("/etc/passwd", "/etc/zzz-does-not-exist-9a7f"),
        # traversal onto a real sibling vs a fabricated one
        ("../playground-evil", "../zzz-no-such-sibling-9a7f"),
        # traversal onto a real parent file vs a fabricated one
        ("../README.md", "../zzz-no-such-file-9a7f"),
    ],
)
def test_rejection_does_not_depend_on_existence(real, fabricated):
    """The whole of step 6.

    If these codes ever differ, the agent can be used to enumerate the host
    filesystem: ask for a path and read the answer off the error code.
    """
    root = workspace_root()

    with pytest.raises(PolicyViolation) as a:
        resolve_in_workspace(real, root=root)
    with pytest.raises(PolicyViolation) as b:
        resolve_in_workspace(fabricated, root=root)

    assert a.value.code == b.value.code


def test_the_real_targets_in_that_test_actually_exist():
    """Guards the test above.

    If /etc/passwd or playground-evil vanished, the pairs would both be
    'fabricated' and the test would pass while proving nothing.
    """
    root = workspace_root()
    assert pathlib.Path("/etc/passwd").exists()
    assert (root.parent / "playground-evil").exists()
    assert (root.parent / "README.md").exists()


def test_policy_contains_no_existence_check():
    """Existence belongs to tools.py (not_found), never to the boundary.

    A path inside the workspace that does not exist must resolve cleanly here.
    If this ever raises, someone has added an existence check to the policy and
    reintroduced the oracle.
    """
    root = workspace_root()
    assert resolve_in_workspace("zzz/not/here.txt", root=root) == root / "zzz" / "not" / "here.txt"


def test_message_text_also_hides_existence():
    """Not just the code - the prose the model sees must match too.

    The model's context becomes a JSONL trace in Session 10. A message that
    said 'no such file' for one and 'outside workspace' for the other would
    leak exactly what the codes were careful not to.
    """
    root = workspace_root()

    with pytest.raises(PolicyViolation) as a:
        resolve_in_workspace("../playground-evil", root=root)
    with pytest.raises(PolicyViolation) as b:
        resolve_in_workspace("../zzz-no-such-sibling-9a7f", root=root)

    # identical except for the path the caller supplied
    assert a.value.message.replace("../playground-evil", "X") == b.value.message.replace(
        "../zzz-no-such-sibling-9a7f", "X"
    )


# --------------------------------------------------------------------------
# Step 7: malformed input
# --------------------------------------------------------------------------


def test_null_byte_is_rejected():
    """resolve() raises ValueError on this. It must never reach the caller."""
    with pytest.raises(PolicyViolation) as exc:
        resolve_in_workspace("a\0b", root=workspace_root())
    assert exc.value.code == "invalid_path"


def test_null_byte_is_rejected_before_any_syscall(monkeypatch):
    """Same argument as step 3: no filesystem call should see this input."""

    def boom(self, *a, **kw):
        raise AssertionError("resolve() was called on a null-byte path")

    monkeypatch.setattr("pathlib.Path.resolve", boom)

    with pytest.raises(PolicyViolation) as exc:
        resolve_in_workspace("a\0b", root=PROJECT_ROOT / "playground")
    assert exc.value.code == "invalid_path"


def test_overlong_component_is_rejected():
    """This one survives resolve() and explodes in the step 5 symlink walk.

    OSError [Errno 63] File name too long - a different exception, from a
    different step, than the null byte. That is why step 7 wraps the whole
    region rather than one call.
    """
    with pytest.raises(PolicyViolation) as exc:
        resolve_in_workspace("x" * 300, root=workspace_root())
    assert exc.value.code == "invalid_path"


@pytest.mark.parametrize(
    "path",
    [
        "a\0b",
        "x" * 300,
        "x" * 5000,
        "a/" + "b" * 400 + "/c",
        "\0",
        "sr\0c/app.py",
    ],
)
def test_no_raw_exception_escapes(path):
    """Session 8 starts here: malformed arguments must not crash the process.

    Anything that is not a PolicyViolation leaving this function would reach
    the agent loop as an unhandled crash.
    """
    try:
        resolve_in_workspace(path, root=workspace_root())
    except PolicyViolation:
        pass
    except Exception as exc:  # noqa: BLE001 - that is the point of the test
        pytest.fail(f"raw {type(exc).__name__} escaped: {exc}")


def test_long_path_is_truncated_in_the_message():
    """A 5000-char path must not put 5000 characters into the model's context."""
    with pytest.raises(PolicyViolation) as exc:
        resolve_in_workspace("x" * 5000, root=workspace_root())
    assert len(exc.value.message) < 200
    assert "5000 chars" in exc.value.message


def test_empty_string_is_the_workspace_root():
    """D7, decided: "" behaves as "." rather than being rejected.

    Path("").parts is (), so the symlink walk sees no components and the join
    leaves the root unchanged. Documented here because it is a decision, not
    an accident.
    """
    root = workspace_root()
    assert resolve_in_workspace("", root=root) == root
    assert resolve_in_workspace(".", root=root) == root


def test_valid_paths_still_work_after_step_7():
    root = workspace_root()
    assert resolve_in_workspace("src/app.py", root=root) == root / "src" / "app.py"
