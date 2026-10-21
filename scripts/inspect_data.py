"""Initial structural profile, not a training admission pipeline."""
import argparse
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import time
import zstandard


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--data', default='data/gatcv6')
    p.add_argument('--output', default='reports/data_inspection.json')
    a = p.parse_args()
    start = time.monotonic()
    root = Path(a.data)
    raw = root / 'raw' / 'sample'
    checks = json.loads((raw / 'CHECKSUMS.json').read_text())
    result = {'revision': json.loads((root/'manifest.json').read_text())['revision'],
              'scope': 'all six published tables; initial structural profile only',
              'tables': {}, 'checksums': {}, 'errors': []}
    ids = {}
    labels = {}
    issues = Counter()
    examples = {}
    counts = Counter()
    step_types = Counter()
    for relative, expected in checks.items():
        path = raw / relative
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        result['checksums'][relative] = {'expected': expected, 'actual': actual, 'match': actual == expected}
        if actual != expected: raise ValueError('Checksum mismatch: '+relative)
        table = path.name.split('.')[0]
        rows = 0
        keys = set()
        table_ids = Counter()
        with path.open('rb') as source:
            reader = zstandard.ZstdDecompressor().stream_reader(source, read_across_frames=True)
            with io.TextIOWrapper(reader, encoding='utf-8') as text:
                for line_no, line in enumerate(text, 1):
                    try: r = json.loads(line)
                    except (ValueError, UnicodeError) as exc:
                        result['errors'].append({'file':relative,'line':line_no,'error':str(exc)})
                        continue
                    rows += 1
                    keys.update(r)
                    tid = r.get('trajectory_id')
                    if tid: table_ids[tid] += 1
                    if table == 'labels':
                        labels[tid] = r
                        counts['is_eval'] += bool(r.get('is_eval'))
                        counts['parent_id_present'] += bool(r.get('parent_trajectory_id'))
                    elif table == 'messages':
                        issues.update(r.get('ft_issues', []))
                        counts['ft_issues_empty'] += not r.get('ft_issues')
                        for issue in r.get('ft_issues', []):
                            if issue not in examples:
                                examples[issue] = {'trajectory_id':tid,'file':relative,'line':line_no,'path':'ft_issues','value':r['ft_issues']}
                        for m in r['messages']:
                            for c in m.get('tool_calls',[]):
                                counts['message_tool_calls'] += 1
                                try: json.loads(c['function']['arguments'])
                                except (ValueError,TypeError): counts['message_arguments_invalid_json'] += 1
                    elif table == 'trajectories':
                        counts['steps'] += len(r['steps'])
                        step_types.update(s['type'] for s in r['steps'])
                        counts['n_steps_mismatch'] += r['n_steps'] != len(r['steps'])
                        if tid in labels:
                            counts['label_n_steps_mismatch'] += labels[tid]['n_steps'] != r['n_steps']
                        for s in r['steps']:
                            if s['type'] in ('tool_call','retrieval','subagent'):
                                counts['call_steps'] += 1
                                counts['call_ok_null'] += s.get('ok') is None
                                counts['call_ok_false'] += s.get('ok') is False
                                counts['call_ok_true'] += s.get('ok') is True
                    elif table == 'error_recovery':
                        counts['recovered_true'] += r.get('recovered') is True
                        counts['recovered_false'] += r.get('recovered') is False
        ids[table] = table_ids
        result['tables'][table] = {'file':relative,'rows':rows,'unique_trajectory_ids':len(table_ids),'fields':sorted(keys)}
    base = set(ids['trajectories'])
    result['associations'] = {t:{'ids_not_in_trajectories':len(set(v)-base),
        'trajectories_not_in_table':len(base-set(v)),
        'duplicate_ids':sum(n-1 for n in v.values()) if t in ('messages','labels','trajectories') else None}
        for t,v in ids.items() if t!='tool_grammar'}
    result['counts'] = dict(counts)
    result['step_types'] = dict(step_types)
    result['ft_issues'] = dict(issues)
    result['issue_examples'] = examples
    result['unresolved_parent_ids'] = sum(bool(r.get('parent_trajectory_id')) and r['parent_trajectory_id'] not in base for r in labels.values())
    result['elapsed_seconds'] = round(time.monotonic()-start,3)
    out = Path(a.output)
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('tables','issue_examples','checksums')},ensure_ascii=False,indent=2))
    print('Rows:', {k:v['rows'] for k,v in result['tables'].items()})
    print('All published checksums match:', all(v['match'] for v in result['checksums'].values()))

if __name__ == '__main__': main()
