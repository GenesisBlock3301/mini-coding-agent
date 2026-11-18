"""Session 11: deliberately fail the grader before trusting model scores."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from agent.budget import Budget, Pricing
from agent.journal import Journal
from agent.model import ModelError
from agent import tracing
from evals import run
from scripts import fixture

CASES = fixture.list_cases()


def execute(tmp_path, name='tail_window', mode='golden', **kwargs):
    case = fixture.load_case(name)
    workspace = fixture.build(case, tmp_path / 'ws')
    return run.run_case(case, mode=mode, run_id='test', workspace=workspace,
                        journal_path=tmp_path/'run.jsonl',
                        budget=Budget(max_steps=12, pricing=Pricing.free()), **kwargs)


def test_cases_with_deliberately_documented_category_distribution():
    assert len(CASES) == 20
    assert {category: sum(fixture.load_case(n).category == category for n in CASES)
            for category in ('read_search', 'simple_edit', 'multi_file', 'boundary', 'already_correct', 'behavioral')} == {
                'read_search': 2, 'simple_edit': 3, 'multi_file': 2, 'boundary': 3, 'already_correct': 1, 'behavioral': 9}


@pytest.mark.parametrize('name', CASES)
def test_all_golden_transcripts_pass_real_tools_and_independent_checkers(name, tmp_path):
    row = execute(tmp_path, name)
    assert row['verdict'] == 'pass', row
    assert row['metrics']['finished']
    assert row['fixture']['template']
    assert row['prompt']['digest']
    assert row['budget']['max_steps'] == 12
    assert row['provider'] == 'scripted'


@pytest.mark.parametrize(('mode', 'reason'), [('wrong_file', 'unexpected_change'),
                                             ('noop', 'check_failed'),
                                             ('double_edit', 'double_edit')])
def test_three_distinct_bad_transcripts(mode, reason, tmp_path):
    row = execute(tmp_path, mode=mode)
    assert row['verdict'] == 'fail'
    assert reason in row['failures']


@pytest.mark.parametrize('name', ['rename_import', 'limit_and_test'])
def test_half_a_pair_fails(name, tmp_path):
    row = execute(tmp_path, name, 'half_edit')
    assert row['verdict'] == 'fail'
    assert row['failures'] == ['check_failed']
    assert len(row['changed']) == 1


@pytest.mark.parametrize('name', ['rename_import', 'limit_and_test'])
def test_real_process_death_after_first_write_fails_the_checker(name, tmp_path):
    case = fixture.load_case(name)
    workspace = fixture.build(case, tmp_path/'ws')
    journal = tmp_path/'crashed.jsonl'
    script = '''
import os, sys
from pathlib import Path
from agent.loop import run_loop
from agent.journal import Journal
from evals.run import scripted_model
from scripts.fixture import load_case
case = load_case(sys.argv[1])
def die(step, call, result):
    if result.changed_path:
        os._exit(73)
run_loop([dict(role='user', content=case.task)], root=Path(sys.argv[2]),
         journal=Journal(Path(sys.argv[3])), model_fn=scripted_model(case), on_tool=die)
'''
    child = subprocess.run([sys.executable, '-c', script, name, str(workspace), str(journal)], cwd=run.ROOT)
    assert child.returncode == 73
    assert len(fixture.changes(case, workspace).all) == 1
    assert not fixture.verify(case, workspace).ok
    assert not tracing.summary(tracing.load(journal))['finished']


@pytest.mark.parametrize(('name', 'answers'), [
    ('find_discount', ['pricing.py: discounted_total', '`discounted_total()` in **pricing.py**.',
                       '/workspace/pricing.py: discounted_total',
                       'The function is DISCOUNTED_TOTAL; the file is PRICING.PY.']),
    ('find_retry', ['net/backoff.py: retry_delay', '`retry_delay()` lives in `net/backoff.py`.',
                    '/workspace/net/backoff.py: retry_delay'])])
def test_read_answer_checker_accepts_format_variations_and_rejects_wrong_pair(name, answers, tmp_path):
    case = fixture.load_case(name)
    workspace = fixture.build(case, tmp_path/'ws')
    for answer in answers:
        assert fixture.verify(case, workspace, answer=answer).ok
    for answer in ('Done.', 'pricing.py: discount_label', 'client.py: retry_request'):
        assert not fixture.verify(case, workspace, answer=answer).ok
    assert not fixture.changes(case, workspace).all


def test_already_correct_must_explain_and_must_not_edit(tmp_path):
    case = fixture.load_case('already_correct')
    workspace = fixture.build(case, tmp_path/'ws')
    assert not fixture.verify(case, workspace, answer='Done.').ok
    assert fixture.verify(case, workspace, answer=case.answer).ok
    assert fixture.verify(case, workspace, answer='The report is false; the function is correct. No files changed.').ok
    with (workspace/'clamp.py').open('a') as handle:
        handle.write('\n# still correct but unrequested\n')
    report = fixture.verify(case, workspace, answer=case.answer)
    assert report.check_ok
    assert report.unexpected == ('clamp.py',)
    assert not report.ok


def test_correct_boundary_prose_without_actual_refusal_fails(tmp_path):
    row = execute(tmp_path, 'outside_refusal', model_fn=lambda messages, tools=None:
                  run.response('Access refused because it is outside the workspace.'))
    assert row['verdict'] == 'fail'
    assert row['missing_errors'] == ['outside_workspace']


def test_ambiguous_anchor_refusal_changes_nothing_before_recovery(tmp_path):
    from agent.tools import dispatch
    case = fixture.load_case('ambiguous_recovery')
    workspace = fixture.build(case, tmp_path/'ws')
    probe = case.probes[0]
    result = dispatch(probe['name'], json.dumps(probe['arguments']), root=workspace)
    assert result.error_code == 'multiple_matches'
    assert not fixture.changes(case, workspace).all
    fixture.apply_golden(case, workspace)
    assert fixture.verify(case, workspace).ok


def test_semantically_correct_non_golden_fix_is_accepted(tmp_path):
    case = fixture.load_case('tail_window')
    workspace = fixture.build(case, tmp_path/'ws')
    path = workspace/'stats.py'
    path.write_text(path.read_text().replace(case.golden['old'], '    return values[len(values) - n:]'))
    assert fixture.verify(case, workspace).ok


def test_hanging_checker_becomes_error_row(tmp_path, monkeypatch):
    case = fixture.load_case('tail_window')
    checker = tmp_path/'hang.py'
    checker.write_text('while True: pass\n')
    monkeypatch.setattr(fixture.Case, 'check_script', property(lambda self: checker))
    row = execute(tmp_path, check_timeout=0.05)
    assert row['verdict'] == 'error'
    assert row['failures'] == ['checker_timeout']


def test_case_deadline_kills_worker_and_preserves_journal(tmp_path, monkeypatch):
    # Real child blocks in a checker. The outer deadline must kill the worker.
    request = make_request(tmp_path)
    request['timeout'] = 0.01
    row = run.isolated_run(request)
    assert row['verdict'] == 'error'
    assert row['failures'] == ['case_timeout']
    assert row['fixture']['template']
    assert row['code']


def make_request(tmp_path):
    case = fixture.load_case('tail_window')
    workspace = fixture.build(case, tmp_path/'ws')
    return dict(case=case.name, mode='golden', run_id='isolated', workspace=str(workspace),
                journal=str(tmp_path/'run.jsonl'), budget=run.asdict(Budget(pricing=Pricing.free())),
                provider='scripted', model='scripted', timeout=5, check_timeout=1,
                metadata=dict(suite='test', repeat=1, code=run.code_version()))


def test_isolated_worker_and_in_process_results_agree(tmp_path):
    row = run.isolated_run(make_request(tmp_path))
    assert row['verdict'] == 'pass', row
    assert row['metrics']['tool_calls'] == 4


def test_auth_error_on_fourth_run_keeps_first_three_and_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(fixture, 'BUILD_DIR', tmp_path/'workspaces')
    calls = []
    def executor(request):
        calls.append(request)
        def auth(messages, tools=None):
            raise ModelError('auth', 'Key refused.', retryable=False, safe_to_retry=True, status_code=401)
        if len(calls) == 4:
            case = fixture.load_case(request['case'])
            return run.run_case(case, mode='golden', run_id=request['run_id'],
                                workspace=Path(request['workspace']), journal_path=Path(request['journal']),
                                budget=Budget(pricing=Pricing.free()), model_fn=auth,
                                metadata=request['metadata'])
        return run.worker(request)
    output = tmp_path/'rows.jsonl'
    rows = run.run_suite(CASES, mode='golden', repeats=3, output=output,
                         timeout=5, check_timeout=1, max_spend=1, executor=executor)
    assert len(calls) == len(rows) == 4
    assert [r['verdict'] for r in rows] == ['pass', 'pass', 'pass', 'error']
    assert rows[-1]['failures'] == ['auth']
    assert run.read_rows(output) == rows
    assert len({r['workspace'] for r in rows}) == 4


def test_checker_exception_records_error_and_suite_continues(tmp_path, monkeypatch):
    monkeypatch.setattr(fixture, 'BUILD_DIR', tmp_path/'workspaces')
    original = fixture.verify
    n = 0
    def broken_once(*args, **kwargs):
        nonlocal n
        n += 1
        if n == 1:
            raise RuntimeError('broken checker')
        return original(*args, **kwargs)
    monkeypatch.setattr(fixture, 'verify', broken_once)
    rows = run.run_suite(['tail_window'], mode='golden', repeats=2, output=tmp_path/'rows.jsonl',
                         timeout=5, check_timeout=1, max_spend=1, executor=run.worker)
    assert [r['verdict'] for r in rows] == ['error', 'pass']


def test_spend_ceiling_stops_before_next_case(tmp_path, monkeypatch):
    monkeypatch.setattr(fixture, 'BUILD_DIR', tmp_path/'workspaces')
    def executor(request):
        row = run.worker(request)
        row['cost_usd'] = 0.10
        return row
    rows = run.run_suite(['tail_window'], mode='golden', repeats=3, output=tmp_path/'rows.jsonl',
                         timeout=5, check_timeout=1, max_spend=0.1, executor=executor)
    assert len(rows) == 1


def test_summary_uses_rows_only_and_reports_flakiness(tmp_path):
    row = execute(tmp_path)
    rows = [row, row, dict(row, verdict='fail')]
    path = tmp_path/'results.jsonl'
    for result in rows:
        run.append_row(path, result)
    report = run.render_summary(run.read_rows(path))
    assert '2/3 | flaky' in report
    assert 'Success: 2/3 (67%)' in report
    assert 'model requests: 6' in report


def test_code_fingerprint_changes_with_local_code_even_at_same_sha(monkeypatch, tmp_path):
    first = run.code_version()
    (tmp_path/'agent').mkdir()
    source = tmp_path/'agent'/'sample.py'
    source.write_text('x=1\n')
    monkeypatch.setattr(run, 'ROOT', tmp_path)
    old = run.code_version()
    source.write_text('x=2\n')
    new = run.code_version()
    assert new['digest'] != old['digest']
    assert first['sha']


def test_result_file_is_not_overwritten(tmp_path):
    path = tmp_path/'rows.jsonl'
    path.write_text('precious\n')
    with pytest.raises(ValueError, match='already exists'):
        run.run_suite(['tail_window'], mode='golden', repeats=1, output=path,
                      timeout=5, check_timeout=1, max_spend=1)
    assert path.read_text() == 'precious\n'


def test_live_spend_admission_stops_before_sending_a_request(tmp_path, monkeypatch):
    from agent.model import ModelConfig
    case = fixture.load_case('tail_window')
    workspace = fixture.build(case, tmp_path/'ws')
    def never_send(*args, **kwargs):
        pytest.fail('a request with no spend reserve must never reach the provider')
    monkeypatch.setattr(run, 'call_model', never_send)
    row = run.run_case(case, mode='live', run_id='limited', workspace=workspace,
                       journal_path=tmp_path/'run.jsonl', config=ModelConfig(api_key='unused'),
                       budget=Budget(pricing=Pricing(1, 1, 1), max_spend=0.0025, token_reserve=1))
    assert row['verdict'] == 'error'
    assert row['failures'] == ['suite_spend']
    assert row['cost_usd'] == 0
    assert not row['cost_uncertain']


def test_partial_usage_and_known_cost_survive_a_later_provider_timeout(tmp_path, monkeypatch):
    from agent.model import ModelConfig
    case = fixture.load_case('tail_window')
    workspace = fixture.build(case, tmp_path/'ws')
    count = 0
    def provider(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise ModelError('timeout', 'Provider timed out.', retryable=True, safe_to_retry=False)
        result = run.response(calls=[dict(id='read1', type='function', function=dict(
            name='read_file', arguments=json.dumps(dict(path='stats.py'))))])
        result.prompt_tokens = 100
        result.completion_tokens = 10
        result.total_tokens = 110
        result.prompt_cache_hit_tokens = 60
        result.prompt_cache_miss_tokens = 40
        return result
    monkeypatch.setattr(run, 'call_model', provider)
    row = run.run_case(case, mode='live', run_id='partial', workspace=workspace,
                       journal_path=tmp_path/'run.jsonl', config=ModelConfig(api_key='unused'),
                       budget=Budget(pricing=Pricing(1, 2, 3), max_spend=1))
    assert row['verdict'] == 'error'
    assert row['cost_uncertain']
    assert row['metrics']['total_tokens'] == 110
    assert row['metrics']['prompt_cache_hit_tokens'] == 60
    assert row['metrics']['requests_unanswered'] == 1
    assert row['cost_usd'] == pytest.approx(0.00017)


def test_a_successful_outside_read_fails_even_if_another_call_was_refused(tmp_path):
    case = fixture.load_case('outside_refusal')
    workspace = fixture.build(case, tmp_path/'ws')
    trace = tracing.Trace(run_id='bad-boundary', path=tmp_path/'run.jsonl', events=[
        dict(t='tool_intent', call_id='safe', name='read_file',
             arguments=json.dumps(dict(path='../outside-secret.txt'))),
        dict(t='tool_outcome', call_id='safe', ok=False, error_code='outside_workspace'),
        dict(t='tool_intent', call_id='escape', name='read_file',
             arguments=json.dumps(dict(path='../another-secret.txt'))),
        dict(t='tool_outcome', call_id='escape', ok=True),
    ])
    grade = run.grade(case, workspace, trace, case.answer, 'answer')
    assert grade['verdict'] == 'fail'
    assert grade['failures'] == ['boundary_breach']


def test_generated_artifacts_are_not_copied_or_scripted_as_source(tmp_path, monkeypatch):
    case = fixture.load_case('tail_window')
    template = tmp_path/'template'
    template.mkdir()
    (template/'source.py').write_text('x = 1\n')
    (template/'__pycache__').mkdir()
    (template/'__pycache__'/'source.pyc').write_bytes(b'\0generated bytecode')
    (template/'.DS_Store').write_bytes(b'generated metadata')
    monkeypatch.setattr(fixture.Case, 'template', property(lambda self: template))
    workspace = fixture.build(case, tmp_path/'ws')
    assert fixture.hash_tree(workspace) == fixture.hash_tree(template)
    assert not (workspace/'__pycache__').exists()
    assert not (workspace/'.DS_Store').exists()
    first = run.scripted_model(replace(case, edits=()))([], tools=None)
    assert len(first.tool_calls) == 1
    assert json.loads(first.tool_calls[0]['function']['arguments'])['path'] == 'source.py'


def test_eval_identity_is_inside_run_started_without_a_phantom_trace_segment(tmp_path):
    row = execute(tmp_path)
    trace = tracing.load(Path(row['journal']))
    assert trace.events[0]['t'] == 'run_started'
    assert trace.events[0]['evaluation']['case'] == 'tail_window'
    assert trace.events[0]['evaluation']['run_id'] == row['run_id']
    assert len(tracing.segments(trace)) == 1


def test_deadline_kills_the_worker_group_including_its_checker(tmp_path, monkeypatch):
    import os
    import signal
    import time
    request = make_request(tmp_path)
    request['timeout'] = 0.3
    marker = tmp_path/'group.json'
    def stuck_worker(request):
        checker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        marker.write_text(json.dumps(dict(worker=os.getpid(), checker=checker.pid,
                                         checker_group=os.getpgid(checker.pid))))
        time.sleep(60)
    monkeypatch.setattr(run, 'worker', stuck_worker)
    killed = []
    real_killpg = os.killpg
    def kill_group(pid, sig):
        killed.append((pid, sig))
        return real_killpg(pid, sig)
    monkeypatch.setattr(run.os, 'killpg', kill_group)
    row = run.isolated_run(request)
    identity = json.loads(marker.read_text())
    assert row['failures'] == ['case_timeout']
    assert identity['checker_group'] == identity['worker']
    assert killed == [(identity['worker'], signal.SIGKILL)]
