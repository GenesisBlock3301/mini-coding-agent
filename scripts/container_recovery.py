"""Two-process, two-container recovery probe using mounted journal and workspace."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.cli import TOOLS_SYSTEM_PROMPT
from agent.journal import Journal, rebuild
from agent.loop import run_loop
from agent.model import ModelResponse
from agent.storage import JOURNALS_DIR, workspace_root, recorded_workspace
from agent.tools import TOOL_SCHEMAS

NAME = 'container_recovery.txt'
BEFORE = 'before\n'
AFTER = 'after\n'


def reply(text=None, call=False):
    calls = [dict(id='edit', type='function', function=dict(name='replace_text',
              arguments=json.dumps(dict(path=NAME, old=BEFORE, new=AFTER))))] if call else []
    return ModelResponse(text=text, model='scripted', finish_reason='tool_calls' if call else 'stop',
                         prompt_tokens=0, completion_tokens=0, total_tokens=0,
                         duration_ms=0, tool_calls=calls)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('crash', 'resume'))
    parser.add_argument('run_id')
    args = parser.parse_args()
    root = workspace_root()
    if not root.is_dir():
        parser.error(f'missing workspace: {root}')
    journal = Journal.create(JOURNALS_DIR, args.run_id) if args.phase == 'crash' else Journal.open(JOURNALS_DIR, args.run_id)
    target = root / NAME
    if args.phase == 'crash':
        if journal.path.exists():
            parser.error('run id already exists')
        target.write_text(BEFORE)
        print(f'journal: {journal.path}\nworkspace: {root}\ncrashing after edit', flush=True)
        run_loop([dict(role='system', content=TOOLS_SYSTEM_PROMPT),
                  dict(role='user', content='change before to after')],
                 root=root, tools=TOOL_SCHEMAS, journal=journal,
                 model_fn=lambda messages, tools=None: reply(call=True),
                 on_tool=lambda *unused: os._exit(137))
    else:
        state = rebuild(journal.events())
        if recorded_workspace(JOURNALS_DIR, args.run_id) != root.resolve():
            parser.error('workspace differs from recorded run')
        result = run_loop([], root=root, tools=TOOL_SCHEMAS, journal=journal,
                          resume=state, model_fn=lambda messages, tools=None: reply(text='done'))
        correct = target.read_text() == AFTER
        print(f'journal: {journal.path}\nworkspace: {root}\n'
              f'changed paths: {result.changed_paths}\n'
              f'recovered: {correct}; stop: {result.stop_reason}')
        return 0 if correct and result.stop_reason == 'answer' else 1
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
