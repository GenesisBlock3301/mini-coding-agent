"""Move legacy runs/ into named runtime directories, preserving every byte.

    python scripts/migrate_runtime.py

Explicit only: imports and agent startup never move or delete user data.
Conflicts are checked before any moves. A checksum manifest remains under
.runtime/migrations/; interrupted moves can be continued by rerunning.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.storage import RuntimePaths


def fingerprint(path: Path) -> str:
    if path.is_symlink():
        payload = 'link:' + str(path.readlink())
    elif path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    else:
        payload = json.dumps({str(p.relative_to(path)): fingerprint(p)
                              for p in sorted(path.iterdir())}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def migrate(project: Path = ROOT) -> dict:
    paths = RuntimePaths(project.resolve())
    legacy = paths.project/'runs'
    if not legacy.exists():
        return dict(journals=0, workspaces=0, manifest=None)
    if legacy.is_symlink() or not legacy.is_dir():
        raise ValueError('Legacy runs must be a real directory.')
    moves = []
    counts = dict(journals=0, workspaces=0)
    for source in sorted(legacy.iterdir()):
        if source.name == 'workspaces' and source.is_dir() and not source.is_symlink():
            for workspace in sorted(source.iterdir()):
                moves.append((workspace, paths.eval_workspaces/workspace.name))
                counts['workspaces'] += 1
        elif source.is_file() and not source.is_symlink() and source.suffix == '.jsonl':
            moves.append((source, paths.journals/source.name))
            counts['journals'] += 1
        else:
            raise ValueError(f'Unexpected legacy entry; nothing moved: {source}')
    for source, destination in moves:
        if destination.exists() or destination.is_symlink():
            raise ValueError(f'Destination exists; nothing moved: {destination}')
    records = [dict(source=str(src), destination=str(dst), sha256=fingerprint(src))
               for src, dst in moves]
    manifests = paths.root/'migrations'
    manifests.mkdir(parents=True, exist_ok=True)
    manifest = manifests/f'{uuid.uuid4().hex}.json'
    data = dict(status='planned', moves=records, **counts)
    manifest.write_text(json.dumps(data, indent=2)+'\n')
    for record in records:
        source, destination = Path(record['source']), Path(record['destination'])
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        if fingerprint(destination) != record['sha256']:
            raise RuntimeError(f'Checksum mismatch after move: {destination}')
    # Only remove empty directories, never recursively delete legacy data.
    if (legacy/'workspaces').exists():
        (legacy/'workspaces').rmdir()
    legacy.rmdir()
    data['status'] = 'complete'
    manifest.write_text(json.dumps(data, indent=2)+'\n')
    return dict(**counts, manifest=str(manifest))


def main():
    print(json.dumps(migrate(), indent=2))


if __name__ == '__main__':
    main()
