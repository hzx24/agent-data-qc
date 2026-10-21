import hashlib
import io
import json
import math
import platform
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import zstandard
from importlib.metadata import version
from . import __version__
from .rules import RULES, CALL_TYPES, canonical, source, check_messages, check_trajectory, check_cross, check_recovery

TABLES = {
 'trajectories':'trajectories/trajectories.jsonl.zst',
 'messages':'messages/messages.jsonl.zst',
 'labels':'labels/labels.jsonl.zst',
 'sft_steps':'views/sft_steps.jsonl.zst',
 'error_recovery':'views/error_recovery.jsonl.zst',
 'tool_grammar':'views/tool_grammar.jsonl.zst',
}
DEFAULTS={'policy':'balanced','review_fraction':0.1,'audit_fraction_of_budget':0.2,'seed':20261020,
          'expected_revision':'216fb6e2c9449dd8eeab0a3b9233e89ea08d1f7a','expected_trajectories':1189}
LIMITATIONS=[
 '未执行原始命令、未重放代码、未核验视觉产物；不保证任务成功或代码正确。',
 '工具schema由发布者重建；兼容性检查不证明原始工具语义正确。',
 '无指定目标模型/模板/tokenizer，不检查实际token窗口或训练框架损失掩码。',
 '没有目标评测集；is_eval与共享开头只作为风险线索，未完成外部去污染或模糊去重。',
 '不是全面隐私审计；保留原始脱敏占位符，不尝试恢复隐藏内容。',
 'error_recovery无步骤索引；非唯一匹配明确为不确定，窗口成功不是因果恢复。',
 '本轮整轨迹准入，无自动裁剪、补写参数或人工判决；队列是待复核建议。',
 '发布方独立脱敏可能改变视图前缀；无法证明等价的跨表差异暂缓，可能产生误报。',
 '截断标记按行和源码字面量语境启发判断；未知语境暂缓，未覆盖所有语言语法或无标记截断。',
]

def shorten(value):
    if isinstance(value,str):return value if len(value)<=400 else value[:400]+'…[evidence excerpt]'
    if isinstance(value,list):return [shorten(v) for v in value[:20]] + ([{'omitted_items':len(value)-20}] if len(value)>20 else [])
    if isinstance(value,dict):return {str(k):shorten(v) for k,v in value.items()}
    return value

class Findings:
    def __init__(self):self.rows=[];self.by_tid=defaultdict(list)
    def add(self,tid,rule,loc,evidence,message,related=None):
        category,action,severity,impact,description,condition=RULES[rule]
        item={'trajectory_id':tid,'rule_id':rule,'category':category,'suggested_action':action,
              'severity':severity,'message':message,'source':loc,'related_sources':related or [],
              'evidence':shorten(evidence)}
        digest=hashlib.sha256(canonical(item).encode()).hexdigest()[:20]
        item['issue_id']='issue_'+digest
        # Identity includes message/evidence/source, allowing multiple findings at one field.
        self.rows.append(item);self.by_tid[tid].append(item)
        return item
    def emitter(self,tid):
        return lambda rule,loc,evidence,message,related=None:self.add(tid,rule,loc,evidence,message,related)


def record(loc,data):return {'source':loc,'data':data}

def load_tables(root, findings):
    tables={};hashes={};stats={};global_problems=[];damaged=[]
    for table,relative in TABLES.items():
        path=root/'sample'/relative;name='sample/'+relative
        rows=[];errors=0;physical=0;complete=True
        try:
            digest=hashlib.sha256()
            with path.open('rb') as f:
                for chunk in iter(lambda:f.read(1024*1024),b''):digest.update(chunk)
            hashes[name]=digest.hexdigest()
            with path.open('rb') as f:
                with io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(f,read_across_frames=True),encoding='utf-8',errors='strict') as text:
                    for line_no,line in enumerate(text,1):
                        physical=line_no;loc={'file':name,'line':line_no,'pointer':''}
                        try:
                            data=json.loads(line,parse_constant=lambda x:(_ for _ in ()).throw(ValueError('Nonfinite JSON: '+x)))
                            if not isinstance(data,dict):raise ValueError('JSON行必须是对象')
                            if table!='tool_grammar' and (not isinstance(data.get('trajectory_id'),str) or not data['trajectory_id']):
                                raise ValueError('trajectory_id必须为非空字符串')
                        except (ValueError,TypeError) as exc:
                            errors+=1
                            tid=f'__corrupt__:{table}:{line_no}'
                            findings.add(tid,'INPUT_CORRUPT',loc,{'error':str(exc),'raw_line_sha256':hashlib.sha256(line.encode()).hexdigest()},'损坏行被隔离；未跳过其状态')
                            damaged.append({'trajectory_id':tid,'sources':[loc],'corrupt':True})
                            # Unknown original ID: cannot know which valid trajectory lacks evidence.
                            global_problems.append(('INPUT_UNREADABLE',loc,{'error':'unattributable corrupt row','table':table},'损坏行无法归属；本批必要证据覆盖不完整'))
                            continue
                        if table=='sft_steps':
                            # Context is a shortened training view, not used for evidence checks.
                            # Keep only checked columns in RAM; the original file is untouched.
                            data={k:v for k,v in data.items() if k!='context'}
                        rows.append(record(loc,data))
        except (OSError,UnicodeError,zstandard.ZstdError) as exc:
            complete=False
            global_problems.append(('INPUT_UNREADABLE',{'file':name,'line':physical+1,'pointer':''},{'error':type(exc).__name__,'detail':str(exc)},'文件读取/解压失败，未读取部分不能视为通过'))
        tables[table]=rows
        stats[table]={'valid_rows':len(rows),'damaged_rows':errors,'physical_lines_read':physical,'read_complete':complete}
    return tables,hashes,stats,global_problems,damaged


def allocate_review(decisions, findings, config):
    n=sum(not r.get('corrupt') for r in decisions);budget=math.floor(n*config['review_fraction'])
    seed=str(config['seed'])
    tie=lambda tid:hashlib.sha256((seed+':'+tid).encode()).hexdigest()
    def rank(row):
        issues=findings.by_tid[row['trajectory_id']]
        impact=max((RULES[i['rule_id']][3] for i in issues if i['suggested_action']=='defer'),default=0)
        # Failure/unknown counts do not raise priority; ambiguity and recoverable evidence do.
        value=min(row.get('tool_calls',0),100)
        return impact,value
    audit_slots=min(budget,math.ceil(budget*config['audit_fraction_of_budget']))
    primary_slots=budget-audit_slots
    candidates=sorted((r for r in decisions if r['decision']=='defer'),key=lambda r:(-rank(r)[0],-rank(r)[1],tie(r['trajectory_id'])))
    selected=[(r,'uncertainty_priority') for r in candidates[:primary_slots]]
    selected_ids={r['trajectory_id'] for r,_ in selected}
    # Seeded audit across the entire remainder, including admitted and excluded records.
    remainder=sorted((r for r in decisions if r['trajectory_id'] not in selected_ids),key=lambda r:tie(r['trajectory_id']))
    selected.extend((r,'seeded_audit') for r in remainder[:audit_slots])
    selected_ids={r['trajectory_id'] for r,_ in selected}
    # Use otherwise unused priority capacity for more uncertainty, then audit.
    if len(selected)<budget:
        fill=[r for r in candidates if r['trajectory_id'] not in selected_ids]
        selected.extend((r,'uncertainty_priority') for r in fill[:budget-len(selected)])
    selected_ids={r['trajectory_id'] for r,_ in selected}
    if len(selected)<budget:
        extra=sorted((r for r in decisions if r['trajectory_id'] not in selected_ids),key=lambda r:tie(r['trajectory_id']))
        selected.extend((r,'seeded_audit') for r in extra[:budget-len(selected)])
    selected_ids={r['trajectory_id'] for r,_ in selected}
    queue=[]
    for idx,(row,reason) in enumerate(selected,1):
        impact,value=rank(row)
        queue.append({'rank':idx,'trajectory_id':row['trajectory_id'],'current_decision':row['decision'],
          'selection_reason':reason,'priority':{'decision_change_impact':impact,'tool_call_value_capped_100':value,'seeded_tiebreak':tie(row['trajectory_id'])},
          'issue_ids':row['issue_ids'],'sources':row['sources'],
          'review_question':'核验关键证据是否足以改变当前准入；记录不确定性及改判条件' if reason=='uncertainty_priority' else '独立检查规则漏检及误报，不预设当前建议正确'})
    for r in decisions:
        r['review_status']='queued' if r['trajectory_id'] in selected_ids else ('deferred_over_budget' if r['decision']=='defer' else 'not_queued')
    return queue,{'denominator':n,'fraction':config['review_fraction'],'maximum':budget,'queued':len(queue),
                  'uncertainty_candidates':len(candidates),'deferred_not_queued':sum(r['review_status']=='deferred_over_budget' for r in decisions),
                  'audit_slots_requested':audit_slots,'seed':config['seed']}


def run(data_dir, output, config=None):
    config={**DEFAULTS,**(config or {})}
    if config['policy'] not in ('balanced','conservative'):raise ValueError('Unknown policy')
    if not 0<=config['review_fraction']<=0.1:raise ValueError('review_fraction must be between 0 and 0.1')
    if not 0<=config['audit_fraction_of_budget']<=1:raise ValueError('audit_fraction_of_budget must be between 0 and 1')
    root=Path(data_dir).resolve();out=Path(output).resolve()
    if out==root or root in out.parents or out in root.parents:raise ValueError('Output and raw data directories must not overlap')
    out.mkdir(parents=True,exist_ok=True)
    start=time.perf_counter();started=datetime.now(timezone.utc).isoformat()
    findings=Findings()
    tables,hashes,stats,global_problems,damaged=load_tables(root,findings)
    try:
        checksum_path=root/'sample/CHECKSUMS.json'
        checks=json.loads(checksum_path.read_text())
        expected_paths=set(TABLES.values())
        if not isinstance(checks,dict) or set(checks)!=expected_paths:raise ValueError('校验清单必须覆盖全部六个数据文件')
        for relative,expected in checks.items():
            actual=hashes.get('sample/'+relative)
            if actual!=expected:global_problems.append(('INTEGRITY_UNVERIFIED',{'file':'sample/'+relative,'line':None,'pointer':''},{'expected':expected,'actual':actual},'数据哈希未通过发布校验'))
    except (OSError,ValueError,TypeError) as exc:
        global_problems.append(('INTEGRITY_UNVERIFIED',{'file':'sample/CHECKSUMS.json','line':None,'pointer':''},{'error':str(exc)},'无法确认发布校验'))
    revision=None
    manifest=root.parent/'manifest.json'
    try:revision=json.loads(manifest.read_text()).get('revision')
    except (OSError,ValueError,AttributeError):pass
    if config.get('expected_revision') and revision!=config['expected_revision']:
        global_problems.append(('VERSION_MISMATCH',{'file':'../manifest.json','line':None,'pointer':'/revision'},{'expected':config['expected_revision'],'recorded':revision},'下载记录版本与配置不符'))
    index={};union=set()
    for table in ('trajectories','messages','labels'):
        by_id=defaultdict(list)
        for row in tables[table]:by_id[row['data']['trajectory_id']].append(row)
        index[table]=by_id;union.update(by_id)
    if config.get('expected_trajectories') is not None and len(index['trajectories'])!=config['expected_trajectories']:
        global_problems.append(('VERSION_MISMATCH',{'file':'sample/'+TABLES['trajectories'],'line':None,'pointer':''},{'expected':config['expected_trajectories'],'observed':len(index['trajectories'])},'轨迹范围与配置不同'))
    views={}
    for name in ('sft_steps','error_recovery'):
        by_id=defaultdict(list)
        for row in tables[name]:by_id[row['data']['trajectory_id']].append(row)
        views[name]=by_id
        union.update(by_id)
    # Aggregate tool table is checked against raw calls; conflicts affect batch provenance.
    try:
        expected_tools=defaultdict(lambda:{'calls':0,'errors':0,'sessions':set()})
        for row in tables['trajectories']:
            for s in row['data']['steps']:
                if s.get('type') in CALL_TYPES:
                    v=expected_tools[s['tool']];v['calls']+=1;v['errors']+=s.get('ok') is False;v['sessions'].add(row['data']['trajectory_id'])
        seen=set()
        for row in tables['tool_grammar']:
            g=row['data'];name=g['tool'];v=expected_tools.get(name)
            if name in seen or not v:raise ValueError('tool_grammar工具重复或不存在: '+str(name))
            seen.add(name)
            if any(g.get(k)!= (len(v[k]) if k=='sessions' else v[k]) for k in ('calls','errors','sessions')) or abs(g['error_rate']-v['errors']/v['calls'])>0.000051:
                global_problems.append(('CROSS_COUNT',source(row),{'tool':name,'reported':g,'derived':{k:len(x) if isinstance(x,set) else x for k,x in v.items()}},'工具汇总与公开样本统计不同'))
        if seen!=set(expected_tools):raise ValueError('tool_grammar覆盖工具集合不完整')
    except Exception as exc:
        global_problems.append(('CHECK_FAILED',{'file':'sample/'+TABLES['tool_grammar'],'line':None,'pointer':''},{'error':type(exc).__name__,'detail':str(exc)},'工具汇总核验未完成'))
    decisions=[];duplicates={}
    for tid in sorted(union):
        emit=findings.emitter(tid);main={};sources=[];check_status={}
        for table in index:
            entries=index[table].get(tid,[])
            if not entries:
                emit('MISSING_TABLE_ROW',{'file':'sample/'+TABLES[table],'line':None,'pointer':''},{'trajectory_id':tid},'主表缺少该轨迹')
            if len(entries)>1:
                emit('DUPLICATE_ID',source(entries[0]),{'count':len(entries)},'主表轨迹ID重复',[source(x) for x in entries[1:]])
            main[table]=entries[0] if entries else None
            sources.extend(source(x) for x in entries)
        for rule,loc,evidence,message in global_problems:emit(rule,loc,evidence,message)
        def guarded(name,func,loc):
            try:
                result=func();check_status[name]='complete';return result
            except Exception as exc:
                check_status[name]='failed'
                emit('CHECK_FAILED',loc,{'check':name,'exception':type(exc).__name__,'detail':str(exc)[:300]},'检查失败，未完成部分不视为通过')
                return None
        t=main['trajectories'];m=main['messages'];l=main['labels']
        parsed=guarded('trajectory',lambda:check_trajectory(t,emit),source(t)) if t else None
        ms=guarded('messages',lambda:check_messages(m,emit),source(m)) if m else None
        if t and parsed is not None:
            guarded('cross_table',lambda:check_cross(t,m,l,views['sft_steps'].get(tid,[]),emit,parsed,ms),source(t))
            guarded('recovery',lambda:check_recovery(t,views['error_recovery'].get(tid,[]),emit,parsed),source(t))
            parent=t['data'].get('parent_trajectory_id')
            if parent and parent not in index['trajectories']:emit('PARENT_OUTSIDE',source(t,'/parent_trajectory_id'),{'parent':parent},'样本外父轨迹不作为引用损坏')
        if m:
            r=m['data']
            if r.get('is_eval') is True:emit('EVAL_RISK',source(m,'/is_eval'),{'is_eval':True},'未证明与目标评测集重叠')
            group_size=r.get('template_group_size')
            if isinstance(group_size,int) and not isinstance(group_size,bool) and group_size>1:
                emit('TEMPLATE_RISK',source(m,'/template_group_size'),{'size_in_full_dataset':group_size,'group':r.get('template_group')},'共享开头不是全文重复证据')
            def duplicate_check():
                tools=r.get('tools');tools=json.loads(tools) if isinstance(tools,str) else tools
                digest=hashlib.sha256(canonical({'messages':r.get('messages'),'tools':tools}).encode()).hexdigest()
                if digest in duplicates:emit('EXACT_DUPLICATE',source(m,'/messages'),{'retained_trajectory_id':duplicates[digest]['data']['trajectory_id'],'content_sha256':digest},'完全相同训练内容按轨迹ID保留一个',[source(duplicates[digest])])
                else:duplicates[digest]=m
            guarded('exact_duplicate',duplicate_check,source(m))
            if config['policy']=='conservative':
                if r.get('is_eval') is True:emit('STRICT_EVAL',source(m,'/is_eval'),{},'保守策略隔离评测来源')
                if any(i['rule_id']=='OPEN_BOUNDARY' for i in findings.by_tid[tid]):emit('STRICT_BOUNDARY',source(m,'/messages'),{},'保守策略要求完整起止')
        decisions.append({'trajectory_id':tid,'sources':sources,'checks':check_status,'tool_calls':ms['calls'] if ms else 0,
                          'message_characters':ms['chars'] if ms else None,'source_status':'present' if all(main.values()) else 'missing_main_row'})
    decisions.extend(damaged)
    for row in decisions:
        issues=findings.by_tid[row['trajectory_id']]
        actions={i['suggested_action'] for i in issues}
        row['decision']='exclude' if 'exclude' in actions else ('defer' if 'defer' in actions else 'admit')
        row['issue_ids']=[i['issue_id'] for i in issues]
        row['reason_codes']=sorted({i['rule_id'] for i in issues if i['suggested_action'] in ('exclude','defer')}) or ['CHECKS_PASSED_WITH_RECORDED_RISKS' if issues else 'CHECKS_PASSED']
        row['risk_codes']=sorted({i['rule_id'] for i in issues if i['suggested_action']=='allow'})
        row['check_status']='failed' if any(i['category']=='check_failure' for i in issues) else ('damaged_input' if row.get('corrupt') else 'completed')
    queue,budget=allocate_review(decisions,findings,config)
    rule_counts=Counter(i['rule_id'] for i in findings.rows)
    rule_trajectories=Counter()
    for row in decisions:rule_trajectories.update({i['rule_id'] for i in findings.by_tid[row['trajectory_id']]})
    summary={'dataset_revision_recorded':revision,'policy':config['policy'],'trajectory_units':len(decisions),
      'valid_id_units':len(union),'unattributable_damaged_units':len(damaged),
      'decisions':dict(Counter(r['decision'] for r in decisions)),
      'check_status':dict(Counter(r['check_status'] for r in decisions)),
      'issues_by_rule':dict(sorted(rule_counts.items())),
      'trajectories_by_rule':dict(sorted(rule_trajectories.items())),
      'issues_by_category':dict(Counter(i['category'] for i in findings.rows)),
      'blocking_trajectories_by_rule':dict(sorted((k,v) for k,v in rule_trajectories.items() if RULES[k][1] in ('defer','exclude'))),
      'nonblocking_risk_trajectories_by_rule':dict(sorted((k,v) for k,v in rule_trajectories.items() if RULES[k][1]=='allow')),
      'table_status':stats,'review_budget':budget,'uncovered_and_unresolved_risks':LIMITATIONS,
      'batch_problems':[{'rule_id':r,'source':l,'evidence':shorten(e),'message':m} for r,l,e,m in global_problems],
      'review_order':'先按暂缓问题的改判影响降序，再按工具调用数（上限100）降序，再用固定种子哈希打破平局；预算20%留作全批次随机审计，其余优先疑难。队列排名先疑难再审计。',
      'source_convention':'file相对--data目录；line为解压后JSONL的1起始行号；pointer为JSON Pointer，/tools下路径需先解析字符串。证据为截取片段，完整内容在原始位置。',
      'decision_precedence':'明确排除 > 暂缓 > 入选；排除并不表示其他检查全部通过；风险项不自动阻断balanced入选。'}
    def write_json(name,value):
        temp=out/(name+'.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n');temp.replace(out/name)
    def write_jsonl(name,values):
        temp=out/(name+'.tmp')
        with temp.open('w') as f:
            for value in values:f.write(canonical(value)+'\n')
        temp.replace(out/name)
    write_jsonl('issues.jsonl',findings.rows)
    write_jsonl('decisions.jsonl',decisions)
    write_jsonl('review_queue.jsonl',queue)
    write_json('summary.json',summary)
    write_json('rules.json',{r:{'category':v[0],'action':v[1],'severity':v[2],'review_impact':v[3],'description':v[4],'applicability':v[5]} for r,v in RULES.items()})
    write_json('config.json',config)
    source_files=sorted(Path(__file__).parent.glob('*.py'))
    code_hash=hashlib.sha256(''.join(p.name+':'+hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files).encode()).hexdigest()
    output_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir()) if p.name in ('issues.jsonl','decisions.jsonl','review_queue.jsonl','summary.json','rules.json','config.json')}
    try:
        import resource
        peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_bytes=int(peak if sys.platform=='darwin' else peak*1024)
    except ImportError:
        peak_bytes=None
    write_json('run_manifest.json',{'pipeline_version':__version__,'code_sha256':code_hash,'started_at_utc':started,
       'elapsed_seconds':round(time.perf_counter()-start,3),'peak_process_memory_bytes':peak_bytes,'python':sys.version,'platform':platform.platform(),
       'dependencies':{x:version(x) for x in ('zstandard','jsonschema')},'command':sys.argv,
       'data_root':str(root),'data_sha256':hashes,'output_sha256':output_hashes,'config':config,
       'note':'时间/平台/命令可能变化；事实文件及队列在相同输入配置代码下应一致。未执行任何数据中的指令。'})
    # Markdown report is generated from machine-readable results, not hand edited counts.
    lines=['# 质检运行汇总','',f"轨迹单位：{len(decisions)}；策略：{config['policy']}。",'',
           '| 处理建议 | 数量 |','|---|---:|']
    lines.extend(f'| {k} | {summary["decisions"].get(k,0)} |' for k in ('admit','exclude','defer'))
    lines.extend(['',f'人工队列 {len(queue)}/{budget["maximum"]}；未入队暂缓 {budget["deferred_not_queued"]}。','',
                  '| 规则 | 命中轨迹 | 问题条数 |','|---|---:|---:|'])
    lines.extend(f'| {k} | {rule_trajectories[k]} | {rule_counts[k]} |' for k in sorted(rule_counts,key=lambda k:(-rule_trajectories[k],k)))
    lines.extend(['','未覆盖范围及未解决风险：','']+['- '+x for x in LIMITATIONS])
    (out/'summary.md').write_text('\n'.join(lines)+'\n')
    return summary
