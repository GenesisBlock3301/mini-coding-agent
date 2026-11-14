"""Independent check for the tail_window fixture. (Session 7, D58)

    python -IB check.py <workspace-dir>

It reads the workspace from disk and exercises it. It never reads the run's
transcript: a model that edited nothing still describes a fix fluently, and
this file exists precisely to be unimpressed by that.

Run isolated (-I) and without bytecode (-B): isolated so the checker's own
directory cannot shadow a workspace module, and without bytecode so running
the check does not leave a __pycache__ behind that the changed-file rule
would then have to explain away.
"""

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check.py <workspace-dir>", file=sys.stderr)
        return 2

    workspace = Path(argv[1]).resolve()
    # The workspace has to be importable: report.py does `from stats import ...`,
    # which is a real import and not something importlib can fake per-file.
    sys.path.insert(0, str(workspace))

    try:
        import report
        import stats
    except Exception as exc:  # a syntax error in an edited file lands here
        print(f"FAIL import: {exc!r}")
        return 1

    failures: list[str] = []

    def check(label: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{label}: got {got!r}, want {want!r}")

    values = list(range(1, 11))

    check("tail(1..10, 3)", stats.tail(values, 3), [8, 9, 10])
    check("tail(1..10, 1)", stats.tail(values, 1), [10])
    check("tail([1, 2], 5)", stats.tail([1, 2], 5), [1, 2])
    check("tail(1..10, 0)", stats.tail(values, 0), [])

    # Collateral guard: mean was never broken and must not become so.
    check("mean([1, 2, 3])", stats.mean([1, 2, 3]), 2.0)
    check("mean([])", stats.mean([]), 0.0)

    week = report.weekly(values)
    check("len(weekly['days'])", len(week["days"]), 7)
    check("weekly['days']", week["days"], [4, 5, 6, 7, 8, 9, 10])
    check("weekly['average']", week["average"], 7.0)

    for failure in failures:
        print(f"FAIL {failure}")
    if failures:
        return 1
    print("PASS tail_window")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
