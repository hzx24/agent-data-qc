"""Verify an archive, then reproduce tests, both policies and draft in a fresh path.

Uses the invoking Python environment and its installed dependencies; no API calls.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from build_delivery import verify_archive, PREFIX


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def invoke(args,cwd):
    result=subprocess.run([sys.executable]+args,cwd=cwd,text=True,capture_output=True)
    if result.returncode:
        raise RuntimeError('Reproduction failed: '+repr(args)+'\n'+result.stdout+'\n'+result.stderr)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive',default='dist/agent-qc-delivery.zip')
    p.add_argument('--output',default='reports/交付包验证.json')
    args=p.parse_args();archive=Path(args.archive);count=verify_archive(archive);start=time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='agent-qc-delivery-check-') as temporary:
        with zipfile.ZipFile(archive) as z:z.extractall(temporary)
        root=Path(temporary)/PREFIX.rstrip('/')
        manifest=json.loads((root/'delivery_manifest.json').read_text())
        tests=invoke(['-m','unittest','discover','-s','tests','-v'],root)
        match=re.search(r'Ran (\d+) tests',tests.stderr)
        if not match:raise ValueError('Test count unavailable')
        facts={};elapsed={}
        for policy in ('balanced','conservative'):
            out='outputs/extracted_'+policy
            invoke(['-m','agent_qc','--config','configs/default.json','--policy',policy,'--output',out],root)
            facts[policy]={name:digest(root/out/name)==digest(root/'outputs'/policy/name)
                for name in ('issues.jsonl','decisions.jsonl','review_queue.jsonl','summary.json','rules.json','config.json')}
            if not all(facts[policy].values()):raise ValueError('Fact files differ: '+policy)
            elapsed[policy]=json.loads((root/out/'run_manifest.json').read_text())['elapsed_seconds']
        invoke(['scripts/compare_policies.py','--balanced','outputs/extracted_balanced',
                '--conservative','outputs/extracted_conservative','--output','outputs/extracted_comparison'],root)
        comparison_match=digest(root/'outputs/extracted_comparison/comparison.json')==digest(root/'outputs/comparison/comparison.json')
        if not comparison_match:raise ValueError('Policy comparison differs')
        draft_files=['sampling.json','reviews.jsonl','reviews.md','summary.json','human_review.json']
        before={name:digest(root/'reports/review_draft'/name) for name in draft_files}
        invoke(['scripts/prepare_review.py'],root);invoke(['scripts/render_review.py'],root)
        review_match={name:h==digest(root/'reports/review_draft'/name) for name,h in before.items()}
        if not all(review_match.values()):raise ValueError('Review regeneration differs')
        report={'status':'passed','verified_archive_files':count,
            'pipeline_code_sha256':manifest['pipeline_code_sha256'],
            'submission_source_sha256':manifest['submission_source_sha256'],
            'environment':'临时目录解压；使用调用脚本的已安装Python依赖环境，未宣称独立操作系统验证。',
            'tests_passed':int(match.group(1)),'fact_file_byte_matches':facts,
            'policy_comparison_byte_match':comparison_match,'draft_regeneration_byte_matches':review_match,
            'extracted_scan_elapsed_seconds':elapsed,'validation_elapsed_seconds':round(time.perf_counter()-start,3),
            'human_review_status':json.loads((root/'reports/review_draft/summary.json').read_text())['status'],
            'note':'验证文件加入最终包时不改变所验证的源码、原始数据、配置及事实结果。zip本身哈希见包外.sha256。'}
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    output.with_suffix('.txt').write_text(tests.stderr)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
