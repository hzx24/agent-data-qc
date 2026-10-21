"""Reproduce two scans and compare actual decisions. No model/API or labels invented."""
import argparse
import csv
import hashlib
import io
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
import zstandard

POLICIES=('balanced','conservative')
STRICT_RULES={'STRICT_EVAL','STRICT_BOUNDARY'}


def read_json(path):
    return json.loads(path.read_text())


def read_jsonl(path):
    with path.open() as f:
        return [json.loads(line) for line in f]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_compatible(left,right):
    """Reject comparisons confounded by input, code, budget or ranking differences."""
    for field in ('code_sha256','data_sha256','dependencies'):
        if left[field]!=right[field]:raise ValueError('Runs differ in '+field)
    a={k:v for k,v in left['config'].items() if k!='policy'}
    b={k:v for k,v in right['config'].items() if k!='policy'}
    if a!=b:raise ValueError('Runs must differ only in policy')
    if [left['config']['policy'],right['config']['policy']]!=list(POLICIES):
        raise ValueError('Expected balanced then conservative')


def load_run(path):
    manifest=read_json(path/'run_manifest.json')
    for name,expected in manifest['output_sha256'].items():
        if digest(path/name)!=expected:raise ValueError('Output was modified: '+str(path/name))
    summary=read_json(path/'summary.json')
    if summary['batch_problems'] or summary['check_status'].get('failed',0):
        raise ValueError('Cannot recommend from incomplete run: '+str(path))
    rows=read_jsonl(path/'decisions.jsonl');decisions={r['trajectory_id']:r for r in rows}
    if len(rows)!=len(decisions):raise ValueError('Duplicate decision ID')
    return {'manifest':manifest,'summary':summary,'decisions':decisions,
            'queue':read_jsonl(path/'review_queue.jsonl'),'issues':read_jsonl(path/'issues.jsonl')}


def changes(left,right):
    if set(left)!=set(right):raise ValueError('Trajectory sets differ')
    matrix=Counter();changed=[];same_decision_new_reasons=[]
    for tid in sorted(left):
        a,b=left[tid],right[tid]
        matrix[a['decision']+'->'+b['decision']]+=1
        added=sorted(set(b['reason_codes'])-set(a['reason_codes']))
        if a['decision']!=b['decision']:
            changed.append({'trajectory_id':tid,'balanced':a['decision'],'conservative':b['decision'],
              'added_reason_codes':added,'policy_causes':sorted(STRICT_RULES.intersection(added)),
              'balanced_reason_codes':a['reason_codes'],'conservative_reason_codes':b['reason_codes'],
              'balanced_review_status':a['review_status'],'conservative_review_status':b['review_status'],
              'sources':a['sources'],'tool_calls':a['tool_calls']})
        elif added:
            same_decision_new_reasons.append({'trajectory_id':tid,'decision':a['decision'],'added_reason_codes':added})
    return dict(sorted(matrix.items())),changed,same_decision_new_reasons


def source_profiles(data):
    profiles={}
    path=data/'sample/trajectories/trajectories.jsonl.zst'
    with path.open('rb') as f:
        with io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(f,read_across_frames=True),encoding='utf-8') as stream:
            for line_no,line in enumerate(stream,1):
                r=json.loads(line);steps=r['steps'];calls=[s for s in steps if s['type'] in ('tool_call','retrieval','subagent')]
                failed=[i for i,s in enumerate(steps) if s['type'] in ('tool_call','retrieval','subagent') and s.get('ok') is False]
                windows=sum(any(s['type'] in ('tool_call','retrieval') and s.get('ok') is True for s in steps[i+1:i+5]) for i in failed)
                profiles[r['trajectory_id']]={'steps':len(steps),'tool_calls':len(calls),'failed_calls':len(failed),
                  'unknown_calls':sum(s.get('ok') is None for s in calls),'successful_followup_windows':windows,
                  'is_eval':r['is_eval'],'eval_kind':r.get('eval_kind'),'session_kind':r.get('session_kind'),
                  'prompt_language':r.get('prompt_language'),'template_group_size':r.get('template_group_size'),
                  'source':{'file':'sample/trajectories/trajectories.jsonl.zst','line':line_no,'pointer':''}}
    return profiles


def retained(decisions,profiles):
    out=Counter();languages=Counter();sessions=Counter()
    for tid,row in decisions.items():
        if row['decision']!='admit':continue
        p=profiles[tid];out['trajectories']+=1
        for k in ('steps','tool_calls','failed_calls','unknown_calls','successful_followup_windows'):out[k]+=p[k]
        out['trajectories_with_failures']+=p['failed_calls']>0
        out['trajectories_with_unknown_status']+=p['unknown_calls']>0
        out['eval_flagged_trajectories']+=p['is_eval'] is True
        out['open_boundary_trajectories']+='OPEN_BOUNDARY' in row['risk_codes']
        out['shared_template_trajectories']+=isinstance(p['template_group_size'],int) and p['template_group_size']>1
        out['uncertain_recovery_view_trajectories']+='RECOVERY_UNCERTAIN' in row['risk_codes']
        languages[p['prompt_language'] or 'unknown']+=1;sessions[p['session_kind'] or 'unknown']+=1
    return {**dict(out),'prompt_language_counts':dict(sorted(languages.items())),'session_kind_counts':dict(sorted(sessions.items()))}


def compare(left_dir,right_dir,data,output):
    runs={POLICIES[0]:load_run(left_dir),POLICIES[1]:load_run(right_dir)}
    a,b=runs.values();check_compatible(a['manifest'],b['manifest'])
    common_b=[row for row in b['issues'] if row['rule_id'] not in STRICT_RULES]
    if a['issues']!=common_b:
        raise ValueError('Common factual findings differ; this is not a policy-only comparison')
    # Metrics must be computed from exactly the same raw trajectory file used by both runs.
    for name,expected in a['manifest']['data_sha256'].items():
        if digest(data/name)!=expected:raise ValueError('Raw data changed since scans: '+name)
    matrix,changed,same_decision=changes(a['decisions'],b['decisions'])
    profiles=source_profiles(data)
    if set(profiles)!=set(a['decisions']):raise ValueError('Raw trajectory coverage differs')
    issues={r['issue_id']:r for r in b['issues']}
    causes=Counter()
    for row in changed:
        tid=row['trajectory_id'];row['profile']=profiles[tid]
        row['policy_evidence']=[issues[i] for i in b['decisions'][tid]['issue_ids'] if issues[i]['rule_id'] in STRICT_RULES]
        causes['+'.join(row['policy_causes'])]+=1
    measures={name:retained(run['decisions'],profiles) for name,run in runs.items()}
    queue_ids={name:{r['trajectory_id'] for r in run['queue']} for name,run in runs.items()}
    changed_ids={r['trajectory_id'] for r in changed}
    queue_metrics={}
    for name,run in runs.items():
        q=run['queue'];defer_ids={tid for tid,r in run['decisions'].items() if r['decision']=='defer'}
        queue_metrics[name]={**run['summary']['review_budget'],
          'composition':dict(Counter(r['selection_reason']+':'+r['current_decision'] for r in q)),
          'changed_trajectories_queued':len(changed_ids&queue_ids[name]),
          'deferred_queued':len(defer_ids&queue_ids[name]),
          'deferred_coverage_fraction':len(defer_ids&queue_ids[name])/len(defer_ids) if defer_ids else None,
          'hypothetical_capacity_shortfall_even_if_all_118_reviewed_defer':max(0,len(defer_ids)-run['summary']['review_budget']['maximum'])}
    queue_compare={'intersection':len(queue_ids['balanced']&queue_ids['conservative']),
       'union':len(queue_ids['balanced']|queue_ids['conservative']),
       'warning':'两队列是相同批次的替代方案，不能分别执行后合并算作118条预算；推荐仅采用balanced的118条队列。'}
    differences={k:measures['balanced'].get(k,0)-measures['conservative'].get(k,0)
       for k in measures['balanced'] if isinstance(measures['balanced'][k],int)}
    summary={'scope':{'revision':a['summary']['dataset_revision_recorded'],'trajectories':len(profiles),
          'same_code_and_data_verified':True,'common_factual_findings_identical':True,'only_config_difference':'policy'},
      'decision_counts':{name:run['summary']['decisions'] for name,run in runs.items()},
      'transition_matrix':matrix,'changed_trajectory_count':len(changed),'changed_cause_counts':dict(sorted(causes.items())),
      'same_decision_with_additional_blocking_reasons':len(same_decision),
      'retained':measures,'balanced_minus_conservative_retained':differences,
      'review':queue_metrics,'queue_comparison':queue_compare,
      'recommendation':'balanced',
      'recommendation_origin':'实现者基于本轮业务目标与下述全量计数作出的选择；不是学习得到的效用或准确率估计。',
      'recommendation_basis':'当前目标包含探索与失败恢复；保守策略额外隔离的是评测来源和起止边界，不是新增已证实损坏。保留相同硬约束，避免将有限人工预算花在批量豁免来源/边界标记上。',
      'metric_limits':['这些是全量记录特征和处置计数，不是准确率、误报率或任务成功率。',
         'successful_followup_windows为原始步骤推导的后四步成功启发式，不证明因果修复。',
         '保留工具调用数不是独立任务数、有效token数或模型收益；没有训练实验。',
         '未提供目标评测集，无法计算真实污染率。is_eval=false仍可能含评测来源。',
         '两种策略都未验证任务语义正确性或完成全面隐私审计。'],
      'switch_conditions':[
        '目标改为只训练用户起点和文本结尾完整的终局回答，且不允许片段训练：转向保守边界门槛。',
        '业务规定禁止所有预设评测来源：采用保守评测隔离；这属于来源政策而非检测到损坏。',
        '取得独立评测集并证实重叠：按真实重叠隔离，未必要把全部is_eval轨迹排除。',
        '分层复核证明开放边界缺少关键动作反馈，或对照训练显示额外数据显著损害目标能力：收紧相应准入规则。',
        '允许片段训练/损失屏蔽，或补齐原始schema和环境：重新评估暂缓数据，可放宽整轨迹门槛。']}
    output=output.resolve()
    if output in (left_dir.resolve(),right_dir.resolve()) or output==data.resolve() or data.resolve() in output.parents:
        raise ValueError('Comparison output must not overwrite run/raw directories')
    output.mkdir(parents=True,exist_ok=True)
    def json_file(name,v):
        (output/name).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
    def json_lines(name,rows):
        with (output/name).open('w') as f:
            for row in rows:f.write(json.dumps(row,ensure_ascii=False,sort_keys=True)+'\n')
    json_lines('decision_changes.jsonl',changed)
    json_lines('same_decision_reason_changes.jsonl',same_decision)
    json_file('comparison.json',summary)
    with (output/'decision_changes.csv').open('w',newline='',encoding='utf-8-sig') as f:
        fields=['trajectory_id','balanced','conservative','policy_causes','tool_calls','steps','failed_calls',
                'is_eval','eval_kind','balanced_review_status','conservative_review_status','trajectory_line','messages_line']
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for row in changed:
            locs={s['file']:s['line'] for s in row['sources']}
            w.writerow({'trajectory_id':row['trajectory_id'],'balanced':row['balanced'],'conservative':row['conservative'],
              'policy_causes':'+'.join(row['policy_causes']),'tool_calls':row['tool_calls'],
              'steps':row['profile']['steps'],'failed_calls':row['profile']['failed_calls'],
              'is_eval':row['profile']['is_eval'],'eval_kind':row['profile']['eval_kind'],
              'balanced_review_status':row['balanced_review_status'],'conservative_review_status':row['conservative_review_status'],
              'trajectory_line':locs['sample/trajectories/trajectories.jsonl.zst'],
              'messages_line':locs['sample/messages/messages.jsonl.zst']})
    # Queue change list includes unchanged decisions whose review allocation changed.
    json_lines('queue_changes.jsonl',[{'trajectory_id':tid,'balanced_queued':tid in queue_ids['balanced'],
       'conservative_queued':tid in queue_ids['conservative'],'balanced':a['decisions'][tid]['decision'],
       'conservative':b['decisions'][tid]['decision']} for tid in sorted(queue_ids['balanced']^queue_ids['conservative'])])
    json_file('comparison_manifest.json',{'comparison_script_sha256':digest(Path(__file__)),
       'pipeline_code_sha256':a['manifest']['code_sha256'],'data_sha256':a['manifest']['data_sha256'],
       'runs':{name:{'config':run['manifest']['config'],'elapsed_seconds':run['manifest']['elapsed_seconds'],
                     'output_sha256':run['manifest']['output_sha256']} for name,run in runs.items()},
       'result_sha256':{p.name:digest(p) for p in sorted(output.iterdir()) if p.name in (
          'comparison.json','decision_changes.jsonl','decision_changes.csv','same_decision_reason_changes.jsonl','queue_changes.jsonl')}})
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',action='store_true',help='Run both policies before comparing (sequential CPU scans)')
    p.add_argument('--config',default='configs/default.json')
    p.add_argument('--data',default='data/gatcv6/raw')
    p.add_argument('--balanced',default='outputs/balanced')
    p.add_argument('--conservative',default='outputs/conservative')
    p.add_argument('--output',default='outputs/comparison')
    a=p.parse_args()
    try:
        if a.run:
            for policy,directory in zip(POLICIES,(a.balanced,a.conservative)):
                subprocess.run([sys.executable,'-m','agent_qc','--config',a.config,'--data',a.data,
                    '--policy',policy,'--output',directory],check=True)
        summary=compare(Path(a.balanced),Path(a.conservative),Path(a.data),Path(a.output))
    except (OSError,ValueError,KeyError,subprocess.CalledProcessError) as exc:
        print('Comparison failed: '+str(exc),file=sys.stderr);return 2
    print(json.dumps({k:summary[k] for k in ('decision_counts','transition_matrix','changed_cause_counts','balanced_minus_conservative_retained','review','queue_comparison','recommendation')},ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__':sys.exit(main())
