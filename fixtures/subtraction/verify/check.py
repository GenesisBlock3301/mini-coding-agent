"""Exercise the edited calculator independently of the agent's answer."""

from pathlib import Path
import sys


def main(argv):
    if len(argv) != 2:
        print("usage: check.py <workspace-dir>", file=sys.stderr)
        return 2
    sys.path.insert(0, str(Path(argv[1]).resolve()))
    try:
        import calculator

        failures = []
        for name, a, b, expected in (
            ("subtract", 8, 3, 5),
            ("subtract", 3, 8, -5),
            ("subtract", -4, -2, -2),
            ("subtract", 0, 5, -5),
            ("subtract", 5, 0, 5),
            ("subtract", 4, 4, 0),
            ("subtract", 2.5, 0.5, 2.0),
            ("add", 8, 3, 11),
            ("add", -4, 2, -2),
            ("add", 0, 5, 5),
        ):
            got = getattr(calculator, name)(a, b)
            if got != expected:
                failures.append(f"{name}({a}, {b}): got {got!r}, want {expected!r}")
    except Exception as exc:
        print(f"FAIL calculator: {exc!r}")
        return 1
    for failure in failures:
        print(f"FAIL {failure}")
    if failures:
        return 1
    print("PASS subtraction")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
