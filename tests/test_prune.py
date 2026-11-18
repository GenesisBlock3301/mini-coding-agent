"""Tests for scripts/prune_runs.py - the rules that stop a cleanup deleting evidence.

Session 10, Phase 4. Every test here is about a REFUSAL. The deleting half of
this script is three lines and nothing in it is interesting; what is worth
pinning is the set of runs it must never touch no matter what it was asked,
because a journal is the only durable record of what the agent did to someone's
files, and the file it most wants to delete - old, finished-looking, large - is
sometimes the one a crashed session is about to `--resume`.

So every test builds its own `runs/` under tmp_path. A test that reads the real
runs/ would be a test that can delete this project's own history, and the first
time it got the rules wrong it would take the evidence with it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc)


def _load_script(path: Path, name: str):
    """Import a scripts/*.py module by path - scripts/ is not a package.

    By path rather than by sys.path surgery: a test suite that mutates
    sys.path changes what every later import in the process means.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


prune = _load_script(PROJECT_ROOT / "scripts" / "prune_runs.py", "prune_runs")


# --------------------------------------------------------------------------
# Scaffolding: journals that look like the ones in runs/
# --------------------------------------------------------------------------


def name_at(days_ago: float, tag: str) -> str:
    """A run id as `new_run_id()` mints one: a LOCAL wall clock, then hex."""
    stamp = (NOW - timedelta(days=days_ago)).astimezone().strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{tag}.jsonl"


def make_run(
    runs: Path,
    days_ago: float,
    tag: str,
    *,
    finished: bool = True,
    dated: bool = True,
) -> Path:
    """One journal of the shape runs/ is full of, aged `days_ago`.

    The name and the `at` epochs agree, because they do in a real run. The
    tests that care which of the two was read say so by making them disagree.
    """
    began = NOW - timedelta(days=days_ago)
    started = {"t": "run_started", "task": "fix the window", "workspace": "ws"}
    if dated:
        started["at"] = began.timestamp()
    records = [
        started,
        {"t": "tool_intent", "at": began.timestamp() + 1, "name": "read_file"},
        {"t": "tool_outcome", "at": began.timestamp() + 2, "ok": True},
    ]
    if finished:
        records.append(
            {"t": "run_finished", "at": began.timestamp() + 3, "stop_reason": "answer"}
        )
    path = runs / name_at(days_ago, tag)
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )
    return path


@pytest.fixture
def runs(tmp_path) -> Path:
    directory = tmp_path / "runs"
    directory.mkdir()
    return directory


def deleted_by(output: str) -> set[str]:
    """The run ids the report's table marked DELETE."""
    return {
        line.split()[0] for line in output.splitlines() if line.endswith("DELETE")
    }


def names(directory: Path) -> set[str]:
    return {p.name for p in directory.glob("*.jsonl")}


# --------------------------------------------------------------------------
# The two refusals, which no flag may reach past
# --------------------------------------------------------------------------


def test_the_most_recent_run_is_never_deleted(runs):
    """Rule 3, under every filter at once.

    All three runs are months old and finished, `--keep 0` asks for no
    reprieve and `--older-than 1` catches everything. The newest still stays,
    because it is the one a killed session is about to `--resume` - and the
    refusal is checked before any filter, so there is no combination of flags
    that selects it.
    """
    doomed = [make_run(runs, 100, "aaaaaa"), make_run(runs, 50, "bbbbbb")]
    newest = make_run(runs, 10, "cccccc")

    verdicts = prune.plan(prune.collect(runs), keep=0, older_than=1, now=NOW)
    survivor = next(v for v in verdicts if v.run.path == newest)

    assert not survivor.deletes
    assert "resume" in survivor.keep
    # Its two neighbours went, so the refusal is what saved it and not the
    # filters quietly selecting nothing.
    assert [v.run.path for v in verdicts if v.deletes] == doomed


def test_an_unfinished_run_is_never_deleted(runs):
    """Rule 4. A journal with no `run_finished` record is not a leftover: it
    is the exact shape of file `--resume` exists for. Nine of them are sitting
    in this project's runs/ right now."""
    crashed = make_run(runs, 200, "aaaaaa", finished=False)
    make_run(runs, 199, "bbbbbb")
    make_run(runs, 1, "cccccc")

    verdicts = prune.plan(prune.collect(runs), older_than=30, now=NOW)
    survivor = next(v for v in verdicts if v.run.path == crashed)

    assert not survivor.deletes
    assert "unfinished" in survivor.keep


def test_a_journal_that_only_quotes_run_finished_is_still_unfinished(runs):
    """The reason the check parses JSON instead of grepping for a string.

    A journal holds the full text of every file the agent read, so a
    workspace file that happens to contain `run_finished` would persuade a
    substring check that a crashed run had ended - deleting the resumable run
    and keeping the finished one, exactly backwards.
    """
    path = runs / name_at(300, "aaaaaa")
    path.write_text(
        json.dumps({"t": "run_started", "at": 0.0, "task": "read the log parser"})
        + "\n"
        + json.dumps(
            {
                "t": "tool_outcome",
                "at": 1.0,
                "ok": True,
                "content": 'if record["t"] == "run_finished": ...',
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _started, finished = prune.scan(path)

    assert not finished


# --------------------------------------------------------------------------
# Dry run by default
# --------------------------------------------------------------------------


def test_a_dry_run_deletes_nothing_and_says_so(runs, capsys):
    """Rule 5. The flag is the consent; printing a table is not performing it."""
    before = {make_run(runs, 90 + n, f"aaaa{n:02}").name for n in range(4)}
    make_run(runs, 1, "newest")

    code = prune.main(["--runs", str(runs), "--older-than", "30"])
    output = capsys.readouterr().out

    assert code == 0
    assert deleted_by(output), "the dry run still has to say what it would take"
    assert "Dry run: nothing was deleted" in output
    assert before <= names(runs)


def test_apply_deletes_exactly_the_files_the_dry_run_listed(runs, capsys):
    """The dry run is only a promise if --apply keeps it.

    The promise is read back out of the printed table rather than re-derived
    from plan(): a report whose list differs from what the deletion does is
    worse than no report at all, and that gap is what this test looks for.
    """
    for n in range(6):
        make_run(runs, 90 - n * 10, f"aaaa{n:02}")
    before = names(runs)

    prune.main(["--runs", str(runs), "--keep", "2"])
    promised = deleted_by(capsys.readouterr().out)

    prune.main(["--runs", str(runs), "--keep", "2", "--apply"])

    assert promised, "nothing was promised, so nothing is being tested"
    assert names(runs) == before - {f"{stem}.jsonl" for stem in promised}


def test_nothing_is_selected_without_a_filter(runs):
    """Rule 1 as the default: a bare report is a report. The script has to be
    told which runs are expendable before any of them is."""
    for n in range(4):
        make_run(runs, 500 - n, f"aaaa{n:02}")

    verdicts = prune.plan(prune.collect(runs), now=NOW)

    assert not any(v.deletes for v in verdicts)
    # All but the last, which is the newest and has a refusal of its own.
    assert all("no filter" in v.keep for v in verdicts[:-1])


# --------------------------------------------------------------------------
# Files that are not journals, and journals that are not readable
# --------------------------------------------------------------------------


def test_a_torn_journal_is_kept_rather_than_read_as_finished(runs, capsys):
    """A crash can stop mid-record, mid-character even.

    The file is unreadable precisely because a process died holding it, which
    is the strongest possible argument for keeping it. It must not raise on
    the way to that conclusion either: one torn file would otherwise take the
    whole report down and prune nothing at all.
    """
    torn = runs / name_at(400, "aaaaaa")
    began = (NOW - timedelta(days=400)).timestamp()
    torn.write_bytes(
        json.dumps({"t": "run_started", "at": began, "task": "go"}).encode()
        + b"\n"
        + b'{"t": "tool_outcome", "ok": true, "content": "caf\xc3'  # cut mid-UTF-8
    )
    make_run(runs, 1, "newest")

    code = prune.main(["--runs", str(runs), "--older-than", "30", "--apply"])

    assert code == 0
    assert torn.exists()
    assert "UNFINISHED" in capsys.readouterr().out


def test_the_workspaces_directory_is_ignored(runs):
    """`runs/workspaces/` holds fixture copies of other people's projects, and
    a journal-shaped file under it is still not a journal. The glob does not
    descend; nothing here needs to know what those files are."""
    workspace = runs / "workspaces" / "tail_window"
    workspace.mkdir(parents=True)
    decoy = workspace / name_at(900, "aaaaaa")
    decoy.write_text("{}\n", encoding="utf-8")
    make_run(runs, 800, "bbbbbb")
    make_run(runs, 1, "cccccc")

    prune.main(["--runs", str(runs), "--older-than", "30", "--apply"])

    assert decoy.exists()
    assert (runs / "workspaces").is_dir()


def test_an_empty_runs_directory_is_not_an_error(runs, capsys):
    """A project that has never run the agent is in a normal state, not a
    broken one. Exit 0 and say zero."""
    code = prune.main(["--runs", str(runs)])

    assert code == 0
    assert "0 journal(s)" in capsys.readouterr().out


def test_an_undated_name_is_never_too_old(runs):
    """`--older-than` is a claim about a run's age, and a name that does not
    parse supports no such claim. Doubt resolves to keep."""
    stray = runs / "0000-handwritten.jsonl"
    stray.write_text(
        json.dumps({"t": "run_finished", "at": 0.0, "stop_reason": "answer"}) + "\n",
        encoding="utf-8",
    )
    make_run(runs, 1, "newest")

    verdicts = prune.plan(prune.collect(runs), older_than=1, now=NOW)
    survivor = next(v for v in verdicts if v.run.path == stray)

    assert not survivor.deletes
    assert survivor.run.started is None


# --------------------------------------------------------------------------
# What the filters actually measure
# --------------------------------------------------------------------------


def test_age_is_the_run_s_own_start_time_and_not_mtime(runs):
    """A resume appends to a journal and a copy or a restore rewrites mtime
    wholesale, so mtime answers "when was this file last touched" - a
    different question from the one --older-than asks.

    Here mtime lies in both directions at once, and is believed neither time.
    """
    old = make_run(runs, 100, "aaaaaa")
    recent = make_run(runs, 10, "bbbbbb")
    make_run(runs, 1, "cccccc")
    os.utime(old, (NOW.timestamp(), NOW.timestamp()))
    ancient = (NOW - timedelta(days=2000)).timestamp()
    os.utime(recent, (ancient, ancient))

    verdicts = prune.plan(prune.collect(runs), older_than=30, now=NOW)

    assert {v.run.path.name for v in verdicts if v.deletes} == {old.name}


def test_the_journal_outranks_the_name_when_the_two_disagree(runs):
    """The name is a local wall clock with no offset in it - `new_run_id()`
    calls `time.strftime()` - so it is only comparable to "now" on a machine
    in the timezone that wrote it. `run_started.at` is an epoch and has no
    such question, so it is what the age is read from.

    The name here claims yesterday; the record says a hundred days ago.
    """
    liar = make_run(runs, 100, "aaaaaa")
    renamed = liar.rename(runs / name_at(1, "aaaaaa"))
    make_run(runs, 0.5, "bbbbbb")

    verdicts = prune.plan(prune.collect(runs), older_than=30, now=NOW)

    assert [v.run.path.name for v in verdicts if v.deletes] == [renamed.name]


def test_the_name_is_the_fallback_when_the_record_has_no_timestamp(runs):
    """D94: absence is given a meaning rather than a crash. An old record
    with no `at` still has a name, and a name is a worse clock, not no clock.
    """
    legacy = make_run(runs, 100, "aaaaaa", dated=False)
    make_run(runs, 1, "bbbbbb")

    verdicts = prune.plan(prune.collect(runs), older_than=30, now=NOW)
    doomed = next(v for v in verdicts if v.run.path == legacy)

    assert doomed.deletes
    assert doomed.run.started is not None


def test_keep_counts_back_from_the_newest_run(runs):
    """`--keep N` is a position rule, not an age rule: it says how much
    history stays regardless of when it happened."""
    made = [make_run(runs, 90 - n * 10, f"aaaa{n:02}") for n in range(6)]

    verdicts = prune.plan(prune.collect(runs), keep=2, now=NOW)

    assert {v.run.path.name for v in verdicts if v.deletes} == {
        p.name for p in made[:4]
    }


def test_two_filters_must_both_agree_before_a_run_goes(runs):
    """An AND, not an OR. When two rules disagree about deleting someone's
    only copy, the conservative reading is the right one - and it is the one a
    human typing both flags means: "old AND beyond the last N"."""
    make_run(runs, 100, "aaaaaa")  # old, outside --keep: goes
    make_run(runs, 100, "bbbbbb")  # old, but inside --keep: stays
    make_run(runs, 1, "cccccc")

    verdicts = prune.plan(prune.collect(runs), keep=2, older_than=30, now=NOW)
    doomed = [v for v in verdicts if v.deletes]
    spared = next(v for v in verdicts if v.run.path.name.endswith("bbbbbb.jsonl"))

    assert [v.run.path.name for v in doomed] == [name_at(100, "aaaaaa")]
    assert "--keep" in spared.keep, "old enough to go, recent enough to stay"


# --------------------------------------------------------------------------
# Rule 2: a human runs this, or nobody does
# --------------------------------------------------------------------------


def test_importing_the_script_prunes_nothing(tmp_path):
    """Rule 2, tested the only way it can be: by giving an import something to
    destroy and watching it not.

    The script is copied where its own `.runtime/journals/` lands on a
    populated directory, then imported. A module that swept on import, or kept
    a size cap that tripped on its own, would empty it here.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    copy = scripts / "prune_runs.py"
    copy.write_bytes((PROJECT_ROOT / "scripts" / "prune_runs.py").read_bytes())
    planted = tmp_path / ".runtime" / "journals"
    planted.mkdir(parents=True)
    for n in range(3):
        make_run(planted, 900 - n, f"aaaa{n:02}")

    module = _load_script(copy, "prune_runs_on_import")

    assert module.RUNS == planted
    assert len(names(planted)) == 3


def test_apply_without_a_filter_is_a_usage_error(runs):
    """`--apply` alone is the typo of someone who meant to name a filter. It
    selects nothing, so performing it would be a silent no-op; the exit code
    is the only place left to say so."""
    make_run(runs, 900, "aaaaaa")
    make_run(runs, 1, "bbbbbb")

    with pytest.raises(SystemExit) as exit_info:
        prune.main(["--runs", str(runs), "--apply"])

    assert exit_info.value.code != 0
    assert len(names(runs)) == 2


def test_a_runs_directory_that_is_not_there_is_a_usage_error(tmp_path):
    """Pointing --runs at a typo must not report "0 journals, nothing to do".
    A quiet success is the wrong answer to a question nobody asked."""
    with pytest.raises(SystemExit) as exit_info:
        prune.main(["--runs", str(tmp_path / "nope")])

    assert exit_info.value.code != 0
