"""Validate review evidence and merge explicit human confirmations.

Never invents confirmations or changes the original pipeline decisions.
"""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pointer_value(value, pointer):
    if not pointer:
        return value
    if not pointer.startswith('/'):
        raise ValueError('Invalid JSON pointer: ' + pointer)
    for part in pointer[1:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def resolve_evidence(packet, spec):
    node = packet[spec['table']]
    if isinstance(node, list):
        matches = [r for r in node if r['line'] == spec['line']]
        if len(matches) != 1:
            raise ValueError('Evidence row is absent or ambiguous')
        node = matches[0]
    if 'line' in spec and node['line'] != spec['line']:
        raise ValueError('Wrong evidence line')
    value = pointer_value(node['data'], spec['pointer'])
    if 'decoded_pointer' in spec:
        # tools is serialized JSON in this dataset. Never pretend /tools/1 is
        # a pointer into the original row; state the extra decoding explicitly.
        value = pointer_value(json.loads(value), spec['decoded_pointer'])
    value_text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    needle = spec.get('contains', '')
    if needle and needle not in value_text:
        raise ValueError('Evidence excerpt not present: ' + needle)
    start = max(0, value_text.find(needle) - 70) if needle else 0
    end = start + 520
    return {'file': node['file'], 'line': node['line'], 'pointer': spec['pointer'],
            'decoded_pointer': spec.get('decoded_pointer'),
            'packet_pointer': '/' + spec['table'] + '/data' + spec['pointer']
            if not isinstance(packet[spec['table']], list) else None,
            'excerpt': value_text[start:end], 'excerpt_start': start,
            'excerpt_is_partial': start > 0 or end < len(value_text),
            'value_sha256': hashlib.sha256(value_text.encode()).hexdigest()}


def assemble(sampling, annotations, packets):
    groups = sampling['groups']
    ids = [g['trajectory_id'] for g in groups]
    labels = annotations['items']
    if len(ids) != 30 or len(set(ids)) != 30:
        raise ValueError('Expected 30 unique sampled IDs')
    label_ids = [a['trajectory_id'] for a in labels]
    if len(label_ids) != 30 or set(label_ids) != set(ids):
        raise ValueError('Annotations must cover exactly the 30 sampled IDs')
    by_id = {a['trajectory_id']: a for a in labels}
    output = []
    for g in groups:
        tid = g['trajectory_id']
        a = by_id[tid]
        p = packets[tid]
        if a['index'] != g['index'] or p['trajectory_id'] != tid:
            raise ValueError('Sample index/ID mismatch')
        if a['recommendation'] not in ('admit', 'defer', 'exclude'):
            raise ValueError('Invalid recommendation')
        if not all(a.get(k) for k in ('rationale', 'uncertainty', 'change_conditions', 'evidence')):
            raise ValueError('Incomplete review')
        evidence = [resolve_evidence(p, e) for e in a['evidence']]
        steps = p['trajectories']['data']['steps']
        calls = [s for s in steps if 'tool' in s]
        pipeline = p['decision']['decision']
        output.append({**a, 'group': g['group'], 'selection_reason': g['selection_reason'],
            'queue_rank': p['queue']['rank'], 'status': 'pending_human_confirmation',
            'reviewer_type': 'unconfirmed', 'pipeline_decision': pipeline,
            'proposal_changes_pipeline_decision': a['recommendation'] != pipeline,
            'rule_ids': sorted({i['rule_id'] for i in p['issues']}),
            'evidence': evidence, 'evidence_packet': 'evidence/' + tid + '.json',
            'observed_counts': {'raw_steps': len(steps), 'raw_tool_calls': len(calls),
                'empty_results': sum(not s.get('result') for s in calls),
                'unknown_status': sum(s.get('ok') is None for s in calls)},
            'coverage': sampling['scope_of_reading'],
            'human_review': {'reviewer': None, 'reviewed_at': None, 'decision': None,
                'agrees_with_record': None, 'notes': None}})
    return output


def merge_human_reviews(rows, document, annotations_sha256):
    """Apply only explicit, version-bound reviewer decisions; missing is pending."""
    confirmations=document.get('reviews',[])
    if not confirmations:
        return rows
    ids=[r['trajectory_id'] for r in confirmations]
    if len(ids)!=len(set(ids)) or not set(ids)<=set(r['trajectory_id'] for r in rows):
        raise ValueError('Duplicate or unknown human review ID')
    completed=[r for r in confirmations if r.get('decision') is not None]
    if completed and (document.get('annotations_sha256')!=annotations_sha256 or not document.get('confirmation_source')):
        raise ValueError('Human confirmation must reference this annotation version and its source')
    by_id={r['trajectory_id']:r for r in completed}
    for row in rows:
        h=by_id.get(row['trajectory_id'])
        if h is None:continue
        if h['decision'] not in ('admit','defer','exclude') or not h.get('reviewer') or not h.get('reviewed_at'):
            raise ValueError('Incomplete human confirmation')
        row['human_review']={k:h.get(k) for k in ('reviewer','reviewed_at','decision','agrees_with_record','notes')}
        row['human_review']['confirmation_source']=document['confirmation_source']
        row['human_review']['annotations_sha256']=annotations_sha256
        row['status']='human_confirmed'
        row['reviewer_type']='human'
        row['recommendation']=h['decision']
        row['proposal_changes_pipeline_decision']=row['recommendation']!=row['pipeline_decision']
    return rows


def markdown(rows):
    names = {'admit': '入选', 'defer': '暂缓', 'exclude': '排除'}
    groups = {'random': '条件随机样本', 'rule_hit': '规则命中样本', 'boundary': '边界样本'}
    count=sum(r['status']=='human_confirmed' for r in rows)
    lines = ['# 30条真实轨迹人工复核记录', '',
        f'**已人工确认：{count}/{len(rows)}条。** 确认依据、审核人及日期逐条记录。原始管线输出与人工结论分别保留，未自动覆盖全量结果。', '',
        '证据行号为zstd解压后的JSONL行号（从1开始），JSON Pointer数组下标从0开始。引文是导航片段，不能替代完整上下文；完整记录见每条证据包。', '',
        '抽样范围、预算口径和重点案例见 [复核说明](README.md)。填写正式结果可编辑 [human_review.json](human_review.json)，重跑生成器不会覆盖该文件。', '']
    for r in rows:
        lines += [f"## {r['index']:02d}. {r['trajectory_id']}", '',
            f"{groups[r['group']]}；既有队列排名 {r['queue_rank']}；规则：{', '.join(r['rule_ids']) or '无命中'}。", '',
            f"管线：**{names[r['pipeline_decision']]}**；复核结论：**{names[r['recommendation']]}**；状态：{'已人工确认' if r['status']=='human_confirmed' else '待人工确认'}。", '',
            '**依据与取舍：** ' + r['rationale'], '', '**尚不确定：** ' + r['uncertainty'], '',
            '**改判条件：** ' + r['change_conditions'], '',
            f"[完整原始证据包]({r['evidence_packet']})；原始步骤 {r['observed_counts']['raw_steps']}，调用 {r['observed_counts']['raw_tool_calls']}，空返回 {r['observed_counts']['empty_results']}。", '',
            '**关键证据位置：**', '']
        for e in r['evidence']:
            decoded = ('；该字段JSON解码后再取 `' + e['decoded_pointer'] + '`') if e['decoded_pointer'] else ''
            lines += [f"- `{e['file']}:{e['line']}#{e['pointer']}`" + decoded, '',
                '```text', e['excerpt'].replace('```', '` ` `'), '```', '']
        h=r['human_review']
        lines += [f"**人工复核：** {h['reviewer'] or '待填写'}；完成确认日期：{h['reviewed_at'] or '待填写'}；说明：{h['notes'] or '待填写'}。", '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--annotations', default='reports/review_annotations.json')
    parser.add_argument('--output', default='reports/review_draft')
    parser.add_argument('--data', default='data/gatcv6/raw')
    parser.add_argument('--run', default='outputs/balanced')
    args = parser.parse_args()
    out = Path(args.output)
    sampling = json.loads((out / 'sampling.json').read_text())
    for name, expected in sampling['raw_sha256'].items():
        if sha(Path(args.data) / name) != expected:
            raise ValueError('Raw source changed: ' + name)
    if sha(Path(args.run) / 'review_queue.jsonl') != sampling['queue_sha256']:
        raise ValueError('Review queue changed')
    queue_ids = {json.loads(line)['trajectory_id'] for line in
                 (Path(args.run) / 'review_queue.jsonl').read_text().splitlines()}
    packets = {g['trajectory_id']: json.loads((out / 'evidence' /
        (g['trajectory_id'] + '.json')).read_text()) for g in sampling['groups']}
    if not set(packets) <= queue_ids:
        raise ValueError('Review would exceed existing queue scope')
    for tid, expected in sampling['evidence_sha256'].items():
        if sha(out / 'evidence' / (tid + '.json')) != expected:
            raise ValueError('Evidence packet changed: ' + tid)
    annotations = json.loads(Path(args.annotations).read_text())
    result = assemble(sampling, annotations, packets)
    human_path = out / 'human_review.json'
    if human_path.exists():
        result=merge_human_reviews(result,json.loads(human_path.read_text()),sha(Path(args.annotations)))
    (out / 'reviews.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False, sort_keys=True) + '\n' for r in result))
    (out / 'reviews.md').write_text(markdown(result) + '\n')
    if not human_path.exists():
        human_path.write_text(json.dumps({'instructions': '由实际审核人填写；生成器不覆盖此文件。完成后需注明确认来源并绑定分析稿SHA256。',
            'reviews': [{'index': r['index'], 'trajectory_id': r['trajectory_id'], **r['human_review']} for r in result]},
            ensure_ascii=False, indent=2) + '\n')
    changes = [{'index': r['index'], 'trajectory_id': r['trajectory_id'],
                'before': r['pipeline_decision'], 'review_after': r['recommendation'],
                'finding_type': r['finding_type']} for r in result if r['proposal_changes_pipeline_decision']]
    confirmed=sum(r['status']=='human_confirmed' for r in result)
    summary = {'status': 'human_review_complete' if confirmed==len(result) else 'pending_human_confirmation', 'count': len(result),
        'human_confirmed_count': confirmed, 'human_confirmation_source': '提交者在本对话中的明确确认；逐条记录见human_review.json',
        'synthetic_cases_in_review_count': 0, 'allocation': dict(Counter(r['group'] for r in result)),
        'pipeline_decisions': dict(Counter(r['pipeline_decision'] for r in result)),
        'review_recommendations': dict(Counter(r['recommendation'] for r in result)),
        'proposed_changes': changes, 'additional_budget_ids': 0,
        'confirmed_rule_false_positive': {'trajectory_id': result[11]['trajectory_id'],
            'rule_id': 'RECORDED_TRUNCATION', 'pointer': '/steps/27/result',
            'fix_status': 'fixed_and_full_scan_verified',
            'explanation': '原规则误报已修复：[TRUNCATED]位于测试断言字符串，当前仅记录TRUNCATION_LITERAL。整轨迹仍因独立schema问题暂缓。'},
        'metrics_warning': '条件随机与有意选样混合；不是全量准确率、漏报率或误报率估计。人工复核与既有全量结果分开保存，未自动覆盖管线处置。'}
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    tracked = [out / 'sampling.json', Path(args.annotations), Path(__file__),
        Path('scripts/prepare_review.py'), human_path, out / 'reviews.jsonl', out / 'reviews.md', out / 'summary.json']
    tracked += [p for p in (out / 'README.md', out / 'validation_tests.txt',
                            Path('tests/test_review.py')) if p.exists()]
    tracked += sorted((out / 'evidence').glob('*.json'))
    (out / 'review_manifest.json').write_text(json.dumps({'revision': sampling['revision'],
        'pipeline_code_sha256': sampling['pipeline_code_sha256'],
        'raw_sha256': sampling['raw_sha256'], 'queue_sha256': sampling['queue_sha256'],
        'files_sha256': {str(p): sha(p) for p in tracked},
        'note': '哈希同时绑定证据、分析稿及人工确认文件；改变分析稿需重新确认，原始管线结果不自动回写。'}, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
