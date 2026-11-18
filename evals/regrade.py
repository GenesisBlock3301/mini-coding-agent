"""Audit saved rows after a checker repair, without model calls or overwrites."""
import argparse
from pathlib import Path

from agent import tracing
from evals import run
from scripts import fixture


def regrade(source, output):
    if output.exists():
        raise ValueError('Audit file already exists.')
    rows = run.read_rows(source)
    for row in rows:
        case = fixture.load_case(row['case'])
        version = run.fixture_version(case)
        if any(version[k] != row['fixture'][k] for k in ('template', 'spec')):
            raise ValueError('Only checker changes may be regraded.')
        trace = tracing.load(Path(row['journal']))
        if not trace.events or not Path(row['workspace']).is_dir():
            raise ValueError('Saved journal and workspace are required.')
        grade = run.grade(case, Path(row['workspace']), trace, row.get('answer') or '',
                          row.get('metrics', {}).get('stop_reason'))
        run.append_row(output, dict(run_id=row['run_id'],case=case.name,
                                   source=str(source),source_sha256=run.hashlib.sha256(source.read_bytes()).hexdigest(),
                                   original_verdict=row['verdict'],original_fixture=row['fixture'],
                                   fixture=version,**grade))
    return run.read_rows(output)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    rows=regrade(args.source,args.output)
    print(f'Regraded {len(rows)} rows: {sum(r["verdict"]=="pass" for r in rows)} pass; '
          f'{sum(r["verdict"]!=r["original_verdict"] for r in rows)} verdicts changed.')


if __name__=='__main__': main()
