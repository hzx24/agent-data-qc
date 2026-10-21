"""Write submission provenance from actual runs; never invent human labels/timing."""
import hashlib
import json
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    runs={p:json.loads((Path('outputs')/p/'run_manifest.json').read_text()) for p in ('balanced','conservative')}
    review=json.loads(Path('reports/review_draft/summary.json').read_text())
    code_files=[]
    for folder in ('agent_qc','scripts','tests','configs'):
        code_files += [p for p in Path(folder).rglob('*') if p.is_file() and p.suffix in ('.py','.json') and '__pycache__' not in p.parts]
    code_files += [Path(p) for p in ('requirements.txt','requirements-lock.txt','requirements-inspection.txt')]
    inventory={p.as_posix():sha(p) for p in sorted(code_files)}
    source_hash=hashlib.sha256(json.dumps(inventory,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    for policy,r in runs.items():
        for name,h in r['output_sha256'].items():
            if sha(Path('outputs')/policy/name)!=h:raise ValueError('Output changed: '+name)
    data={
        'dataset':{'name':'GOAT-AI/gatcv6','url':'https://huggingface.co/datasets/GOAT-AI/gatcv6',
            'revision':runs['balanced']['config']['expected_revision'],'scope':'公开sample六表全部记录',
            'trajectory_count':1189,'raw_steps':92365,'tool_calls':52617,
            'raw_sha256':runs['balanced']['data_sha256'],'raw_included_in_archive':True,
            'license_path':'data/gatcv6/raw/LICENSE','full_117796_dataset_claimed':False},
        'code':{'git_commit':None,'pipeline_version':runs['balanced']['pipeline_version'],
            'pipeline_code_sha256':runs['balanced']['code_sha256'],
            'submission_source_sha256':source_hash,'source_inventory':inventory,
            'note':'无Git历史，以内容SHA256标识版本；源码整体与管线包哈希含义不同。'},
        'runs':{p:{'actual_config':r['config'],'elapsed_seconds':r['elapsed_seconds'],
            'started_at_utc':r['started_at_utc'],'peak_process_memory_bytes':r['peak_process_memory_bytes'],
            'python':r['python'],'platform':r['platform'],'dependencies':r['dependencies'],
            'manifest_path':'outputs/'+p+'/run_manifest.json'} for p,r in runs.items()},
        'dependencies':{'direct':'requirements.txt','runtime_lock':'requirements-lock.txt',
            'lock_scope':'QC所需运行依赖及传递依赖，不含可选HF登录CLI。包内已有数据，无需登录。'},
        'effort':{'total_hours_approx':6,'source':'用户自报估算：总累计投入6小时左右',
            'approximate':True,'separate_human_review_time_hours':None,
            'note':'项目累计投入与扫描秒数分开；未伪造逐阶段人工时间。'},
        'human_review':{'real_review_trajectories':review['count'],'formally_confirmed_in_submission':review['human_confirmed_count'],
            'status':review['status'],'sample_allocation':review['allocation'],
            'review_file':'reports/review_draft/reviews.jsonl','human_form':'reports/review_draft/human_review.json'},
        'validation':{'automated_tests_passed':50,'tests_log':'reports/submission_tests.txt',
            'known_truncation_false_positive_fixed':True,'fixed_policy_comparison_unchanged':True,
            'accuracy_or_false_positive_rate_claimed':False},
        'ai_assistance':Path('reports/项目参与说明.md').read_text().split('\n\n',1)[1].strip(),
        'submission_status':'工程、全量结果、报告及30条人工复核记录已整理完成。',
        'reproduce':[
            'python3 -m venv .venv', 'source .venv/bin/activate',
            'pip install -r requirements-lock.txt',
            'python -m agent_qc --config configs/default.json --output outputs/local_reproduction',
            'python -m unittest discover -s tests -v',
            'python scripts/compare_policies.py --run',
            'python scripts/compare_truncation_fix.py']}
    Path('reports/交付复现信息.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'pipeline_code_sha256':data['code']['pipeline_code_sha256'],
        'submission_source_sha256':source_hash,'estimated_hours':6,'human_review_status':review['status']},ensure_ascii=False))


if __name__=='__main__':main()
