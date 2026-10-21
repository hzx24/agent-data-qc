"""Prepare a reproducible real-data review packet. Does not create human labels."""
import argparse
import hashlib
import io
import json
import random
from pathlib import Path
from collections import Counter,defaultdict
import zstandard

# Explicit purposeful boundary cases, all drawn from the existing audit queue.
# Chosen before the conditional random draw; IDs and reasons are recorded in sampling.json.
BOUNDARIES={
 '7daf4462d83e7964ecc06877':'失败后针对性修正，但最后返回为空且无最终回答',
 'e8e7875ed949d0b4242d3da4':'仅评测来源改变策略，且状态成功可伴随错误文本',
 '753530dd642d338581edde8e':'权限失败后换工具，评测来源及开放结尾同时命中',
 '34b796f5a03369d11b40e110':'开头是工具输出，未知工具是否实际可执行',
 '938fb20e9c2c354b2ac2f6f6':'只有一次调用，派生视图截短但原始结果完整',
 'eecd796755bb58f80dfefafa':'状态未知但无其他阻断标记，检查是否应保留',
 'c4704adb2a18332df39148b2':'失败密集且恢复视图关联不唯一',
 'd101de2b80d1abf850b43f07':'管线无风险标记的正常对照',
 'f6ee1f33426b334fc6e6d1a1':'全部调用状态未知，评测且无最终文本回答',
 '4fd7f1ffc160d75c53ad659f':'交互轨迹缺少清晰用户开头/结尾，核验边界价值',
}
HITS={
 'a11e0688dc7f141f22ea507b':'跨表结果差异、移除文本及schema冲突',
 '7bce5aa6ee3bc6fa68957d61':'截断标记误报的回归案例与参数兼容问题',
 '4e7f55928c33fd2fff1db7af':'replacements参数变体不应直接判缺参',
 'fffa584c9e10be4e287f1a94':'仅dirty-files摘要，不能重建改动',
 '9b0406d7ec2f9dc41a7d1824':'嵌套parameters记录仅含路径',
 '235c0f6f1675e4095344e9f1':'写入记录只有路径',
 '109c981e1c9043aac0b41131':'write的input承载内容但与schema不兼容',
 'd357581401a83f863e7f5fff':'task调用参数变体与发布schema不一致',
 '77e1e9ccc1420c26899e1759':'移除文本可能阻断上下文解释',
 '7db6c1d8a023ed1a889c6881':'仅一次调用的schema疑问，核验暂缓是否过度',
}

def rows(path):
    with path.open('rb') as f:
        with io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(f,read_across_frames=True),encoding='utf-8') as stream:
            for n,line in enumerate(stream,1):yield n,json.loads(line)

def readl(path):return [json.loads(x) for x in path.read_text().splitlines()]
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',default='data/gatcv6/raw')
    p.add_argument('--run',default='outputs/balanced')
    p.add_argument('--output',default='reports/review_draft')
    a=p.parse_args();data=Path(a.data);run=Path(a.run);out=Path(a.output)
    manifest=json.loads((run/'run_manifest.json').read_text())
    for name,h in manifest['data_sha256'].items():
        if sha(data/name)!=h:raise ValueError('Raw data changed: '+name)
    for name,h in manifest['output_sha256'].items():
        if sha(run/name)!=h:raise ValueError('Run output changed: '+name)
    queue=readl(run/'review_queue.jsonl');qid={r['trajectory_id']:r for r in queue}
    decisions={r['trajectory_id']:r for r in readl(run/'decisions.jsonl')}
    issues=defaultdict(list)
    for r in readl(run/'issues.jsonl'):issues[r['trajectory_id']].append(r)
    purpose={**BOUNDARIES,**HITS}
    if len(purpose)!=20 or not set(purpose)<=set(qid):raise ValueError('Purpose samples must be 20 unique in-budget IDs')
    pool=sorted(r['trajectory_id'] for r in queue if r['selection_reason']=='seeded_audit' and r['trajectory_id'] not in purpose)
    seed=20261020
    random_ids=random.Random(seed).sample(pool,10)
    groups=[('random',tid,'固定种子从剩余随机审计子池抽取') for tid in random_ids]
    groups += [('rule_hit',tid,why) for tid,why in HITS.items()]
    groups += [('boundary',tid,why) for tid,why in BOUNDARIES.items()]
    selected={tid for _,tid,_ in groups}
    if len(selected)!=30:raise ValueError('Need 30 distinct trajectories')
    out.mkdir(parents=True,exist_ok=True);(out/'evidence').mkdir(exist_ok=True)
    sampling={'status':'人工复核抽样及证据包；确认状态另见human_review.json与summary.json','revision':manifest['config']['expected_revision'],
      'seed':seed,'sample_size':30,'unique_sample_size':len(selected),'budget_queue_size':len(queue),
      'all_samples_in_existing_queue':True,'new_trajectory_ids_outside_queue':0,
      'allocation':{'random':10,'rule_hit':10,'boundary':10},
      'random_sampling':'先固定10个边界与10个规则命中案例；再从seeded_audit队列移除这些ID后的有序池中，用random.Random(seed).sample不放回抽10条。边界有意选择会影响随机池，随机组仅代表剩余审计子池，不是全量简单随机样本。',
      'random_pool_ids':pool,'random_pool_size':len(pool),
      'purpose_selection':'规则组有意覆盖参数缺失、schema变体、移除和跨表差异；边界组覆盖策略分歧、未知状态、合法视图截短、正常对照与恢复歧义。类别不凑齐：既有队列没有exclude样本。',
      'scope_of_reading':'核对输入/末尾、代表性规则命中位置及相关上下文和失败窗口；长轨迹未逐一核对全部命中，也未逐字阅读全部代码、完整重放或验收任务语义。各条关键证据和阅读局限见reviews；完整原始轨迹和训练消息在evidence中供人类核验。',
      'groups':[{'index':n,'group':g,'trajectory_id':tid,'selection_reason':why,'queue_rank':qid[tid]['rank']} for n,(g,tid,why) in enumerate(groups,1)],
      'pipeline_code_sha256':manifest['code_sha256'],'queue_sha256':sha(run/'review_queue.jsonl'),
      'raw_sha256':manifest['data_sha256'],'script_sha256':sha(Path(__file__))}
    packets={tid:{'trajectory_id':tid,'decision':decisions[tid],'queue':qid[tid],'issues':issues[tid]} for tid in selected}
    for table in ('trajectories','messages','labels'):
        for line,r in rows(data/'sample'/table/(table+'.jsonl.zst')):
            if r['trajectory_id'] in selected:
                packets[r['trajectory_id']][table]={'file':f'sample/{table}/{table}.jsonl.zst','line':line,'data':r}
    for table,relative in [('sft_steps','views/sft_steps.jsonl.zst'),('error_recovery','views/error_recovery.jsonl.zst')]:
        for line,r in rows(data/'sample'/relative):
            if r['trajectory_id'] in selected:
                packets[r['trajectory_id']].setdefault(table,[]).append({'file':'sample/'+relative,'line':line,'data':r})
    with (out/'dossiers.jsonl').open('w') as dossier:
        for n,(group,tid,why) in enumerate(groups,1):
            packet=packets[tid]
            (out/'evidence'/(tid+'.json')).write_text(json.dumps(packet,ensure_ascii=False,indent=2)+'\n')
            t=packet['trajectories']['data'];m=packet['messages']['data'];steps=t['steps']
            user=next((s for s in steps if s.get('type')=='user' and not s.get('is_wrapper')),None)
            # Compact excerpts are only for navigation; each full evidence file stays intact.
            selected_issues=[]
            for rule in sorted({i['rule_id'] for i in packet['issues']}):
                matches=[i for i in packet['issues'] if i['rule_id']==rule]
                selected_issues.extend(matches[:2])
            failed=[i for i,s in enumerate(steps) if s.get('ok') is False]
            selected_failed=failed[:2]
            if failed and failed[-1] not in selected_failed:selected_failed.append(failed[-1])
            def trim(x,limit=550):
                if isinstance(x,str):return x[:limit]+('…' if len(x)>limit else '')
                if isinstance(x,list):return [trim(v,limit) for v in x]
                if isinstance(x,dict):return {k:trim(v,limit) for k,v in x.items()}
                return x
            compact={'index':n,'group':group,'trajectory_id':tid,'selection_reason':why,'queue_rank':qid[tid]['rank'],
              'pipeline_decision':packet['decision']['decision'],'reason_codes':packet['decision']['reason_codes'],
              'metadata':{k:v for k,v in t.items() if k not in ('steps','tokens','models','redaction')},
              'first_user':trim(user),'first_two_steps':trim(steps[:2]),'last_two_steps':trim(steps[-2:]),
              'message_boundary':trim([{'index':0,**m['messages'][0]},{'index':len(m['messages'])-1,**m['messages'][-1]}]),
              'issues':selected_issues,'issue_counts':dict(Counter(i['rule_id'] for i in packet['issues'])),
              'failure_windows':[{'step':i,'steps':trim(steps[i:i+5])} for i in selected_failed],
              'raw_line':packet['trajectories']['line'],'messages_line':packet['messages']['line']}
            dossier.write(json.dumps(compact,ensure_ascii=False,sort_keys=True)+'\n')
    sampling['evidence_sha256']={tid:sha(out/'evidence'/(tid+'.json')) for tid in sorted(selected)}
    (out/'sampling.json').write_text(json.dumps(sampling,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'groups':sampling['groups'],'random_pool_size':len(pool),'output':str(out)},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
