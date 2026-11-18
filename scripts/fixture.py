"""Fixture projects for the read -> edit workflow. (Session 7+)

    python scripts/fixture.py list
    python scripts/fixture.py build  tail_window
    python scripts/fixture.py verify tail_window --workspace=<dir>
    python scripts/fixture.py reset  tail_window --workspace=<dir>
    python scripts/fixture.py task   tail_window

A case is a directory under `fixtures/` - see fixtures/README.md for the
shape. The decisions this file implements:

D53/D54  The template IS the source of truth and a reset is a fresh copy, not
         a `git checkout`. A checkout resets tracked files to their committed
         state and leaves an untracked one exactly where it was, so the one
         kind of damage a run can do that a checkout misses is the kind a
         write tool does when it goes wrong. A copy has no such blind spot and
         works on a dirty tree.

D55      `build` returns a NEW directory per run, under .runtime/eval-workspaces/.
         Point a run at it with `python -m agent.cli --workspace=<dir> ...`.
         Runs that share a workspace are not repetitions of an experiment;
         they are one experiment with a growing amount of prior state.

D58/D59  Verification asks the FILESYSTEM two questions, never the transcript:
         did the case's check script pass, and did anything change that was
         not allowed to? Both must hold. The check alone would accept an agent
         that fixed the bug and flattened three other files on the way; the
         changed-file rule alone would accept an agent that edited the right
         file to no effect.

What this harness deliberately does NOT do: decide whether a run was good. It
reports pass or fail for one run against one case. The tally across runs is
Session 7's Phase 3, by hand; automating it is Session 11.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from agent.storage import EVAL_WORKSPACES_DIR, PATHS

FIXTURES_DIR = PROJECT_ROOT / "fixtures"

# Every build and every reset happens under here, and nowhere else. `reset`
# deletes a directory tree, and a tool that deletes a tree should be able to
# say in one sentence which trees it can reach: this one can reach
# .runtime/eval-workspaces/ and nothing above it. A typo'd --workspace is then a
# refusal rather than a morning spent in a backup.
BUILD_DIR = EVAL_WORKSPACES_DIR

# Not hashed, not reported as a change: these are produced by running the
# workspace's own code, not by the agent. check.py runs under -B so they
# should never appear, which makes this a second line of defence rather than
# the first one.
IGNORED_NAMES = {"__pycache__", ".DS_Store"}

CHECK_TIMEOUT_S = 30.0


class CaseError(Exception):
    """A case that cannot be loaded, or a path this harness refuses to touch."""


@dataclass(frozen=True)
class Case:
    """One fixture case, already validated."""

    name: str
    task: str
    allowed_changed: tuple[str, ...]
    golden: dict[str, str]
    max_steps: int | None
    notes: str
    edits: tuple[dict[str, str], ...] = ()
    answer: str = "Fixed it."
    category: str = "simple_edit"
    required_errors: tuple[str, ...] = ()
    probes: tuple[dict, ...] = ()

    @property
    def directory(self) -> Path:
        return FIXTURES_DIR / self.name

    @property
    def template(self) -> Path:
        """The pristine workspace. Never written to - copied from."""
        return self.directory / "workspace"

    @property
    def check_script(self) -> Path:
        """The independent check. Outside `workspace/` on purpose (D60)."""
        return self.directory / "verify" / "check.py"


def list_cases() -> list[str]:
    if not FIXTURES_DIR.is_dir():
        return []
    return sorted(
        entry.name
        for entry in FIXTURES_DIR.iterdir()
        if (entry / "case.json").is_file()
    )


def load_case(name: str) -> Case:
    """Read and validate one case.

    Validated here rather than at use time so that a malformed case fails
    before a run is started, not after one has been paid for.
    """
    if name not in list_cases():
        raise CaseError(f"Unknown fixture case: {name!r}")
    directory = FIXTURES_DIR / name
    case_file = directory / "case.json"
    if not case_file.is_file():
        known = ", ".join(list_cases()) or "(none)"
        raise CaseError(f"No fixture case named {name!r}. Known cases: {known}.")

    try:
        data = json.loads(case_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CaseError(f"{case_file} is not valid JSON ({exc.msg}).") from exc

    task = data.get("task")
    if not isinstance(task, str) or not task.strip():
        raise CaseError(f"{case_file} needs a non-empty 'task'.")

    allowed = data.get("allowed_changed", [])
    if not isinstance(allowed, list) or not all(
        isinstance(p, str) and p and not Path(p).is_absolute() and '..' not in Path(p).parts
        for p in allowed
    ):
        raise CaseError(f"{case_file}: 'allowed_changed' must be a list of paths.")

    golden = data.get("golden", {})
    edits = data.get("edits", [golden] if golden else [])
    if not isinstance(edits, list) or any(
        not isinstance(edit, dict) or set(edit) != {"path", "old", "new"}
        or not all(isinstance(v, str) for v in edit.values())
        or not edit['path'] or not edit['old']
        or Path(edit["path"]).is_absolute() or ".." in Path(edit["path"]).parts
        for edit in edits
    ):
        raise CaseError(f"{case_file}: edits need relative path, old and new strings.")
    max_steps = data.get("max_steps")
    if max_steps is not None and (type(max_steps) is not int or max_steps < 1):
        raise CaseError(f"{case_file}: max_steps must be a positive integer.")

    case = Case(
        name=name,
        task=task,
        allowed_changed=tuple(allowed),
        golden=golden,
        max_steps=max_steps,
        notes=data.get("notes", ""),
        edits=tuple(edits),
        answer=data.get("answer", "Fixed it."),
        category=data.get("category", "simple_edit"),
        required_errors=tuple(data.get("required_errors", [])),
        probes=tuple(data.get("probes", [])),
    )

    if not case.template.is_dir():
        raise CaseError(f"{case.template} is missing.")
    if not case.check_script.is_file():
        raise CaseError(f"{case.check_script} is missing.")
    return case


def hash_tree(root: Path) -> dict[str, str]:
    """relative path -> sha256 of the bytes. The whole tree, sorted.

    Bytes, not text. A fixture reset that restores the characters and changes
    the line endings is not a reset, and Session 6 spent a whole step on the
    difference.

    A symlink is hashed as the link target it names rather than followed: a
    run that replaces a file with a link to the original has changed the
    workspace, and following the link would hide exactly that.
    """
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if any(part in IGNORED_NAMES for part in path.relative_to(root).parts):
            continue
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            digests[relative] = "link:" + hashlib.sha256(
                str(path.readlink()).encode("utf-8")
            ).hexdigest()
        elif path.is_file():
            digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            digests[relative + "/"] = "dir"
    return digests


def _require_disposable(path: Path) -> Path:
    """Refuse any path this harness is not allowed to delete."""
    resolved = PATHS.resolve_artifact(path).expanduser().resolve()
    build_root = BUILD_DIR.resolve()
    if resolved == build_root or build_root not in resolved.parents:
        raise CaseError(
            f"{path} is not under {BUILD_DIR}. build and reset only touch "
            "directories they created - delete anything else by hand."
        )
    return resolved


def build(case: Case, into: Path | None = None) -> Path:
    """Copy the case's template to a fresh directory and return it.

    `into` must not exist. Overwriting on build would make "fresh workspace"
    depend on the caller remembering to pass a new path, and the one time they
    forget is the run whose result they keep.
    """
    if into is None:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        into = BUILD_DIR / f"{case.name}-{stamp}-{hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:6]}"
    target = into.expanduser().resolve()
    if target.exists():
        raise CaseError(f"{target} already exists. Use `reset` to reuse it.")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(case.template, target, symlinks=True,
                    ignore=shutil.ignore_patterns(*IGNORED_NAMES))
    return target


def reset(case: Case, workspace: Path) -> Path:
    """Delete a built workspace and copy the template back into place."""
    target = _require_disposable(workspace)
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(case.template, target, symlinks=True,
                    ignore=shutil.ignore_patterns(*IGNORED_NAMES))
    return target


@dataclass(frozen=True)
class Changes:
    """What a run did to the workspace, by comparison with the template."""

    modified: tuple[str, ...]
    added: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def all(self) -> tuple[str, ...]:
        return tuple(sorted({*self.modified, *self.added, *self.removed}))

    def unexpected(self, allowed: tuple[str, ...]) -> tuple[str, ...]:
        """Changes outside the case's allowance.

        A removal is never allowed, even of an allowed path: `replace_text`
        cannot delete a file, so a missing one means something happened that
        this project has no tool for.
        """
        allowed_set = set(allowed)
        return tuple(
            sorted(
                [p for p in self.modified if p not in allowed_set]
                + [p for p in self.added if p not in allowed_set]
                + list(self.removed)
            )
        )


def changes(case: Case, workspace: Path) -> Changes:
    before = hash_tree(case.template)
    after = hash_tree(workspace)
    return Changes(
        modified=tuple(
            sorted(p for p in before.keys() & after.keys() if before[p] != after[p])
        ),
        added=tuple(sorted(after.keys() - before.keys())),
        removed=tuple(sorted(before.keys() - after.keys())),
    )


@dataclass(frozen=True)
class Report:
    """The verdict for one run. `ok` is the only field a caller should branch on."""

    case: str
    workspace: Path
    check_ok: bool
    check_output: str
    changes: Changes
    unexpected: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.check_ok and not self.unexpected


def run_check(case: Case, workspace: Path, *, answer: str = "") -> tuple[bool, str]:
    """Run the case's check script against a workspace, in its own process.

    Its own process because the check imports code the agent has edited, and
    an edited module that loops forever, calls sys.exit or corrupts an import
    cache would take this harness with it. A subprocess with a timeout turns
    all three into a failed check.

    -I isolates it so the checker's own directory cannot shadow a workspace
    module; -B keeps a __pycache__ from appearing in a workspace whose file
    list is about to be compared byte for byte.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-B", str(case.check_script), str(workspace)],
            capture_output=True,
            text=True,
            input=answer,
            timeout=CHECK_TIMEOUT_S,
            cwd=str(PROJECT_ROOT),
        )
    except subprocess.TimeoutExpired:
        return False, f"check timed out after {CHECK_TIMEOUT_S:.0f}s"
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode == 0, output


def verify(case: Case, workspace: Path, *, answer: str = "") -> Report:
    """The whole verdict: the check, plus what moved on disk."""
    resolved = PATHS.resolve_artifact(workspace).expanduser().resolve()
    if not resolved.is_dir():
        raise CaseError(f"{workspace} is not a directory.")
    check_ok, output = run_check(case, resolved, answer=answer)
    changed = changes(case, resolved)
    return Report(
        case=case.name,
        workspace=resolved,
        check_ok=check_ok,
        check_output=output,
        changes=changed,
        unexpected=changed.unexpected(case.allowed_changed),
    )


def apply_golden(case: Case, workspace: Path) -> None:
    """Apply the case's recorded fix. The hand-run of Phase 1, as a function.

    Deliberately NOT routed through agent.tools.replace_text: this is the
    answer key, and an answer key that shares a code path with the thing it
    grades proves less than it appears to. tests/test_fixture.py drives the
    real tool separately.
    """
    for edit in case.edits:
        target = workspace / edit["path"]
        text = target.read_text(encoding="utf-8")
        if text.count(edit["old"]) != 1:
            raise CaseError(f"{case.name}: golden anchor must match exactly once.")
        target.write_text(text.replace(edit["old"], edit["new"]), encoding="utf-8")


def _print_report(report: Report) -> None:
    verdict = "PASS" if report.ok else "FAIL"
    print(f"{verdict} {report.case}  workspace={report.workspace}")
    print(f"  check: {'passed' if report.check_ok else 'FAILED'}")
    for line in report.check_output.splitlines():
        print(f"    {line}")
    changed = report.changes.all
    print(f"  changed: {', '.join(changed) if changed else '(nothing)'}")
    if report.unexpected:
        print(f"  UNEXPECTED: {', '.join(report.unexpected)}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="fixture.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show the known cases")

    for name, help_text in (
        ("build", "copy a case's template to a fresh workspace"),
        ("verify", "check a workspace against a case"),
        ("reset", "restore a built workspace from the template"),
        ("task", "print the case's task text"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("case")
        if name in ("verify", "reset"):
            p.add_argument("--workspace", required=True)
        if name == "build":
            p.add_argument("--into", default=None)
            p.add_argument(
                "--golden",
                action="store_true",
                help="apply the recorded fix straight away (a known-good run)",
            )

    args = parser.parse_args(argv[1:])

    if args.command == "list":
        for name in list_cases():
            case = load_case(name)
            print(f"{name}\n    {case.task}")
        return 0

    try:
        case = load_case(args.case)

        if args.command == "task":
            print(case.task)
            return 0

        if args.command == "build":
            into = Path(args.into) if args.into else None
            workspace = build(case, into)
            if args.golden:
                apply_golden(case, workspace)
            print(workspace)
            return 0

        if args.command == "reset":
            print(reset(case, Path(args.workspace)))
            return 0

        report = verify(case, Path(args.workspace))
    except CaseError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    _print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
