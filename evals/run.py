"""Independent evaluations, with fresh workspaces and durable result rows.

    python -m evals.run --mode golden --repeats 1
    python -m evals.run --mode live --cases tail_window --repeats 1
    python -m evals.run --summary evals/results/<suite>.jsonl

Golden tests the harness; only live measures model quality. No agent capability
is added here. Checkers receive the final answer on stdin, never an answer key.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, replace
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from agent.budget import Budget, Pricing
from agent.cli import TOOLS_SYSTEM_PROMPT
from agent.journal import Journal
from agent.loop import run_loop
from agent.model import ModelConfig, ModelError, ModelResponse, call_model
from agent.retry import RetryPolicy
from agent.tools import TOOL_SCHEMAS
from agent import tracing
from agent.storage import JOURNALS_DIR, PATHS
from scripts import fixture

ROOT = Path(__file__).resolve().parents[1]
MODES = ('golden', 'live', 'noop', 'wrong_file', 'double_edit', 'half_edit')
MAX_OUTPUT_TOKENS = 2048


class EvalJournal(Journal):
    """Attach identity to the existing start record without a phantom segment."""

    def __init__(self, path: Path, identity: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(path)
        self.identity = identity

    def append(self, event_type: str, **payload):
        if event_type == 'run_started':
            payload['evaluation'] = self.identity
        super().append(event_type, **payload)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def code_version() -> dict:
    """Include dirty/untracked implementation bytes; a SHA alone hides local work."""
    sha = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True)
    status = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT, capture_output=True, text=True)
    files = [p for folder in ('agent', 'evals', 'scripts') for p in (ROOT/folder).rglob('*.py')]
    return dict(sha=sha.stdout.strip() or None, dirty=bool(status.stdout.strip()),
                digest=digest({str(p.relative_to(ROOT)): p.read_text() for p in sorted(files)}))


def fixture_version(case) -> dict:
    return dict(template=digest(fixture.hash_tree(case.template)),
                spec=digest(json.loads((case.directory/'case.json').read_text())),
                checker=hashlib.sha256(case.check_script.read_bytes()).hexdigest())


def response(text=None, calls=()) -> ModelResponse:
    return ModelResponse(text=text, model='scripted', finish_reason='tool_calls' if calls else 'stop',
                         prompt_tokens=0, completion_tokens=0, total_tokens=0,
                         duration_ms=0.0, tool_calls=list(calls))


def scripted_model(case, mode='golden'):
    """Scripted model in the style of fault_matrix.py, through the real tools."""
    actions = []
    for path in sorted(p.relative_to(case.template).as_posix()
                       for p in case.template.rglob('*') if p.is_file()
                       and not any(part in fixture.IGNORED_NAMES for part in p.relative_to(case.template).parts)):
        actions.append(dict(name='read_file', arguments=dict(path=path)))
    actions.extend(case.probes)
    edits = list(case.edits)
    if mode == 'noop':
        actions = []
        edits = []
    elif mode == 'half_edit':
        edits = edits[:1]
    for edit in edits:
        actions.append(dict(name='replace_text', arguments=edit))
    if mode == 'double_edit' and edits:
        # A SECOND successful edit reverses the first. A repeated rejected
        # call is not a double write, and must not be graded as one.
        edit = edits[0]
        actions.append(dict(name='replace_text', arguments=dict(
            path=edit['path'], old=edit['new'], new=edit['old'])))
    if mode == 'wrong_file':
        path = next(p for p in sorted(case.template.rglob('*'))
                    if p.is_file() and p.relative_to(case.template).as_posix() not in case.allowed_changed)
        old = path.read_text()
        actions.append(dict(name='replace_text', arguments=dict(
            path=path.relative_to(case.template).as_posix(), old=old, new=old+'\n# unrelated edit\n')))
    calls = [dict(id=f'call_{i}', type='function', function=dict(
        name=action['name'], arguments=json.dumps(action['arguments'])))
        for i, action in enumerate(actions)]
    replies = iter([response(calls=calls), response(text=case.answer)] if calls
                   else [response(text=case.answer if mode != 'noop' else 'Done.')])
    return lambda messages, tools=None: next(replies)


def grade(case, workspace: Path, trace, answer: str, stop_reason: str | None) -> dict:
    report = fixture.verify(case, workspace, answer=answer)
    outcomes = trace.of('tool_outcome')
    errors = Counter(o.get('error_code') for o in outcomes if o.get('error_code'))
    writes = Counter(o['changed_path'] for o in outcomes if o.get('changed_path'))
    missing = sorted(set(case.required_errors) - errors.keys())
    intents = {e['call_id']: e for e in trace.of('tool_intent')}
    for probe, code in zip(case.probes, case.required_errors):
        if not any(o.get('error_code') == code
                   and intents.get(o.get('call_id'), {}).get('name') == probe['name']
                   and json.loads(intents[o['call_id']]['arguments']) == probe['arguments']
                   for o in outcomes):
            missing.append(code)
    missing = sorted(set(missing))
    escaped = []
    for outcome in outcomes:
        intent = intents.get(outcome.get('call_id'), {})
        path = json.loads(intent.get('arguments') or '{}').get('path')
        if outcome.get('ok') and isinstance(path, str):
            target = (workspace/path).resolve()
            if not target.is_relative_to(workspace.resolve()):
                escaped.append(path)
    reasons = []
    if report.unexpected:
        reasons.append('unexpected_change')
    if not report.check_ok:
        reasons.append('double_edit' if any(n > 1 for n in writes.values()) else 'check_failed')
    if missing:
        reasons.append('missing_refusal')
    if escaped:
        reasons.append('boundary_breach')
    if stop_reason != 'answer':
        reasons.append('unexpected_stop')
    timeout = report.check_output.startswith('check timed out after ')
    return dict(verdict='error' if timeout else ('fail' if reasons else 'pass'),
                failures=['checker_timeout'] if timeout else reasons,
                check_ok=report.check_ok, check_output=report.check_output,
                changed=list(report.changes.all), unexpected=list(report.unexpected),
                missing_errors=missing, successful_writes=dict(writes))


def trace_metrics(trace) -> dict:
    """Read disk even on errors: partial model usage must not disappear."""
    metrics = tracing.summary(trace)
    if not metrics['finished']:
        results = trace.of('model_result')
        for key in ('prompt_tokens', 'completion_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'):
            metrics[key] = sum(r.get('usage', {}).get(key, 0) for r in results)
        metrics['total_tokens'] = metrics['prompt_tokens'] + metrics['completion_tokens']
    return metrics


def run_case(case, *, mode, run_id, workspace, journal_path, budget, config=None,
             model_fn=None, timeout=120, check_timeout=5, metadata=None,
             system_prompt=TOOLS_SYSTEM_PROMPT) -> dict:
    started = time.monotonic()
    base = dict(schema=1, case=case.name, category=case.category, mode=mode,
                run_id=run_id, workspace=str(workspace), journal=str(journal_path),
                fixture=fixture_version(case), budget=asdict(budget),
                prompt=dict(version='cli-tools-s11-v1' if system_prompt == TOOLS_SYSTEM_PROMPT else 's12-boundaries-v1', digest=digest(system_prompt)),
                tools_digest=digest(TOOL_SCHEMAS), retry='off', temperature='provider_default',
                max_output_tokens=MAX_OUTPUT_TOKENS,
                requested_model=config.model if config else 'scripted',
                model=config.model if config else 'scripted',
                provider=config.provider.name if config else 'scripted',
                endpoint=config.base_url if config else None,
                timeout_s=timeout, check_timeout_s=check_timeout, **(metadata or {}))
    # Kept in the start record too, so an orphan journal identifies its case.
    journal = EvalJournal(journal_path, dict(case=case.name, run_id=run_id,
                          suite=base.get('suite'), variant=mode, repeat=base.get('repeat')))
    spent = 0.0
    def live_model(messages, *, tools=None):
        nonlocal spent
        # Conservative admission estimate: UTF-8 bytes (rather than chars/4)
        # for the full request plus output cap. Prices and caps are in every row.
        request_tokens = len(json.dumps(dict(messages=messages, tools=tools)).encode()) + 512
        reserve = (request_tokens * max(budget.pricing.input_cache_hit_per_m,
                                        budget.pricing.input_cache_miss_per_m)
                   + MAX_OUTPUT_TOKENS * budget.pricing.output_per_m) / 1_000_000
        if budget.max_spend is not None and spent + reserve > budget.max_spend:
            raise ModelError('suite_spend', 'Insufficient suite spend reserve for next request.',
                             retryable=False, safe_to_retry=True)
        result = call_model(messages, tools=tools, config=config, max_tokens=MAX_OUTPUT_TOKENS)
        spent += budget.pricing.cost(cache_hit=result.prompt_cache_hit_tokens,
                                     cache_miss=result.prompt_cache_miss_tokens or
                                     (result.prompt_tokens-result.prompt_cache_hit_tokens),
                                     output=result.completion_tokens)
        return result
    old_timeout = fixture.CHECK_TIMEOUT_S
    fixture.CHECK_TIMEOUT_S = check_timeout
    try:
        result = run_loop([dict(role='system', content=system_prompt),
                           dict(role='user', content=case.task)], root=workspace,
                          budget=budget, journal=journal, retry=RetryPolicy.off(),
                          model_fn=model_fn or (live_model if mode == 'live' else scripted_model(case, mode)))
        trace = tracing.load(journal_path)
        base.update(grade(case, workspace, trace, result.text or '', result.stop_reason))
        base['answer'] = result.text
    except ModelError as exc:
        base.update(verdict='error', failures=[exc.kind], error=exc.user_message,
                    fatal=exc.kind in ('auth', 'bad_request', 'suite_spend'),
                    # A timed-out or disconnected request may have been billed.
                    cost_uncertain=not exc.safe_to_retry)
    except Exception as exc:
        base.update(verdict='error', failures=['runner_error'], error=f'{type(exc).__name__}: {exc}')
    finally:
        fixture.CHECK_TIMEOUT_S = old_timeout
    metrics = trace_metrics(tracing.load(journal_path))
    base['metrics'] = metrics
    base['model'] = (metrics.get('models') or [base['model']])[-1]
    if mode == 'live' and metrics.get('requests_unanswered'):
        base.setdefault('cost_uncertain', True)
    base['duration_s'] = round(time.monotonic() - started, 4)
    base['cost_usd'] = spent if mode == 'live' else 0.0
    return base


def render_summary(rows: list[dict]) -> str:
    groups = defaultdict(list)
    for row in rows:
        groups[(row['case'], row['mode'])].append(row)
    lines = ['case / mode | pass / runs | result | tokens (min–max) | seconds (min–max)',
             '--- | --- | --- | --- | ---']
    for (name, mode), group in sorted(groups.items()):
        passes = sum(r['verdict'] == 'pass' for r in group)
        verdict = 'pass' if passes == len(group) else 'flaky' if passes else 'fail'
        tokens = [r.get('metrics', {}).get('total_tokens') or 0 for r in group]
        durations = [r['duration_s'] for r in group]
        lines.append(f'{name} / {mode} | {passes}/{len(group)} | {verdict} | '
                     f'{min(tokens)}–{max(tokens)} | {min(durations):.2f}–{max(durations):.2f}')
    passes = sum(r['verdict'] == 'pass' for r in rows)
    metrics = [r.get('metrics', {}) for r in rows]
    lines += ['', f'Success: {passes}/{len(rows)} ({passes/len(rows):.0%})' if rows else 'No runs.',
              f'Errors: {sum(r["verdict"] == "error" for r in rows)}; '
              f'unexpected changes: {sum(bool(r.get("unexpected")) for r in rows)}',
              f'Tool calls: {sum(m.get("tool_calls") or 0 for m in metrics)}; '
              f'model requests: {sum((m.get("requests") or 0) + (m.get("requests_unanswered") or 0) for m in metrics)}; '
              f'tokens: {sum(m.get("total_tokens") or 0 for m in metrics)}',
              f'Duration: {sum(r["duration_s"] for r in rows):.2f}s; '
              f'estimated cost: ${sum(r.get("cost_usd") or 0 for r in rows):.6f}',
              f'Runs with uncertain additional cost: {sum(bool(r.get("cost_uncertain")) for r in rows)}',
              'Stop reasons: '+json.dumps(dict(Counter(m.get('stop_reason') or 'error' for m in metrics)), sort_keys=True)]
    return '\n'.join(lines)


def read_rows(path: Path) -> list[dict]:
    # Fail loudly on torn results rather than quietly skipping measurements.
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for row in rows:
        for key in ('journal', 'workspace'):
            if row.get(key):
                row[key] = str(PATHS.resolve_artifact(row[key]))
        if row.get('metrics', {}).get('workspace'):
            row['metrics']['workspace'] = str(PATHS.resolve_artifact(row['metrics']['workspace']))
    return rows


def append_row(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, sort_keys=True)+'\n')
        handle.flush()
        os.fsync(handle.fileno())


def worker(request: dict) -> dict:
    case = fixture.load_case(request['case'])
    config = ModelConfig.from_env() if request['mode'] == 'live' else None
    values = dict(request['budget'])
    values['pricing'] = Pricing(**values['pricing']) if values['pricing'] else None
    return run_case(case, mode=request['mode'], run_id=request['run_id'],
                    workspace=Path(request['workspace']), journal_path=Path(request['journal']),
                    budget=Budget(**values), config=config, timeout=request['timeout'],
                    check_timeout=request['check_timeout'], metadata=request['metadata'],
                    system_prompt=request.get('system_prompt', TOOLS_SYSTEM_PROMPT))


def isolated_run(request: dict) -> dict:
    """Fork a dedicated Unix worker, avoiding ten repeated interpreter imports.

    The sequential CLI starts no threads or HTTP clients before this point.
    Every worker gets its own session/process group, workspace and journal;
    checks and model calls still run through the same worker() implementation.
    """
    started = time.monotonic()
    context = multiprocessing.get_context('fork')
    receive, send = context.Pipe(duplex=False)

    def child_main():
        receive.close()
        os.setsid()
        try:
            send.send(worker(request))
        except Exception:
            # Do not forward arbitrary exception/request content or credentials.
            pass
        finally:
            send.close()

    child = context.Process(target=child_main)
    try:
        child.start()
        send.close()
        if not receive.poll(request['timeout']):
            raise TimeoutError
        row = receive.recv()
        child.join(timeout=max(0, request['timeout']-(time.monotonic()-started)))
        if child.is_alive():
            raise TimeoutError
        if child.exitcode:
            raise RuntimeError
        return row
    except TimeoutError:
        reason = 'case_timeout'
    except Exception as exc:
        reason = f'worker_error: {type(exc).__name__}'
    finally:
        if child.pid is not None and child.is_alive():
            # Kill descendants (including a stuck checker), then reap.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                child.kill()  # deadline beat the child's setsid()
            child.join()
        receive.close()
        send.close()
    trace = tracing.load(Path(request['journal']))
    case = fixture.load_case(request['case'])
    return dict(schema=1, case=case.name, category=case.category, mode=request['mode'],
                run_id=request['run_id'], journal=request['journal'], workspace=request['workspace'],
                fixture=fixture_version(case), budget=request['budget'],
                prompt=dict(version='cli-tools-s11-v1' if request.get('system_prompt', TOOLS_SYSTEM_PROMPT) == TOOLS_SYSTEM_PROMPT else 's12-boundaries-v1',
                            digest=digest(request.get('system_prompt', TOOLS_SYSTEM_PROMPT))),
                tools_digest=digest(TOOL_SCHEMAS), retry='off', temperature='provider_default',
                max_output_tokens=MAX_OUTPUT_TOKENS,
                provider=request['provider'], model=request['model'], requested_model=request['model'], endpoint=request.get('endpoint'),
                timeout_s=request['timeout'], check_timeout_s=request['check_timeout'],
                verdict='error', failures=[reason], cost_usd=None,
                cost_uncertain=request['mode'] == 'live',
                duration_s=round(time.monotonic()-started, 4), metrics=trace_metrics(trace),
                **request['metadata'])


def run_suite(names, *, mode, repeats, output, timeout, check_timeout, max_spend,
              prices=None, executor=isolated_run) -> list[dict]:
    cases = [fixture.load_case(name) for name in names]  # validate before paying
    if len(set(names)) != len(names):
        raise ValueError('Duplicate case names.')
    config = ModelConfig.from_env() if mode == 'live' else None
    pricing = (prices or (Pricing.free() if config.provider.free else Pricing.from_env())) if config else Pricing.free()
    if pricing is None or any(not math.isfinite(p) or p < 0 for p in asdict(pricing).values()):
        raise ValueError('Live spend ceiling requires configured prices or --prices HIT MISS OUTPUT (USD per million tokens).')
    if config and not config.provider.free and pricing.worst_per_token == 0:
        raise ValueError('A hosted provider requires nonzero prices to enforce a spend ceiling.')
    if output.exists():
        raise ValueError('Result file already exists; choose a new file. Completed rows are never overwritten.')
    suite = output.stem
    code = code_version()
    spent = 0.0
    rows = []
    for case in cases:
        for repeat in range(1, repeats+1):
            if spent >= max_spend:
                print('Suite stopped: spend ceiling reached.', file=sys.stderr)
                return rows
            run_id = f'{suite}-{case.name}-{mode}-{repeat}-{uuid.uuid4().hex[:8]}'
            workspace = fixture.build(case)
            budget = replace(Budget.from_env(pricing=pricing), max_steps=case.max_steps or 12,
                             max_seconds=timeout, max_spend=max_spend-spent,
                             context_window=(int(os.environ['CONTEXT_WINDOW']) if os.getenv('CONTEXT_WINDOW')
                                             else config.provider.context_window if config else 65536))
            request = dict(case=case.name, mode=mode, run_id=run_id, workspace=str(workspace),
                           journal=str(JOURNALS_DIR/f'{run_id}.jsonl'), budget=asdict(budget),
                           provider=config.provider.name if config else 'scripted',
                           model=config.model if config else 'scripted',
                           endpoint=config.base_url if config else None,
                           timeout=timeout, check_timeout=check_timeout,
                           metadata=dict(suite=suite, repeat=repeat, code=code))
            row = executor(request)
            append_row(output, row)
            rows.append(row)
            print(f'{case.name} {repeat}/{repeats}: {row["verdict"]} '
                  f'{",".join(row.get("failures", []))}', flush=True)
            spent += row.get('cost_usd') or 0.0
            if row.get('fatal') or row.get('cost_uncertain'):
                print('Suite stopped: fatal provider error or uncertain spend; completed rows kept.', file=sys.stderr)
                return rows
    return rows


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be finite and positive')
    return number


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be positive')
    return number


def main(argv=None) -> int:
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=MODES, default='golden')
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--repeats', type=positive_int, default=3)
    parser.add_argument('--timeout', type=positive, default=120)
    parser.add_argument('--check-timeout', type=positive, default=5)
    parser.add_argument('--max-spend', type=positive, default=1.0)
    parser.add_argument('--prices', nargs=3, type=float, metavar=('HIT', 'MISS', 'OUTPUT'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--summary', type=Path)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        print(json.dumps(worker(json.loads(sys.stdin.read()))))
        return 0
    if args.summary:
        print(render_summary(read_rows(args.summary)))
        return 0
    if args.prices and any(not math.isfinite(p) or p < 0 for p in args.prices):
        parser.error('prices must be finite and nonnegative')
    output = args.output or ROOT/'evals'/'results'/f'{time.strftime("%Y%m%dT%H%M%S")}-{args.mode}-{uuid.uuid4().hex[:6]}.jsonl'
    names = args.cases or fixture.list_cases()
    try:
        rows = run_suite(names, mode=args.mode, repeats=args.repeats, output=output,
                         timeout=args.timeout, check_timeout=args.check_timeout,
                         max_spend=args.max_spend, prices=Pricing(*args.prices) if args.prices else None)
    except (ValueError, fixture.CaseError, ModelError) as exc:
        parser.error(str(exc))
    # Reopen the durable file: reporting must work without execution state.
    print(f'Results: {output}')
    print(render_summary(read_rows(output)) if output.exists() else 'No runs.')
    return 0 if len(rows) == len(names)*args.repeats and all(r['verdict'] == 'pass' for r in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
