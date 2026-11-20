"""Runtime relocation preserves recovery journals and evaluation evidence."""
import json
from pathlib import Path

import pytest

from agent import storage, tracing
from agent.journal import Journal, rebuild
from agent.storage import RuntimePaths
from evals import run
from scripts.migrate_runtime import fingerprint, migrate


def legacy_project(tmp_path):
    old = tmp_path/'runs'
    workspace = old/'workspaces'/'case-one'
    workspace.mkdir(parents=True)
    (workspace/'source.py').write_text('answer = 42\n')
    (workspace/'empty').mkdir()
    (workspace/'link').symlink_to('source.py')
    journal = old/'old-run.jsonl'
    Journal(journal).append('run_started', schema=1, workspace=str(workspace),
                            task='inspect', messages=[dict(role='user', content='inspect')])
    return RuntimePaths(tmp_path), journal, workspace


def test_migration_preserves_journal_bytes_and_entire_workspace_tree(tmp_path):
    paths, journal, workspace = legacy_project(tmp_path)
    before_bytes = journal.read_bytes()
    before_tree = fingerprint(workspace)
    result = migrate(tmp_path)
    assert result['journals'] == result['workspaces'] == 1
    assert not (tmp_path/'runs').exists()
    moved = paths.journals/journal.name
    assert moved.read_bytes() == before_bytes
    assert fingerprint(paths.eval_workspaces/workspace.name) == before_tree
    assert rebuild(Journal(moved).events()).task == 'inspect'
    manifest = json.loads(Path(result['manifest']).read_text())
    assert manifest['status'] == 'complete'
    assert len(manifest['moves']) == 2
    assert migrate(tmp_path) == dict(journals=0, workspaces=0, manifest=None)


def test_all_conflicts_are_detected_before_any_data_is_moved(tmp_path):
    paths, journal, workspace = legacy_project(tmp_path)
    paths.journals.mkdir(parents=True)
    destination = paths.journals/journal.name
    destination.write_text('existing destination\n')
    before = journal.read_bytes()
    with pytest.raises(ValueError, match='Destination exists'):
        migrate(tmp_path)
    assert journal.read_bytes() == before
    assert workspace.is_dir()
    assert destination.read_text() == 'existing destination\n'
    assert not paths.eval_workspaces.exists()


def test_unrecognized_legacy_files_are_not_discarded(tmp_path):
    _, journal, workspace = legacy_project(tmp_path)
    (tmp_path/'runs'/'precious.txt').write_text('keep me')
    with pytest.raises(ValueError, match='Unexpected legacy entry'):
        migrate(tmp_path)
    assert journal.is_file() and workspace.is_dir()
    assert not (tmp_path/'.runtime').exists()


def test_legacy_paths_resolve_only_after_migration_and_preserve_unrelated_paths(tmp_path):
    paths, journal, workspace = legacy_project(tmp_path)
    assert paths.resolve_artifact(journal) == journal
    assert paths.resolve_artifact(workspace) == workspace
    migrate(tmp_path)
    assert paths.resolve_artifact(journal) == paths.journals/journal.name
    assert paths.resolve_artifact(workspace) == paths.eval_workspaces/workspace.name
    unrelated = tmp_path/'my-project'
    assert paths.resolve_artifact(unrelated) == unrelated
    assert paths.resolve_artifact(tmp_path/'runs'/'unknown.txt') == tmp_path/'runs'/'unknown.txt'


def test_old_eval_rows_and_traces_resolve_without_rewriting_evidence(tmp_path, monkeypatch):
    paths, journal, workspace = legacy_project(tmp_path)
    results = tmp_path/'results.jsonl'
    row = dict(journal=str(journal), workspace=str(workspace), metrics=dict(workspace=str(workspace)))
    results.write_text(json.dumps(row)+'\n')
    original_row = results.read_bytes()
    original_journal = journal.read_bytes()
    migrate(tmp_path)
    monkeypatch.setattr(run, 'PATHS', paths)
    monkeypatch.setattr(tracing, 'PATHS', paths)
    monkeypatch.setattr(storage, 'PATHS', paths)
    current = run.read_rows(results)[0]
    assert current['journal'] == str(paths.journals/journal.name)
    assert current['workspace'] == str(paths.eval_workspaces/workspace.name)
    assert current['metrics']['workspace'] == current['workspace']
    assert tracing.summary(tracing.load(journal))['workspace'] == current['workspace']
    assert storage.workspace_root(workspace) == paths.eval_workspaces/workspace.name
    assert results.read_bytes() == original_row
    assert (paths.journals/journal.name).read_bytes() == original_journal


def test_new_runs_and_workspaces_use_explicit_runtime_locations():
    from agent.cli import RUNS_DIR
    from agent.storage import PATHS
    from scripts import fixture
    assert RUNS_DIR == tracing.RUNS_DIR == PATHS.journals
    assert fixture.BUILD_DIR == PATHS.eval_workspaces
    assert PATHS.journals.name == 'journals'
    assert PATHS.eval_workspaces.name == 'eval-workspaces'
