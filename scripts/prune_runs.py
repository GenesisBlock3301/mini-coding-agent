"""What `.runtime/journals/` is allowed to lose, and the two refusals that outrank the ask.

    python scripts/prune_runs.py                         # report; deletes nothing
    python scripts/prune_runs.py --keep 40               # what that rule would drop
    python scripts/prune_runs.py --older-than 30 --apply

Step 8 of docs/session-10.md. `.runtime/journals/` only grows, and `*.jsonl` is gitignored,
so the growth is invisible in every view of this repository anyone normally
looks at: the first report you get is a disk that is full.

That is the case for the script, and it is also the case for being careful
with it, because a journal is not a log. It is the only durable record of what
an agent did to someone's files - Session 8's argument was precisely that a
run's own account of itself dies with the process while the journal does not -
and `--resume` reads it to finish a run that was killed.

So this is a report that can be told to delete, never a cleaner that can be
told to be careful:

  Deleting is explicit.  No filter selects nothing. `--apply` or it is a dry
                         run, and the dry run is the default.

  Nobody calls this.     No import hook, no startup sweep, no size cap that
                         trips on its own. It prunes when a human types it.

  Two runs are never     The newest, because that is the one someone is about
  for sale.              to `--resume`, and any run with no `run_finished`
                         record, because an unfinished run is exactly a
                         resumable one. Both refusals are checked BEFORE the
                         filters, so no filter - and no filter added later -
                         can reach past them.

Anything this script cannot read, it keeps: a journal it cannot parse, a name
it cannot date, a file torn where the crash left it. Doubt resolves to keep in
every case, because the two mistakes are not the same size. Keeping a file
that could have gone costs bytes; deleting one that should have stayed costs
the only copy.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.storage import RuntimePaths

RUNS = RuntimePaths(Path(__file__).resolve().parents[1]).journals

# Journals are named "<timestamp>-<6 hex>.jsonl", so a lexical sort is a
# chronological one. Both "the newest run" and "--keep N" stand on that, and
# that is all the name is trusted for - see `started_from_name` for why the
# age is read out of the journal instead.
NAME_STAMP = "%Y%m%dT%H%M%S"


@dataclass(frozen=True)
class Run:
    """One journal, reduced to the three facts the rules are stated in."""

    path: Path
    size: int
    started: datetime | None  # None when neither journal nor name dates it
    finished: bool  # a run_finished record was found


@dataclass(frozen=True)
class Verdict:
    run: Run
    keep: str | None  # the reason it stays, or None to delete

    @property
    def deletes(self) -> bool:
        return self.keep is None


# --------------------------------------------------------------------------
# Reading a journal without trusting it
# --------------------------------------------------------------------------


def scan(path: Path) -> tuple[datetime | None, bool]:
    """(when the run began, whether it ever ended), read from the journal.

    One pass for both, because they are in the same file and the second
    answer can be on the last line.

    Parsed, never grepped. A journal quotes the full contents of every file
    the agent read, so a workspace file containing the text `run_finished`
    would make a substring check call a resumable run finished - the one
    mistake this script must not make.

    Unreadable, torn or unparseable answers "not finished", which is the
    answer that keeps the file.
    """
    started: datetime | None = None
    finished = False
    try:
        # errors="replace" because a crash can tear a record mid-character.
        # That turns an undecodable tail into a line json refuses, rather
        # than an exception thrown from the middle of a for loop.
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("t") == "run_started" and started is None:
                    # The FIRST one. A resumed run journals a `run_started`
                    # per process, and the run began at the earliest of them.
                    started = _at(record.get("at"))
                elif record.get("t") == "run_finished":
                    finished = True
                    break
    except OSError:
        return None, False
    return started, finished


def _at(value: object) -> datetime | None:
    """An `at` epoch as a datetime, or None when the record has no usable one.

    Every journal in `.runtime/journals/` today carries a numeric `at` on `run_started`.
    The check is here because D94 says an older shape gets a meaning rather
    than a crash, and the meaning is: fall back to the name, and if that
    cannot be dated either, the run has no age and no `--older-than` can
    select it.
    """
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    return None


def started_from_name(path: Path) -> datetime | None:
    """The run's start time as its filename records it. The fallback.

    Second choice, and the reason is a trap worth writing down: `new_run_id()`
    stamps the name with `time.strftime()`, which is LOCAL time with no offset
    in it. The name looks like a UTC timestamp and is not one, so an age
    computed from it is wrong by this machine's offset and ambiguous across a
    DST fall-back. The `run_started` record carries an epoch, which has
    neither problem, and this script opens every journal anyway.

    Not mtime, in either case: a resume appends to a journal and a copy or a
    restore rewrites mtime wholesale, so mtime answers "when was this file
    last touched", which is a different question from the one `--older-than`
    asks.
    """
    stamp = path.name.split("-", 1)[0]
    try:
        return datetime.strptime(stamp, NAME_STAMP).astimezone()
    except ValueError:
        return None


def collect(runs_dir: Path) -> list[Run]:
    """Every journal in `runs_dir`, oldest first.

    `glob` and not `rglob`: only direct journal files are candidates.
    Nested directories are never inspected or selected for deletion.
    """
    runs = []
    for path in sorted(runs_dir.glob("*.jsonl")):
        started, finished = scan(path)
        runs.append(
            Run(
                path=path,
                size=path.stat().st_size,
                started=started or started_from_name(path),
                finished=finished,
            )
        )
    return runs


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------


def _keep_reason(
    run: Run,
    *,
    newest: Path | None,
    rank: int,
    keep: int | None,
    older_than: float | None,
    cutoff: datetime | None,
) -> str | None:
    """Why this run stays, or None when nothing argues for it.

    The order of these branches IS the policy. The two refusals come first and
    take no arguments, so they cannot be widened by a flag; the filters are
    asked afterwards and only ever narrow what is left.
    """
    if run.path == newest:
        return "newest - someone may --resume it"
    if not run.finished:
        return "unfinished - resumable"
    if keep is None and cutoff is None:
        return "no filter given"
    if keep is not None and rank <= keep:
        return f"within --keep {keep}"
    if cutoff is not None and run.started is None:
        # An undated name cannot be shown to be old, so --older-than never
        # selects it. `--keep` still can: that rule counts positions, not days.
        return "undated name"
    if cutoff is not None and run.started > cutoff:
        return f"younger than {older_than:g}d"
    return None


def plan(
    runs: list[Run],
    *,
    keep: int | None = None,
    older_than: float | None = None,
    now: datetime | None = None,
) -> list[Verdict]:
    """Decide every run, oldest first. Touches nothing.

    Two filters are an AND, not an OR: a run goes only when every rule the
    human named agrees it may. The conservative reading is the right one when
    the disagreement is about deleting someone's only copy.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=older_than) if older_than is not None else None
    newest = runs[-1].path if runs else None
    total = len(runs)

    return [
        Verdict(
            run=run,
            keep=_keep_reason(
                run,
                newest=newest,
                rank=total - index,  # 1 is the newest
                keep=keep,
                older_than=older_than,
                cutoff=cutoff,
            ),
        )
        for index, run in enumerate(runs)
    ]


def delete(verdicts: list[Verdict]) -> int:
    """Unlink the selected journals. Returns the bytes freed.

    No `missing_ok`, no swallowed OSError: a delete that fails is something to
    see, not something to tally.
    """
    freed = 0
    for verdict in verdicts:
        verdict.run.path.unlink()
        freed += verdict.run.size
    return freed


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


def human(size: float) -> str:
    for unit in ("B", "K", "M"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}G"


def _age(run: Run, now: datetime) -> str:
    if run.started is None:
        return "-"
    return f"{(now - run.started).days}d"


def report(
    verdicts: list[Verdict], *, runs_dir: Path, now: datetime, applying: bool
) -> None:
    doomed = [v for v in verdicts if v.deletes]
    total_size = sum(v.run.size for v in verdicts)

    print("=" * 78)
    print(
        f"PRUNE RUNS  -  {len(verdicts)} journal(s), {human(total_size)} in {runs_dir}"
    )
    print("=" * 78)
    print(f"{'run':24} {'size':>6} {'age':>5}  {'state':11} verdict")
    print("-" * 78)
    for verdict in verdicts:
        run = verdict.run
        print(
            f"{run.path.stem:24} {human(run.size):>6} {_age(run, now):>5}  "
            f"{('finished' if run.finished else 'UNFINISHED'):11} "
            f"{'DELETE' if verdict.deletes else 'keep: ' + verdict.keep}"
        )

    print()
    # Newest first, so the two refusals head the list in the order they are
    # argued for in the docstring.
    for reason, count in Counter(
        v.keep for v in reversed(verdicts) if not v.deletes
    ).items():
        print(f"  {count:>4} kept: {reason}")
    freed = sum(v.run.size for v in doomed)
    verb = "to delete" if applying else "would be deleted"
    print(f"  {len(doomed):>4} {verb}, {human(freed)}")
    if not applying:
        print()
        print("  Dry run: nothing was deleted. Add --apply to do it for real.")


# --------------------------------------------------------------------------
# Entry point. Nothing above runs on import, and nothing calls main() but a
# human at a terminal: this is rule 2, enforced by there being no other caller.
# --------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="prune_runs.py",
        description="Report on .runtime/journals/, and delete journals only when told to.",
    )
    parser.add_argument(
        "--older-than", type=float, metavar="DAYS",
        help="consider runs that started more than DAYS days ago",
    )
    parser.add_argument(
        "--keep", type=int, metavar="N",
        help="consider every run but the N most recent",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually delete; without it this is a dry run",
    )
    parser.add_argument(
        "--runs", type=Path, default=RUNS, metavar="DIR",
        help="the directory of journals (default: the project's .runtime/journals/)",
    )
    args = parser.parse_args(argv)

    # Every non-zero exit this script has is one of these: a mistyped flag, a
    # directory that is not there, or an --apply that selects nothing. A run
    # that deletes nothing because nothing qualified is a successful report.
    if not args.runs.is_dir():
        parser.error(f"no such directory: {args.runs}")
    if args.older_than is not None and args.older_than < 0:
        parser.error("--older-than takes a number of days, not a negative one")
    if args.keep is not None and args.keep < 0:
        parser.error("--keep takes a count, not a negative one")
    if args.apply and args.older_than is None and args.keep is None:
        parser.error(
            "--apply selects nothing without --older-than or --keep; "
            "say what to prune"
        )

    now = datetime.now(timezone.utc)
    verdicts = plan(
        collect(args.runs), keep=args.keep, older_than=args.older_than, now=now
    )
    report(verdicts, runs_dir=args.runs, now=now, applying=args.apply)

    if args.apply:
        doomed = [v for v in verdicts if v.deletes]
        freed = delete(doomed)
        print()
        print(f"  {len(doomed)} journal(s) deleted, {human(freed)} freed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
