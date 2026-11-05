"""Poke at the policy by hand and watch what it does. Edit freely.

    python scripts/try_policy.py              # verdict table + mechanism
    python scripts/try_policy.py --short      # verdict table only

This is not a test. Tests assert; this one just prints, so you can see the
actual return values and error messages instead of a pass/fail dot.
Add your own rows to ATTACKS and rerun.
"""

import sys
from pathlib import Path

# Running a file inside scripts/ puts scripts/ on sys.path, not the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.policy import PolicyViolation, resolve_in_workspace, workspace_root

ATTACKS = [
    # (path, what you expect to happen)
    ("src/app.py",         "ok - normal file"),
    ("src",                "ok - normal directory"),
    (".",                  "ok - the root itself"),
    ("nope/missing.txt",   "ok here - 'not found' is a TOOL error, added later"),
    ("..",                 "blocked by step 4"),
    ("a/../../..",         "blocked by step 4"),
    ("../playground-evil", "blocked by step 4 (the startswith trap)"),
    ("/etc/passwd",        "blocked by step 3"),
    ("/",                  "blocked by step 3"),
    ("escape",             "ALREADY blocked by step 4 - resolve() followed the link"),
    ("evil_dir/loot.txt",  "ALREADY blocked by step 4 - resolve() followed the link"),
    ("inside_link.py",     "blocked by step 5 - symlink, stays inside"),
    ("inside_dir_link/app.py", "blocked by step 5 - symlink PARENT, stays inside"),
]


def verdict_table(root: Path) -> None:
    """What the policy actually decides."""
    print("=" * 76)
    print("VERDICTS")
    print("=" * 76)
    for path, expectation in ATTACKS:
        try:
            resolved = resolve_in_workspace(path, root=root)
        except PolicyViolation as err:
            verdict = f"BLOCKED [{err.code}]"
        else:
            verdict = "*** ESCAPED ***" if not resolved.is_relative_to(root) else "allowed"
        print(f"  {path!r:24} {verdict:30} {expectation}")
    print()


def explain(path: str, root: Path) -> None:
    """Replay the checks from agent/policy.py, printing every intermediate value.

    This MIRRORS resolve_in_workspace - it is not the real thing. Edit the
    comparison below and rerun to see what a different rule would decide,
    without touching the module the tests cover.
    """
    print(f"  input            : {path!r}")

    # --- step 3: absolute paths, BEFORE the join -------------------------
    if Path(path).is_absolute():
        joined = root / path
        print("  step 3           : is_absolute() -> True")
        print(f"  had we joined    : {joined}")
        print("                     ^ the root was discarded: an absolute right-hand")
        print("                       side wins in pathlib, which is why step 3 must")
        print("                       run before the join")
        print("  verdict          : PolicyViolation('absolute_path')")
        print()
        return

    # --- step 4: containment ----------------------------------------------
    resolved_root = Path(root).resolve()
    candidate = (resolved_root / path).resolve()
    real_check = candidate.is_relative_to(resolved_root)
    naive_check = str(candidate).startswith(str(resolved_root))

    print(f"  1 root.resolve() : {resolved_root}")
    print(f"  2 candidate      : {candidate}")
    print(f"  3 is_relative_to : {real_check}    <- the rule we use")
    print(f"    startswith     : {naive_check}    <- the rule we rejected")
    if real_check != naive_check:
        print("                     ^^ THE TWO DISAGREE. startswith would have let")
        print("                        this through. This is the trap, on your disk.")
    decision = "allow" if real_check else "PolicyViolation('outside_workspace')"
    print(f"  4 verdict        : {decision}")
    print()


def mechanism(root: Path) -> None:
    print("=" * 76)
    print("MECHANISM - the same checks, with every intermediate value shown")
    print("=" * 76)
    for path, _ in ATTACKS:
        explain(path, root)


def main() -> None:
    root = workspace_root()
    print(f"workspace root : {root}\n")
    verdict_table(root)
    if "--short" not in sys.argv:
        mechanism(root)


if __name__ == "__main__":
    main()
