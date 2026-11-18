"""Pre-registered, sequential paired comparison. No tuning or early score display.

python -m evals.compare --protocol evals/results/session-12-protocol.json --mode golden --output /tmp/check.jsonl
python -m evals.compare --protocol evals/results/session-12-protocol.json --mode live --output evals/results/session-12-live.jsonl
python -m evals.compare --report evals/results/session-12-live.jsonl
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
import json
import math
from pathlib import Path
import subprocess
import uuid

from agent.budget import Budget, Pricing
from agent.cli import TOOLS_SYSTEM_PROMPT
from agent.model import ModelConfig
from agent.storage import JOURNALS_DIR
from evals import run
from scripts import fixture

BOUNDARY_SENTENCE = (' Before editing, reason through empty, zero, negative, and boundary inputs '
                     'and preserve behavior outside the requested change.')


def schedule(protocol):
    """Unseal reserved cases only after every development pair has completed."""
    for stage in ('development', 'heldout'):
        for name in protocol[stage]:
            for repeat in range(1, protocol['repeats'] + 1):
                for arm in ('A', 'B'):
                    yield stage, name, repeat, arm


def estimate(protocol):
    pricing = Pricing(**protocol['pricing'])
    usage = protocol['estimate_usage_per_run']
    return len(list(schedule(protocol))) * pricing.cost(
        cache_hit=usage['cache_hit'], cache_miss=usage['cache_miss'], output=usage['output']) * protocol['estimate_multiplier']


def validate(protocol):
    names = protocol['development'] + protocol['heldout']
    if len(names) != 20 or len(set(names)) != 20 or not protocol['heldout']:
        raise ValueError('Protocol requires twenty unique cases and a reserved subset.')
    if type(protocol['repeats']) is not int or protocol['repeats'] < 3:
        raise ValueError('At least three repeats are required.')
    if set(protocol['decisions']) != {f'D{i}' for i in range(112, 123)}:
        raise ValueError('D112–D122 must be locked.')
    positive = [protocol['max_spend'], protocol['timeout'], protocol['check_timeout'],
                protocol['estimate_multiplier'], *protocol['estimate_usage_per_run'].values(),
                *protocol['pricing'].values(), protocol['rule']['min_gain'],
                protocol['rule']['max_p'], protocol['rule']['max_token_ratio'],
                protocol['rule']['max_latency_ratio']]
    if any(not math.isfinite(v) or v < 0 for v in positive) or protocol['max_spend'] <= 0:
        raise ValueError('Invalid protocol limits or prices.')
    if protocol['arms'] != {'A': TOOLS_SYSTEM_PROMPT, 'B': TOOLS_SYSTEM_PROMPT + BOUNDARY_SENTENCE}:
        raise ValueError('Only the registered prompt sentence may vary.')
    for name in names:
        if run.fixture_version(fixture.load_case(name)) != protocol['fixtures'][name]:
            raise ValueError(f'Fixture drift: {name}')
    if protocol['code_digest'] != run.code_version()['digest']:
        raise ValueError('Implementation drift since registration; register a new experiment.')
    if estimate(protocol) > protocol['max_spend']:
        raise ValueError('Estimated suite cost exceeds registered ceiling.')


def registration_commit(path):
    relative = path.resolve().relative_to(run.ROOT).as_posix()
    result = subprocess.run(['git', 'log', '-1', '--format=%H', '--', relative],
                            cwd=run.ROOT, capture_output=True, text=True, check=True)
    sha = result.stdout.strip()
    if not sha:
        raise ValueError('Protocol must be committed before live runs.')
    committed = subprocess.run(['git', 'show', f'{sha}:{relative}'], cwd=run.ROOT,
                               capture_output=True, check=True).stdout
    if committed != path.read_bytes():
        raise ValueError('Protocol differs from committed pre-registration.')
    return sha


def compare(protocol_path, *, mode, output, executor=run.isolated_run):
    protocol = json.loads(protocol_path.read_text())
    validate(protocol)
    if mode not in ('live', 'golden'):
        raise ValueError('Comparisons support live or golden mode only.')
    if output.exists():
        raise ValueError('Result file already exists.')
    registration = registration_commit(protocol_path) if mode == 'live' else None
    config = ModelConfig.from_env() if mode == 'live' else None
    if config and (config.model != protocol['model'] or config.provider.name != protocol['provider']
                   or config.base_url != protocol['endpoint']):
        raise ValueError('Provider configuration differs from registration.')
    pricing = Pricing(**protocol['pricing']) if config else Pricing.free()
    code = run.code_version()
    rows, spent = [], 0.0
    print(f'Estimated live cost: ${estimate(protocol):.6f}; ceiling ${protocol["max_spend"]:.2f}', flush=True)
    for order, (stage, name, repeat, arm) in enumerate(schedule(protocol), 1):
        if spent >= protocol['max_spend']:
            break
        # Verify all inputs again: a mid-suite edit must never contaminate a pair.
        validate(protocol)
        case = fixture.load_case(name)
        run_id = f'{output.stem}-{order:03d}-{arm}-{uuid.uuid4().hex[:8]}'
        workspace = fixture.build(case)
        budget = Budget(**{**protocol['budget'], 'pricing': pricing,
                           'max_steps': case.max_steps or 12,
                           'max_seconds': protocol['timeout'], 'max_spend': protocol['max_spend']-spent})
        request = dict(case=name, mode=mode, run_id=run_id, workspace=str(workspace),
                       journal=str(JOURNALS_DIR/f'{run_id}.jsonl'), budget=asdict(budget),
                       provider=config.provider.name if config else 'scripted',
                       model=config.model if config else 'scripted', endpoint=config.base_url if config else None,
                       timeout=protocol['timeout'], check_timeout=protocol['check_timeout'],
                       system_prompt=protocol['arms'][arm],
                       metadata=dict(suite=output.stem, repeat=repeat, code=code, arm=arm,
                                     stage=stage, order=order, protocol=protocol,
                                     protocol_digest=run.digest(protocol), registration_commit=registration))
        row = executor(request)
        run.append_row(output, row)
        rows.append(row)
        spent += row.get('cost_usd') or 0.0
        print(f'Completed {order}/{len(list(schedule(protocol)))} ({stage}); scores sealed.', flush=True)
        if row.get('fatal') or row.get('cost_uncertain'):
            break
    return rows


def exact_mcnemar(a_only, b_only):
    n = a_only + b_only
    return min(1.0, 2 * sum(math.comb(n, k) for k in range(min(a_only, b_only)+1)) / 2**n) if n else 1.0


def paired_counts(rows):
    pairs = defaultdict(dict)
    for row in rows:
        key = (row['case'], row['repeat'])
        if row['arm'] in pairs[key]:
            raise ValueError('Duplicate arm in paired results.')
        pairs[key][row['arm']] = row['verdict'] == 'pass'
    counts = Counter()
    for pair in pairs.values():
        if len(pair) != 2:
            counts['incomplete'] += 1
        else:
            counts['both_pass' if pair['A'] and pair['B'] else 'A_only' if pair['A']
                   else 'B_only' if pair['B'] else 'both_fail'] += 1
    return {key: counts[key] for key in ('both_pass', 'both_fail', 'A_only', 'B_only', 'incomplete')}


def report(rows):
    if not rows:
        raise ValueError('No comparison rows.')
    protocol = rows[0]['protocol']
    expected = list(schedule(protocol))
    if len(rows) > len(expected):
        raise ValueError('More rows than registered.')
    for row, (stage, name, repeat, arm) in zip(rows, expected):
        if (row['stage'], row['case'], row['repeat'], row['arm']) != (stage, name, repeat, arm):
            raise ValueError('Rows differ from registered interleaved order.')
        if (row['protocol'] != protocol or row['protocol_digest'] != run.digest(protocol)
                or row['fixture'] != protocol['fixtures'][name]
                or row['prompt']['digest'] != run.digest(protocol['arms'][arm])
                or row['code']['digest'] != protocol['code_digest']
                or row['order'] != expected.index((stage, name, repeat, arm))+1
                or row['registration_commit'] != rows[0]['registration_commit']):
            raise ValueError('Comparison input drift or invalid row identity.')
    complete = len(rows) == len(expected)
    arms = {arm: [r for r in rows if r['arm'] == arm] for arm in ('A', 'B')}
    totals = {}
    for arm, group in arms.items():
        metrics = [r.get('metrics', {}) for r in group]
        totals[arm] = dict(runs=len(group), passes=sum(r['verdict']=='pass' for r in group),
                          unexpected=sum(bool(r.get('unexpected')) for r in group),
                          tools=sum(m.get('tool_calls') or 0 for m in metrics),
                          requests=sum((m.get('requests') or 0)+(m.get('requests_unanswered') or 0) for m in metrics),
                          tokens=sum(m.get('total_tokens') or 0 for m in metrics),
                          seconds=sum(r['duration_s'] for r in group),
                          cost=sum(r.get('cost_usd') or 0 for r in group),
                          stops=dict(Counter(m.get('stop_reason') or 'error' for m in metrics)),
                          models=dict(Counter(r.get('model', 'unknown') for r in group)),
                          errors=sum(r['verdict']=='error' for r in group),
                          prompt_tokens=sum(m.get('prompt_tokens') or 0 for m in metrics),
                          completion_tokens=sum(m.get('completion_tokens') or 0 for m in metrics))
    counts = paired_counts(rows)
    paired = sum(counts[k] for k in ('both_pass', 'both_fail', 'A_only', 'B_only'))
    gain = (counts['B_only']-counts['A_only'])/paired if paired else 0
    p = exact_mcnemar(counts['A_only'], counts['B_only'])
    def ratio(key):
        a,b=totals['A'][key],totals['B'][key]
        return b/a if a else 1.0 if not b else math.inf
    heldout = paired_counts([r for r in rows if r['stage']=='heldout'])
    rule=protocol['rule']
    live = all(r['mode']=='live' for r in rows)
    reliable = not any(r.get('cost_uncertain') for r in rows)
    gates = dict(complete=complete, live=live, known_cost=reliable,
                 improvement=gain >= rule['min_gain'], paired_test=p <= rule['max_p'],
                 unexpected=totals['B']['unexpected'] <= totals['A']['unexpected'],
                 tokens=ratio('tokens') <= rule['max_token_ratio'],
                 latency=ratio('seconds') <= rule['max_latency_ratio'],
                 heldout=heldout['B_only'] >= heldout['A_only'])
    ship=all(gates.values())
    # Case-level means avoid treating repeats as independent interval samples.
    by_case = defaultdict(list)
    for name in sorted({r['case'] for r in rows}):
        group=[r for r in rows if r['case']==name]
        c=paired_counts(group)
        n=sum(c[k] for k in ('both_pass','both_fail','A_only','B_only'))
        if n: by_case[name]=(c['B_only']-c['A_only'])/n
    radius=math.sqrt(2*math.log(40)/len(by_case)) if by_case else 1
    center=sum(by_case.values())/len(by_case) if by_case else 0
    lines=[f'# Session 12 comparison — {"ship" if ship else "do not ship"}', '',
           f'The change flipped {counts["B_only"]} observations to passing and {counts["A_only"]} to failing; '
           f'{"enough" if ship else "not enough"} to ship under the registered rule.', '',
           f'Rows: {len(rows)}/{len(expected)}. Complete: {complete}. Mode: {rows[0]["mode"]}.',
           f'Pre-registration commit: `{rows[0]["registration_commit"]}`; protocol digest: `{run.digest(protocol)}`.',
           f'Primary gain: {gain:.1%}; exact two-sided McNemar p={p:.6f}.',
           f'95% case-cluster Hoeffding interval: [{max(-1,center-radius):.1%}, {min(1,center+radius):.1%}].',
           'McNemar treats repeated outcomes as independent; correlated repeats and authored cases limit inference. '
           'The conservative interval uses case means. Neither supports generalization beyond this suite.', '',
           'arm | success | unexpected | tools | requests | tokens | seconds | estimated USD',
           '--- | --- | --- | --- | --- | --- | --- | ---']
    for arm,t in totals.items():
        lines.append(f'{arm} | {t["passes"]}/{t["runs"]} | {t["unexpected"]} | {t["tools"]} | {t["requests"]} | {t["tokens"]} | {t["seconds"]:.2f} | {t["cost"]:.6f}')
    lines += ['', f'Token ratio B/A: {ratio("tokens"):.3f}; latency ratio B/A: {ratio("seconds"):.3f}.',
              'Decision gates: '+json.dumps(gates, sort_keys=True),
              'Stop reasons: '+json.dumps({a:t['stops'] for a,t in totals.items()},sort_keys=True),
              'Returned models: '+json.dumps({a:t['models'] for a,t in totals.items()},sort_keys=True),
              'Usage detail: '+json.dumps({a:{k:t[k] for k in ('prompt_tokens','completion_tokens','errors')} for a,t in totals.items()},sort_keys=True),
              f'Cost-uncertain rows: {sum(bool(r.get("cost_uncertain")) for r in rows)}. '
              f'Estimated preflight cost: ${estimate(protocol):.6f}; ceiling: ${protocol["max_spend"]:.2f}.', '',
              'case | stage | both pass | both fail | A only | B only | incomplete | flaky arms',
              '--- | --- | --- | --- | --- | --- | --- | ---']
    for name in protocol['development']+protocol['heldout']:
        group=[r for r in rows if r['case']==name]
        c=paired_counts(group)
        flaky=[a for a in ('A','B') if len({r['verdict']=='pass' for r in group if r['arm']==a})>1]
        stage='heldout' if name in protocol['heldout'] else 'development'
        lines.append(f'{name} | {stage} | '+ ' | '.join(str(c[k]) for k in c)+f' | {",".join(flaky) or "none"}')
    for stage in ('development','heldout'):
        lines += ['', stage+': '+json.dumps(paired_counts([r for r in rows if r['stage']==stage]),sort_keys=True)]
    lines += ['', 'Errors and flaky outcomes remain in the denominator; no selective reruns. Partial or scripted suites cannot ship. '
              'No negative control was run. A null or saturated suite does not establish equivalence. '
              'Latency includes checks and local worker time; concurrent service latency must be measured separately.']
    return '\n'.join(lines)+'\n'


def main(argv=None):
    from dotenv import load_dotenv
    load_dotenv(run.ROOT/'.env')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', type=Path)
    parser.add_argument('--mode', choices=('golden','live'), default='golden')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--report', type=Path)
    args=parser.parse_args(argv)
    try:
        if args.report:
            print(report(run.read_rows(args.report)),end='')
            return 0
        if not args.protocol or not args.output:
            parser.error('--protocol and --output are required to run')
        rows=compare(args.protocol, mode=args.mode, output=args.output)
        print(report(run.read_rows(args.output)) if rows else 'No completed rows.')
        return 0 if len(rows)==len(list(schedule(json.loads(args.protocol.read_text())))) else 1
    except (ValueError, KeyError, fixture.CaseError) as exc:
        parser.error(str(exc))


if __name__=='__main__':
    raise SystemExit(main())
