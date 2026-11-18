"""Freeze Session 12 inputs before its live comparison; never overwrite a protocol."""
from dataclasses import asdict
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from agent.budget import Budget, Pricing
from agent.model import ModelConfig
from agent.cli import TOOLS_SYSTEM_PROMPT
from evals import compare, run
from scripts import fixture

HELDOUT = ['atomic_transfer', 'copy_merge', 'literal_suffix', 'pagination_total']


def register(baseline, output):
    if output.exists():
        raise ValueError('Registration already exists; never revise a live experiment.')
    rows=run.read_rows(baseline)
    if not rows or any(r['mode']!='live' for r in rows):
        raise ValueError('Estimate requires historical live rows.')
    config=ModelConfig.from_env()
    prices=Pricing(.006, .30, 1.20)  # Conservative configured experiment rates, not a current price claim.
    usage=dict(cache_hit=0.,cache_miss=0.,output=0.)
    for row in rows:
        m=row['metrics']
        hit=m.get('cache_hit_tokens') or m.get('prompt_cache_hit_tokens') or 0
        usage['cache_hit']+=hit
        usage['cache_miss']+=m.get('prompt_cache_miss_tokens') or (m['prompt_tokens']-hit)
        usage['output']+=m['completion_tokens']
    usage={k:v/len(rows) for k,v in usage.items()}
    budget=asdict(Budget.from_env(pricing=prices))
    for key in ('pricing','max_steps','max_seconds','max_spend'): budget.pop(key)
    budget['context_window']=config.provider.context_window
    names=fixture.list_cases()
    protocol=dict(schema=1, date=datetime.now(timezone.utc).isoformat(),
                  hypothesis='Explicit boundary reasoning will improve safe task success on behavioral edge cases without more than 25% token or latency growth.',
                  source_git=run.code_version(), code_digest=run.code_version()['digest'],
                  provider=config.provider.name,model=config.model,endpoint=config.base_url,
                  development=[n for n in names if n not in HELDOUT],heldout=HELDOUT,
                  repeats=3, arms={'A':TOOLS_SYSTEM_PROMPT,'B':TOOLS_SYSTEM_PROMPT+compare.BOUNDARY_SENTENCE},
                  pricing=asdict(prices),max_spend=.50,timeout=120,check_timeout=5,budget=budget,
                  estimate_usage_per_run=usage, estimate_multiplier=3,
                  estimate_source=dict(path=str(baseline.relative_to(run.ROOT)),sha256=run.hashlib.sha256(baseline.read_bytes()).hexdigest(),runs=len(rows)),
                  fixtures={n:run.fixture_version(fixture.load_case(n)) for n in names},
                  rule=dict(min_gain=.10,max_p=.05,max_token_ratio=1.25,max_latency_ratio=1.25),
                  decisions={
                      'D112':'Only one appended boundary-reasoning prompt sentence; no production default changes.',
                      'D113':'Paired A then B per case/repeat, sequential; 96 development observations then 24 reserved observations.',
                      'D114':'Task success including unexpected-change gate; errors count as nonpasses.',
                      'D115':'Ship only complete live data: >=10 percentage-point gain, exact two-sided McNemar p<=.05, and every trade-off gate.',
                      'D116':'Exactly three runs per case per arm; no optional stopping or selective reruns.',
                      'D117':'Four predetermined new cases; fixture author may read specs for checker validation, but no live held-out rows or transcripts before development finishes; no tuning from them.',
                      'D118':'Mixed pass/nonpass arms reported as flaky, every observation retained in denominator; no exclusions.',
                      'D119':'Historical mean usage times 120 times 3 safety margin; refuse if estimate exceeds $0.50; cumulative request admission and actual-spend stop; uncertain billing stops.',
                      'D120':'Committed protocol SHA; source/fixture/prompt/tool hashes; requested and returned model, provider, endpoint, budgets, usage, prices, order and journals; provider-default temperature, no seed supported.',
                      'D121':'Always publish null and raw result rows; report regenerates from rows alone.',
                      'D122':'No increase in unexpected-change runs; B tokens and summed latency each <=1.25 A; held-out net flips nonnegative; no negative-control suite in this experiment.'})
    compare.validate(protocol)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(protocol,indent=2,sort_keys=True)+'\n')
    print(f'Registration: {output}; estimate ${compare.estimate(protocol):.6f}; ceiling $0.50')


def main():
    from dotenv import load_dotenv
    load_dotenv(run.ROOT/'.env')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    register(args.baseline.resolve(),args.output)


if __name__=='__main__': main()
