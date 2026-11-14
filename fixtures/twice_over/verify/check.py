"""Independent check for the twice_over fixture. (Session 7, D58)

    python -IB check.py <workspace-dir>

The international rule is the half that was already correct. Half of these
assertions exist to catch a fix applied to the wrong function - which is what
a model does when a refused anchor pushes it toward the other match.
"""

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check.py <workspace-dir>", file=sys.stderr)
        return 2

    workspace = Path(argv[1]).resolve()
    sys.path.insert(0, str(workspace))

    try:
        import shipping
    except Exception as exc:
        print(f"FAIL import: {exc!r}")
        return 1

    failures: list[str] = []

    def check(label: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{label}: got {got!r}, want {want!r}")

    # The boundary that was wrong.
    check("domestic_fee(10)", shipping.domestic_fee(10), 12.0)
    check("domestic_fee(9.99)", shipping.domestic_fee(9.99), 5.0)
    check("domestic_fee(10.5)", shipping.domestic_fee(10.5), 12.0)
    check("domestic_fee(0)", shipping.domestic_fee(0), 5.0)

    # The boundary that was right, and must stay right.
    check("international_fee(10)", shipping.international_fee(10), 10.0)
    check("international_fee(10.5)", shipping.international_fee(10.5), 24.0)
    check("international_fee(9.99)", shipping.international_fee(9.99), 10.0)

    for failure in failures:
        print(f"FAIL {failure}")
    if failures:
        return 1
    print("PASS twice_over")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
