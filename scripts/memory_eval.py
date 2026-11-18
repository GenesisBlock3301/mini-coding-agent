"""Paired, interleaved memory check over all fixture cases (offline by default)."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import hashlib
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.budget import Budget, Pricing
from agent.storage import JOURNALS_DIR
from evals import run
from scripts import fixture


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--memory-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists')
    data = args.memory_file.read_bytes()
    from agent.memory import MAX_BYTES
    if len(data) > MAX_BYTES:
        parser.error('memory exceeds the configured size cap')
    memory = data.decode('utf-8')
    version = hashlib.sha256(data).hexdigest()
    names = fixture.list_cases()
    if len(names) != 20:
        parser.error(f'expected 20 cases, found {len(names)}')
    code = run.code_version()
    rows = []
    for name in names:
        case = fixture.load_case(name)
        for arm, text in (('empty', ''), ('memory', memory)):
            run_id = f'memory-{name}-{arm}-{uuid.uuid4().hex[:8]}'
            workspace = fixture.build(case)
            budget = replace(Budget.from_env(pricing=Pricing.free()),
                             max_steps=case.max_steps or 12, max_seconds=120,
                             context_window=65536)
            request = dict(case=name, mode='golden', run_id=run_id,
                           workspace=str(workspace), journal=str(JOURNALS_DIR / f'{run_id}.jsonl'),
                           budget=asdict(budget), provider='scripted', model='scripted',
                           timeout=120, check_timeout=5, memory_text=text,
                           metadata=dict(suite=args.output.stem, arm=arm, repeat=1,
                                         code=code, memory_file_sha256=version))
            row = run.isolated_run(request)
            run.append_row(args.output, row)
            rows.append(row)
            print(f'{name} {arm}: {row["verdict"]}', flush=True)
    print(f'Results: {args.output}')
    for arm in ('empty', 'memory'):
        group = [row for row in rows if row['arm'] == arm]
        print(f'{arm}: {sum(row["verdict"] == "pass" for row in group)}/{len(group)} pass; '
              f'one-memory-version: {len({row["memory"]["sha256"] for row in group}) == 1}')
    print(run.render_summary(rows))
    return 0 if all(row['verdict'] == 'pass' for row in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
