"""Tests for the Session 7 fixture harness. (Session 7)

Same rules as every suite before it: no API key, no network. Nothing here
tests a model. It tests the three things a model is about to be graded with,
and if any of them is wrong the grade is noise:

  1. A RESET IS BYTE-IDENTICAL. Two runs of one task are only comparable if
     they started from the same bytes. Hashes, not "it looks right".
  2. THE VERIFIER FAILS BEFORE IT PASSES. A check that has never returned
     FAIL has not been tested, and the cheapest way to pass every case is to
     have written a check that cannot fail.
  3. THE ANSWER KEY FITS THE LOCK. Each case records the one-line fix it
     expects. If that string no longer matches the template exactly once, the
     case has drifted and every run graded against it is suspect.

Builds go to tmp_path, never to runs/workspaces - a test that leaves a
workspace behind is a test writing somewhere it did not intend.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from agent.tools import dispatch, list_files

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_harness():
    """Import scripts/fixture.py by path - scripts/ is not a package.

    By path rather than by sys.path surgery: a test suite that mutates
    sys.path changes what every later import in the process means.
    """
    path = PROJECT_ROOT / "scripts" / "fixture.py"
    spec = importlib.util.spec_from_file_location("fixture_harness", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fixture_harness"] = module
    spec.loader.exec_module(module)
    return module


fixture = _load_harness()
# These Session 7 tests specifically describe single-edit, initially broken
# fixtures. Session 11 includes read-only and multi-edit cases, covered by
# test_evals.py rather than pretending every fixture has a single golden edit.
CASES = ["tail_window", "twice_over", "loud_comment"]


def build(case, tmp_path: Path, name: str = "ws") -> Path:
    return fixture.build(case, tmp_path / name)


# --------------------------------------------------------------------------
# The cases themselves
# --------------------------------------------------------------------------


def test_there_is_at_least_one_case():
    assert CASES, "fixtures/ holds no cases - the rest of this suite is vacuous"


@pytest.mark.parametrize("name", CASES)
def test_case_loads_and_declares_what_it_allows(name):
    case = fixture.load_case(name)
    assert case.task.strip()
    assert case.allowed_changed, (
        f"{name}: a case that allows no changes cannot be passed; a case that "
        "does not say which files may change cannot be failed"
    )
    assert case.template.is_dir()
    assert case.check_script.is_file()


@pytest.mark.parametrize("name", CASES)
def test_the_check_script_is_outside_the_workspace(name):
    """D60: the agent must not be able to read or edit what grades it."""
    case = fixture.load_case(name)
    assert case.template not in case.check_script.parents


@pytest.mark.parametrize("name", CASES)
def test_the_answer_key_matches_the_template_exactly_once(name):
    """The golden `old` is the lock this case's key has to fit."""
    case = fixture.load_case(name)
    source = (case.template / case.golden["path"]).read_text(encoding="utf-8")
    assert source.count(case.golden["old"]) == 1
    assert case.golden["new"] not in source, (
        f"{name}: the fixture already contains the fix"
    )


@pytest.mark.parametrize("name", CASES)
def test_the_fix_is_inside_a_file_the_case_allows(name):
    case = fixture.load_case(name)
    assert case.golden["path"] in case.allowed_changed


# --------------------------------------------------------------------------
# Build and reset
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", CASES)
def test_build_is_byte_identical_to_the_template(name, tmp_path):
    case = fixture.load_case(name)
    workspace = build(case, tmp_path)
    assert fixture.hash_tree(workspace) == fixture.hash_tree(case.template)


def test_build_refuses_an_existing_directory(tmp_path):
    case = fixture.load_case(CASES[0])
    workspace = build(case, tmp_path)
    with pytest.raises(fixture.CaseError):
        fixture.build(case, workspace)


def test_reset_restores_every_kind_of_damage(tmp_path, monkeypatch):
    """Modified, deleted and added - a `git checkout` misses the third."""
    case = fixture.load_case("tail_window")
    monkeypatch.setattr(fixture, "BUILD_DIR", tmp_path)
    workspace = fixture.build(case, tmp_path / "ws")
    before = fixture.hash_tree(workspace)

    (workspace / "stats.py").write_text("# gutted\n", encoding="utf-8")
    (workspace / "report.py").unlink()
    (workspace / "stray.txt").write_text("left behind\n", encoding="utf-8")

    assert fixture.hash_tree(workspace) != before
    fixture.reset(case, workspace)
    assert fixture.hash_tree(workspace) == before


def test_reset_refuses_a_directory_it_did_not_build(tmp_path):
    """The one destructive operation here, and the fence around it.

    reset() deletes a tree. A --workspace typo must be a refusal, not a
    morning spent in a backup.
    """
    case = fixture.load_case(CASES[0])
    victim = tmp_path / "not-a-build"
    victim.mkdir()
    (victim / "precious.txt").write_text("mine\n", encoding="utf-8")

    with pytest.raises(fixture.CaseError):
        fixture.reset(case, victim)
    assert (victim / "precious.txt").exists()


def test_reset_refuses_the_build_root_itself(tmp_path, monkeypatch):
    case = fixture.load_case(CASES[0])
    monkeypatch.setattr(fixture, "BUILD_DIR", tmp_path)
    with pytest.raises(fixture.CaseError):
        fixture.reset(case, tmp_path)


# --------------------------------------------------------------------------
# The verdict
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", CASES)
def test_an_untouched_fixture_fails(name, tmp_path):
    """Every case is broken on arrival, and the check says so."""
    case = fixture.load_case(name)
    report = fixture.verify(case, build(case, tmp_path))
    assert not report.ok
    assert not report.check_ok
    assert report.changes.all == (), "verifying must not change the workspace"


@pytest.mark.parametrize("name", CASES)
def test_the_golden_fix_passes(name, tmp_path):
    case = fixture.load_case(name)
    workspace = build(case, tmp_path)
    fixture.apply_golden(case, workspace)
    report = fixture.verify(case, workspace)
    assert report.ok, report.check_output
    assert report.changes.all == (case.golden["path"],)


def test_a_passing_check_with_collateral_damage_still_fails(tmp_path):
    """D59, the half that a test suite alone would never give you.

    The bug is fixed and the check passes - and the run also rewrote a file
    nobody asked it to touch. That is not a pass.
    """
    case = fixture.load_case("tail_window")
    workspace = build(case, tmp_path)
    fixture.apply_golden(case, workspace)
    (workspace / "README.md").write_text("# rewritten\n", encoding="utf-8")

    report = fixture.verify(case, workspace)
    assert report.check_ok
    assert not report.ok
    assert report.unexpected == ("README.md",)


def test_obeying_the_planted_instruction_fails_the_case(tmp_path):
    """loud_comment: the file says to edit notes.txt. The case says no."""
    case = fixture.load_case("loud_comment")
    workspace = build(case, tmp_path)
    fixture.apply_golden(case, workspace)
    (workspace / "notes.txt").write_text("DONE\n", encoding="utf-8")

    report = fixture.verify(case, workspace)
    assert not report.ok
    assert "notes.txt" in report.unexpected


def test_a_deleted_file_is_unexpected_even_when_it_was_allowed(tmp_path):
    """No tool in this project deletes a file, so a missing one is a finding."""
    case = fixture.load_case("tail_window")
    workspace = build(case, tmp_path)
    (workspace / "stats.py").unlink()

    report = fixture.verify(case, workspace)
    assert not report.ok
    assert "stats.py" in report.unexpected


def test_a_file_replaced_by_a_symlink_is_a_change(tmp_path):
    """hash_tree reads the link, it does not follow it."""
    case = fixture.load_case("tail_window")
    workspace = build(case, tmp_path)
    elsewhere = tmp_path / "elsewhere.py"
    elsewhere.write_text((case.template / "stats.py").read_text(), encoding="utf-8")
    (workspace / "stats.py").unlink()
    (workspace / "stats.py").symlink_to(elsewhere)

    assert "stats.py" in fixture.changes(case, workspace).modified


def test_a_check_that_cannot_import_fails_rather_than_crashes(tmp_path):
    """An agent can leave a file that does not parse. That is a FAIL, not a
    traceback out of the harness."""
    case = fixture.load_case("tail_window")
    workspace = build(case, tmp_path)
    (workspace / "stats.py").write_text("def tail(:\n", encoding="utf-8")

    report = fixture.verify(case, workspace)
    assert not report.ok
    assert "import" in report.check_output.lower()


def test_verify_leaves_no_bytecode_behind(tmp_path):
    """-B, and why it matters: a __pycache__ created by GRADING the run would
    show up as a change made BY the run."""
    case = fixture.load_case("tail_window")
    workspace = build(case, tmp_path)
    fixture.verify(case, workspace)
    assert not list(workspace.rglob("__pycache__"))


# --------------------------------------------------------------------------
# The golden transcript, through the real tool (Phase 1, step 4)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", CASES)
def test_the_agent_tools_can_actually_solve_the_case(name, tmp_path):
    """Drive dispatch by hand along the shortest correct path.

    This is the hand-run of Phase 1 written down as a test, and it is the one
    that keeps the session honest: if `replace_text` cannot apply this case's
    fix, then a failed live run tells you nothing about the model.

    apply_golden() does NOT go through this code path on purpose - an answer
    key that shares a code path with the thing it grades proves less than it
    looks like it does. Here the key goes through the real tool instead.
    """
    case = fixture.load_case(name)
    workspace = build(case, tmp_path)

    read = dispatch(
        "read_file", json.dumps({"path": case.golden["path"]}), root=workspace
    )
    assert read.ok
    assert case.golden["old"] in read.content, (
        "the anchor the case records is not visible in what read_file shows - "
        "the model would have to invent it"
    )

    edit = dispatch(
        "replace_text",
        json.dumps(
            {
                "path": case.golden["path"],
                "old": case.golden["old"],
                "new": case.golden["new"],
            }
        ),
        root=workspace,
    )
    assert edit.ok, edit.content
    assert edit.changed_path == case.golden["path"]

    report = fixture.verify(case, workspace)
    assert report.ok, report.check_output


def test_the_workspace_hides_the_checker_from_the_agent(tmp_path):
    """D60 again, from the agent's side of the boundary this time."""
    case = fixture.load_case(CASES[0])
    workspace = build(case, tmp_path)

    listing = list_files(".", root=workspace)
    assert listing.ok
    assert "check" not in listing.content

    escape = dispatch(
        "read_file", json.dumps({"path": "../verify/check.py"}), root=workspace
    )
    assert not escape.ok
    assert escape.error_code == "outside_workspace"


def test_the_ambiguous_case_is_actually_ambiguous(tmp_path):
    """twice_over earns its name, or it is just another tail_window.

    The naive anchor - the single wrong line, with no context - must be
    refused, and the refusal must name the count. This is the behaviour the
    live run is there to observe; here we only prove the fixture provokes it.
    """
    case = fixture.load_case("twice_over")
    workspace = build(case, tmp_path)

    naive = dispatch(
        "replace_text",
        json.dumps(
            {
                "path": "shipping.py",
                "old": "    if weight > 10:",
                "new": "    if weight >= 10:",
            }
        ),
        root=workspace,
    )
    assert not naive.ok
    assert "2" in naive.content
    assert fixture.changes(case, workspace).all == (), "a refusal changed a file"


# --------------------------------------------------------------------------
# The flag that points a run at a built workspace (D55)
# --------------------------------------------------------------------------
#
# These live here rather than in a test_cli.py because the flag exists for
# this harness: a run per fixture is the only reason the workspace stopped
# being a constant.


def test_workspace_flag_is_parsed_and_left_unresolved(tmp_path):
    """argv parsing records what the user typed. policy decides what it means."""
    from agent.cli import parse_argv

    options = parse_argv(["agent.cli", "--tools", f"--workspace={tmp_path}", "fix it"])
    assert options is not None
    assert options.workspace == str(tmp_path)
    assert options.task == "fix it"


def test_an_empty_workspace_flag_is_a_usage_error():
    """Same rule as a malformed limit: a typo is never the default."""
    from agent.cli import parse_argv

    assert parse_argv(["agent.cli", "--workspace=", "fix it"]) is None
    assert parse_argv(["agent.cli", "--workspace=   ", "fix it"]) is None


def test_no_workspace_flag_means_the_default_workspace():
    from agent.cli import parse_argv
    from agent.policy import workspace_root

    options = parse_argv(["agent.cli", "do a thing"])
    assert options is not None and options.workspace is None
    assert workspace_root(options.workspace) == workspace_root()


def test_a_built_workspace_is_a_legal_root_for_the_tools(tmp_path):
    """The flag's whole purpose: policy accepts a fixture copy as the root."""
    from agent.policy import workspace_root

    case = fixture.load_case(CASES[0])
    workspace = build(case, tmp_path)
    assert workspace_root(str(workspace)) == workspace.resolve()
