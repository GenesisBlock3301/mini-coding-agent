"""Session 12: protect protocol, pairing, spend admission and mechanical decision."""
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from agent.budget import Budget, Pricing
from agent.cli import TOOLS_SYSTEM_PROMPT
from evals import compare, run
from scripts import fixture


def protocol():
    names=fixture.list_cases()
    reserved=['atomic_transfer','copy_merge','literal_suffix','pagination_total']
    budget=asdict(Budget(pricing=Pricing.free()))
    for key in ('pricing','max_steps','max_seconds','max_spend'):
        budget.pop(key)
    return dict(development=[n for n in names if n not in reserved], heldout=reserved,
                repeats=3, arms={'A':TOOLS_SYSTEM_PROMPT,'B':TOOLS_SYSTEM_PROMPT+compare.BOUNDARY_SENTENCE},
                decisions={f'D{i}':'locked' for i in range(112,123)}, max_spend=.5,
                timeout=120, check_timeout=5, estimate_multiplier=3,
                pricing=asdict(Pricing(.006,.3,1.2)),
                estimate_usage_per_run={'cache_hit':2000,'cache_miss':2500,'output':400},
                fixtures={n:run.fixture_version(fixture.load_case(n)) for n in names},
                code_digest=run.code_version()['digest'], budget=budget,
                rule=dict(min_gain=.1,max_p=.05,max_token_ratio=1.25,max_latency_ratio=1.25))


def rows_for(p, verdicts=None):
    rows=[]
    for order,(stage,name,repeat,arm) in enumerate(compare.schedule(p),1):
        rows.append(dict(case=name,stage=stage,repeat=repeat,arm=arm,order=order,
                         mode='live', protocol=p,protocol_digest=run.digest(p),
                         registration_commit='registered',code={'digest':p['code_digest']},
                         fixture=p['fixtures'][name],prompt={'digest':run.digest(p['arms'][arm])},
                         verdict=(verdicts or {}).get(arm,'pass'),metrics={'total_tokens':100,'stop_reason':'answer'},
                         duration_s=1,cost_usd=.001))
    return rows


def test_schedule_interleaves_and_unseals_last():
    p=protocol()
    schedule=list(compare.schedule(p))
    assert len(schedule)==120
    assert [s[3] for s in schedule]==['A','B']*60
    assert all(s[0]=='development' for s in schedule[:96])
    assert all(s[0]=='heldout' for s in schedule[96:])


def test_admission_rejects_estimate_and_fixture_drift():
    p=protocol()
    compare.validate(p)
    p['max_spend']=.0001
    with pytest.raises(ValueError,match='Estimated'):
        compare.validate(p)
    p=protocol()
    p['fixtures'][p['development'][0]]['template']='drift'
    with pytest.raises(ValueError,match='Fixture drift'):
        compare.validate(p)


def test_uncommitted_or_modified_registration_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        compare.registration_commit(tmp_path/'not-in-repository.json')


def test_paired_statistics_null_and_direction():
    assert compare.exact_mcnemar(3,3)==1
    assert compare.exact_mcnemar(0,0)==1
    assert compare.exact_mcnemar(0,6)==pytest.approx(.03125)
    rows=rows_for(protocol(),{'A':'fail','B':'pass'})
    counts=compare.paired_counts(rows)
    assert counts['B_only']==60 and counts['A_only']==0
    assert 'comparison — ship' in compare.report(rows)
    assert 'comparison — do not ship' in compare.report(rows_for(protocol()))


def test_partial_scripted_cost_and_latency_cannot_ship():
    p=protocol()
    rows=rows_for(p,{'A':'fail','B':'pass'})
    assert 'comparison — do not ship' in compare.report(rows[:-1])
    for row in rows: row['mode']='golden'
    assert 'comparison — do not ship' in compare.report(rows)
    for row in rows:
        row['mode']='live'
        if row['arm']=='B': row['duration_s']=2
    assert '"latency": false' in compare.report(rows)
    for row in rows: row['duration_s']=1
    rows[0]['cost_uncertain']=True
    assert '"known_cost": false' in compare.report(rows)


def test_report_rejects_duplicate_missing_reordered_and_drifted_rows():
    rows=rows_for(protocol())
    for invalid in ([rows[0],rows[0]],rows[1:],list(reversed(rows))):
        with pytest.raises(ValueError): compare.report(invalid)
    rows[0]['fixture']={}
    with pytest.raises(ValueError): compare.report(rows)


def test_unexpected_changes_and_heldout_reversal_block_shipping():
    rows=rows_for(protocol(),{'A':'fail','B':'pass'})
    rows[1]['unexpected']=['unrelated.py']
    assert '"unexpected": false' in compare.report(rows)
    rows[1].pop('unexpected')
    for row in rows:
        if row['stage']=='heldout': row['verdict']='pass' if row['arm']=='A' else 'fail'
    assert '"heldout": false' in compare.report(rows)


def test_suite_stops_on_uncertain_cost_and_keeps_interleaved_rows(tmp_path,monkeypatch):
    p=protocol()
    path=tmp_path/'protocol.json'
    path.write_text(json.dumps(p))
    seen=[]
    def executor(request):
        seen.append(request)
        return dict(request['metadata'],case=request['case'],verdict='error',cost_usd=.01,cost_uncertain=True)
    output=tmp_path/'rows.jsonl'
    rows=compare.compare(path,mode='golden',output=output,executor=executor)
    assert len(rows)==1
    assert rows[0]['arm']=='A'
    assert run.read_rows(output)==rows


def test_new_cases_reject_noop_and_accept_golden(tmp_path):
    for name in ['zero_window','falsy_setting','stable_dedupe','adjacent_ranges','nonadjacent_anchor',
                 'copy_merge','pagination_total','literal_suffix','atomic_transfer']:
        case=fixture.load_case(name)
        workspace=fixture.build(case,tmp_path/name)
        assert not fixture.verify(case,workspace).ok, name
        fixture.apply_golden(case,workspace)
        assert fixture.verify(case,workspace).ok, name


def test_prompt_variant_is_used_and_hashed(tmp_path):
    case=fixture.load_case('tail_window')
    workspace=fixture.build(case,tmp_path/'ws')
    prompt=TOOLS_SYSTEM_PROMPT+compare.BOUNDARY_SENTENCE
    scripted=run.scripted_model(case)
    def model(messages,tools=None):
        assert messages[0]['content']==prompt
        return scripted(messages,tools=tools)
    row=run.run_case(case,mode='golden',run_id='prompt',workspace=workspace,
                     journal_path=tmp_path/'j.jsonl',budget=Budget(max_steps=12,pricing=Pricing.free()),
                     model_fn=model,system_prompt=prompt)
    assert row['verdict']=='pass'
    assert row['prompt']['digest']==run.digest(prompt)


def test_cumulative_ceiling_stops_and_keeps_rows(tmp_path):
    p=protocol()
    path=tmp_path/'protocol.json'
    path.write_text(json.dumps(p))
    def executor(request):
        return dict(request['metadata'],case=request['case'],verdict='pass',cost_usd=.25)
    rows=compare.compare(path,mode='golden',output=tmp_path/'rows.jsonl',executor=executor)
    assert len(rows)==2
    assert [r['arm'] for r in rows]==['A','B']


def test_estimate_reconstructs_historical_mean_cost():
    p=protocol()
    prices=Pricing(**p['pricing'])
    u=p['estimate_usage_per_run']
    assert compare.estimate(p)==pytest.approx(360*prices.cost(
        cache_hit=u['cache_hit'],cache_miss=u['cache_miss'],output=u['output']))
