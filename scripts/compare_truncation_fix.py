"""Compare verified before/after truncation-rule runs, allowing only code changes."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from compare_policies import load_run, read_json, digest
from prepare_review import rows


MARKER_RULES={'RECORDED_TRUNCATION','TRUNCATION_LITERAL'}


def code_digest(folder):
    return hashlib.sha256(''.join(p.name+':'+digest(p) for p in sorted(folder.glob('*.py'))).encode()).hexdigest()


def source_key(row):
    return json.dumps([row['trajectory_id'],row['rule_id'],row['source']],sort_keys=True)


def compare(before,after,baseline_code):
    for key in ('data_sha256','dependencies','config'):
        if before['manifest'][key]!=after['manifest'][key]:
            raise ValueError('Confounded before/after comparison: '+key)
    if code_digest(baseline_code)!=before['manifest']['code_sha256']:
        raise ValueError('Saved baseline code does not match old run')
    if code_digest(Path('agent_qc'))!=after['manifest']['code_sha256']:
        raise ValueError('Current code does not match new run')
    a,b=before['decisions'],after['decisions']
    if set(a)!=set(b):raise ValueError('Trajectory sets differ')
    common=lambda run:[r for r in run['issues'] if r['rule_id'] not in MARKER_RULES]
    if common(before)!=common(after):
        raise ValueError('A non-truncation finding changed')
    old={source_key(r):r for r in before['issues'] if r['rule_id'] in MARKER_RULES}
    new={source_key(r):r for r in after['issues'] if r['rule_id'] in MARKER_RULES}
    differences=[]
    for key in sorted(set(old)|set(new)):
        left,right=old.get(key),new.get(key)
        if left==right:continue
        differences.append({'change':'removed' if right is None else ('added' if left is None else 'evidence_updated'),
                            'before':left,'after':right})
    changed=[{'trajectory_id':tid,'before':a[tid]['decision'],'after':b[tid]['decision']}
             for tid in sorted(a) if a[tid]['decision']!=b[tid]['decision']]
    reasons=[{'trajectory_id':tid,'decision_before':a[tid]['decision'],'decision_after':b[tid]['decision'],
        'reason_codes_before':a[tid]['reason_codes'],'reason_codes_after':b[tid]['reason_codes'],
        'risk_codes_before':a[tid]['risk_codes'],'risk_codes_after':b[tid]['risk_codes']}
        for tid in sorted(a) if any(a[tid][k]!=b[tid][k] for k in ('reason_codes','risk_codes'))]
    qold={r['trajectory_id']:r for r in before['queue']};qnew={r['trajectory_id']:r for r in after['queue']}
    structural=lambda q:[{k:v for k,v in r.items() if k!='issue_ids'} for r in q]
    return {'raw_and_config_and_dependencies_identical':True,
        'non_truncation_findings_identical':True,
        'before_code_sha256':before['manifest']['code_sha256'],'after_code_sha256':after['manifest']['code_sha256'],
        'trajectory_count':len(a),'decision_counts_before':before['summary']['decisions'],
        'decision_counts_after':after['summary']['decisions'],'decision_changes':changed,
        'reason_or_risk_changes':reasons,
        'marker_issues_before':dict(Counter(r['rule_id'] for r in old.values())),
        'marker_issues_after':dict(Counter(r['rule_id'] for r in new.values())),
        'marker_issue_changes':dict(Counter(r['change'] for r in differences)),
        'queue_ids_added':sorted(set(qnew)-set(qold)),'queue_ids_removed':sorted(set(qold)-set(qnew)),
        'queue_order_priority_and_selection_unchanged':structural(before['queue'])==structural(after['queue']),
        'queue_issue_references_changed':[tid for tid in sorted(set(qold)&set(qnew)) if qold[tid]['issue_ids']!=qnew[tid]['issue_ids']],
        'review_budget_before':before['summary']['review_budget'],
        'review_budget_after':after['summary']['review_budget'],
        'elapsed_seconds_before':before['manifest']['elapsed_seconds'],
        'elapsed_seconds_after':after['manifest']['elapsed_seconds'],
        'check_status_after':after['summary']['check_status']},differences


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before',default='outputs/truncation_fix/before')
    parser.add_argument('--after',default='outputs')
    parser.add_argument('--output',default='outputs/truncation_fix')
    parser.add_argument('--baseline-code',default='reports/truncation_fix/baseline_code/agent_qc')
    args=parser.parse_args();out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    summary={'data_revision':None,'policies':{}};diffs=[];runs=[]
    for policy in ('balanced','conservative'):
        old_path=Path(args.before)/policy;new_path=Path(args.after)/policy
        before=load_run(old_path);after=load_run(new_path)
        for name,h in after['manifest']['data_sha256'].items():
            if digest(Path(after['manifest']['data_root'])/name)!=h:
                raise ValueError('Raw input changed: '+name)
        result,difference=compare(before,after,Path(args.baseline_code))
        summary['policies'][policy]=result
        diffs.extend({'policy':policy,**d} for d in difference)
        runs += [old_path/'run_manifest.json',new_path/'run_manifest.json']
        summary['data_revision']=after['manifest']['config']['expected_revision']
    old_cmp=read_json(Path(args.before)/'comparison/comparison.json')
    new_cmp=read_json(Path(args.after)/'comparison/comparison.json')
    summary['policy_comparison_unchanged']=old_cmp==new_cmp
    summary['policy_comparison_changed_keys']=[k for k in sorted(set(old_cmp)|set(new_cmp)) if old_cmp.get(k)!=new_cmp.get(k)]
    fixtures_path=Path('tests/fixtures/truncation_cases.json')
    fixtures=read_json(fixtures_path)['cases']
    data_root=Path(load_run(Path(args.after)/'balanced')['manifest']['data_root'])
    verified=[]
    for line,record in rows(data_root/'sample/trajectories/trajectories.jsonl.zst'):
        for case in fixtures:
            if case['source']['line']!=line:continue
            step=int(case['source']['pointer'].split('/')[2])
            actual=record['steps'][step]['result']
            if record['trajectory_id']!=case['trajectory_id'] or actual!=case['result']:
                raise ValueError('Real regression fixture differs from pinned raw source')
            if hashlib.sha256(actual.encode()).hexdigest()!=case['result_sha256']:
                raise ValueError('Regression fixture hash differs')
            verified.append({'trajectory_id':case['trajectory_id'],'source':case['source']})
        if len(verified)==len(fixtures):break
    if len(verified)!=len(fixtures):raise ValueError('Regression source row missing')
    summary['real_regression_fixtures_verified']=verified
    (out/'impact.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    (out/'issue_changes.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False,sort_keys=True)+'\n' for r in diffs))
    files=runs+[Path(__file__),Path('scripts/compare_policies.py'),Path('scripts/prepare_review.py'),
                fixtures_path,out/'impact.json',out/'issue_changes.jsonl',
                Path(args.before)/'comparison/comparison.json',Path(args.after)/'comparison/comparison.json']
    files+=sorted(Path(args.baseline_code).glob('*.py'))
    (out/'impact_manifest.json').write_text(json.dumps({'files_sha256':{str(p):digest(p) for p in files},
        'note':'修复前后允许代码不同；原始数据、依赖和配置必须相同。原运行产物和代码均经哈希核对。'},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
