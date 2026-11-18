"""Three-act local demo: verified edit, interrupted edit and comparison evidence."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='use the configured provider for act one')
    args = parser.parse_args()
    output = Path(tempfile.mkdtemp(prefix="mini-agent-demo-")) / 'results.jsonl'
    mode = 'live' if args.live else 'golden'
    print(f'ACT 1: provider={os.getenv("PROVIDER", "deepseek") if args.live else "scripted"}; '
          f'mode={mode}; result={output}', flush=True)
    command = [sys.executable, '-m', 'evals.run', '--mode', mode, '--cases',
               'tail_window', '--repeats', '1', '--output', str(output)]
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode:
        return result.returncode
    row = json.loads(output.read_text().splitlines()[0])
    print(f'journal: {row["journal"]}\nworkspace: {row["workspace"]}\n'
          f'budget: {row["budget"]}\nchanged paths: '
          f'{sorted(row.get("successful_writes", {}))}\n'
          f'independent verifier: {row["verdict"]}', flush=True)
    print('\nACT 2: controlled interruption and recovery', flush=True)
    result = subprocess.run([sys.executable, 'scripts/crash_demo.py', '--edit'], cwd=ROOT)
    if result.returncode:
        return result.returncode
    print('\nACT 3: registered Session 12 comparison', flush=True)
    for line in (ROOT / 'evals/session-12-report.md').read_text().splitlines()[:20]:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
