"""Rules emit observations; admission and review allocation live in pipeline.py."""
import json
import re
from collections import Counter
from jsonschema import Draft202012Validator

CALL_TYPES = {'tool_call', 'retrieval', 'subagent'}
# category, disposition, severity, review impact, explanation, applicability
RULES = {
 'INPUT_CORRUPT': ('data_defect','exclude','critical',0,'原始行不是有效JSON对象或缺少有效轨迹ID','所有含trajectory_id的输入表'),
 'INPUT_UNREADABLE': ('check_failure','defer','critical',100,'文件缺失、损坏或解压失败，检查范围不完整','所有输入文件'),
 'INTEGRITY_UNVERIFIED': ('insufficient_evidence','defer','high',90,'文件哈希与发布校验不符或校验清单不可信','全批次'),
 'VERSION_MISMATCH': ('insufficient_evidence','defer','high',80,'记录版本或轨迹范围与配置不同','全批次'),
 'CHECK_FAILED': ('check_failure','defer','critical',100,'检查器异常或记录形状阻止必要检查','所有检查组'),
 'DUPLICATE_ID': ('data_defect','exclude','critical',0,'主表轨迹ID重复导致监督关联歧义','trajectories/messages/labels主键'),
 'MISSING_TABLE_ROW': ('insufficient_evidence','defer','high',90,'主表缺少对应轨迹','三张主表ID并集'),
 'STRUCTURE_INVALID': ('data_defect','exclude','critical',0,'训练消息或原始步骤的必要结构不合法','轨迹及训练消息'),
 'CALL_ID_DUPLICATE': ('data_defect','exclude','critical',0,'调用ID在同一轨迹中重复','messages.tool_calls'),
 'RETURN_ORPHAN': ('data_defect','exclude','critical',0,'工具返回引用不存在或尚未出现的调用','messages中的tool消息'),
 'RETURN_DUPLICATE': ('data_defect','exclude','high',0,'同一调用收到多个返回','messages中的tool消息'),
 'CALL_UNRETURNED': ('insufficient_evidence','defer','high',85,'调用没有对应返回，无法还原动作反馈','messages中的调用'),
 'ARGS_INVALID': ('data_defect','exclude','high',0,'训练参数不是有效JSON对象','messages中的function.arguments'),
 'TOOL_UNDECLARED': ('insufficient_evidence','defer','high',90,'调用函数在会话工具定义中不存在','messages中的调用'),
 'SCHEMA_INVALID': ('insufficient_evidence','defer','high',85,'工具声明无法作为受支持的JSON Schema验证','messages.tools'),
 'SCHEMA_MISMATCH': ('insufficient_evidence','defer','high',95,'参数不符合发布schema；schema非原始工具真值，需核验','每个可解析的训练调用'),
 'STEP_INDEX': ('data_defect','exclude','high',0,'步骤i与0起始位置不一致','trajectories.steps'),
 'CROSS_COUNT': ('insufficient_evidence','defer','high',90,'原始步骤、主表统计或派生视图计数不一致','可关联的跨表记录'),
 'CROSS_VALUE': ('insufficient_evidence','defer','high',90,'同一原始调用在跨表表示中值不一致','sft_steps与原始步骤；同名主表元数据'),
 'VIEW_REFERENCE': ('insufficient_evidence','defer','high',90,'派生视图无法关联唯一有效调用步骤','sft_steps/trajectory_id+i'),
 'VIEW_TRUNCATED': ('risk','allow','info',10,'派生视图合法截短，完整证据仍在原始表','sft_steps结果为原始结果前缀'),
 'RECOVERY_UNCERTAIN': ('insufficient_evidence','allow','medium',40,'失败视图无法唯一定位原始失败；不使用其标签作监督','error_recovery候选匹配'),
 'RECOVERY_MISMATCH': ('insufficient_evidence','defer','high',85,'唯一定位后窗口恢复事实或类型与原始步骤不一致','error_recovery唯一匹配'),
 'RECOVERY_HEURISTIC': ('risk','allow','info',10,'窗口成功不证明原故障修复或任务成功','原始轨迹至少一次ok=false'),
 'STATUS_UNKNOWN': ('risk','allow','medium',20,'调用状态未记录；不计失败','原始调用ok=null或缺失'),
 'PARAMS_INCOMPLETE': ('insufficient_evidence','defer','high',95,'编辑/写入动作缺少实际改动内容或仅记录参数摘要','原始及训练动作参数'),
 'WITHHELD': ('insufficient_evidence','defer','high',75,'训练消息含移除文本，缺失内容影响未被验证','训练消息文本/推理/参数'),
 'RECORDED_TRUNCATION': ('insufficient_evidence','defer','high',70,'结果含截断提示或未解释的标记；是否缺失关键反馈待核验','原始调用结果中非明确代码字面量的标记'),
 'TRUNCATION_LITERAL': ('risk','allow','info',0,'明确源码语境内的带引号常量，不单凭该常量推断观测缺失','原始调用结果中的截断标记代码字面量'),
 'OPEN_BOUNDARY': ('risk','allow','medium',30,'开头无真实用户需求或末尾无文本回答，保留中间合法行为','messages会话边界'),
 'PUBLISHER_FLAG': ('risk','allow','info',10,'发布方已有标记，仅作线索而非真值','非空ft_issues'),
 'EVAL_RISK': ('selection_risk','allow','medium',30,'预设评测来源，尚未核对目标评测重叠','is_eval=true'),
 'TEMPLATE_RISK': ('selection_risk','allow','info',10,'完整数据集中共享开头；不等于全文重复','template_group_size>1'),
 'EXACT_DUPLICATE': ('selection_mismatch','exclude','medium',0,'训练内容与另一轨迹完全相同，按ID保留一个','规范化messages+tools哈希'),
 'NO_TOOL_TARGET': ('selection_mismatch','exclude','medium',0,'没有训练工具调用，不符合本轮工具行为目标','messages中零工具调用'),
 'PARENT_OUTSIDE': ('risk','allow','info',0,'父轨迹不在公开样本内，不直接判数据损坏','非空parent_trajectory_id'),
 'STRICT_BOUNDARY': ('selection_mismatch','defer','medium',50,'保守策略要求用户起点和文本结尾完整','conservative策略且OPEN_BOUNDARY'),
 'STRICT_EVAL': ('selection_mismatch','defer','medium',50,'保守策略隔离预设评测来源，待污染核验','conservative策略且is_eval'),
}

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def json_equivalent(a, b):
    # Canonical JSON preserves bool vs int, array order and object-vs-string distinctions.
    try:
        av = json.loads(a) if isinstance(a, str) else a
        bv = json.loads(b) if isinstance(b, str) else b
        return canonical(av) == canonical(bv)
    except (ValueError, TypeError):
        return type(a) is type(b) and a == b

def source(record, pointer=''):
    return {**record['source'], 'pointer': pointer}

def check_messages(record, emit):
    r = record['data']
    msgs = r.get('messages')
    if not isinstance(msgs, list) or not msgs:
        emit('STRUCTURE_INVALID', source(record,'/messages'), {'observed':msgs}, 'messages必须是非空数组')
        return {'calls':0,'chars':0}
    tools = r.get('tools')
    try:
        tools = json.loads(tools) if isinstance(tools,str) else tools
        if not isinstance(tools,list): raise ValueError('tools必须是数组')
    except (ValueError,TypeError) as exc:
        emit('SCHEMA_INVALID',source(record,'/tools'),{'error':str(exc)},'无法解析工具声明')
        tools = []
    definitions = {}
    for j, tool in enumerate(tools):
        loc = source(record,f'/tools/{j}')
        if not isinstance(tool,dict) or not isinstance(tool.get('function'),dict):
            emit('SCHEMA_INVALID',loc,{'observed':tool},'工具声明缺少function对象');continue
        f = tool['function'];name=f.get('name')
        if not isinstance(name,str) or not name or name in definitions:
            emit('SCHEMA_INVALID',loc,{'name':name},'函数名缺失或声明重复');continue
        schema=f.get('parameters')
        # External references are never retrieved. Only local JSON Schema refs are supported.
        def external_refs(x):
            if isinstance(x,dict):
                return any(k=='$ref' and (not isinstance(v,str) or not v.startswith('#')) or external_refs(v) for k,v in x.items())
            return isinstance(x,list) and any(external_refs(v) for v in x)
        try:
            if not isinstance(schema,dict) or external_refs(schema): raise ValueError('schema非对象或含外部引用')
            Draft202012Validator.check_schema(schema)
            definitions[name] = (Draft202012Validator(schema),loc)
        except Exception as exc:
            emit('SCHEMA_INVALID',loc,{'error':type(exc).__name__,'detail':str(exc)[:250]},'工具schema无法验证')
    pending={};returned=set();calls=0;chars=0
    for j,m in enumerate(msgs):
        loc=source(record,f'/messages/{j}')
        if not isinstance(m,dict) or m.get('role') not in ('user','assistant','tool','system','developer'):
            emit('STRUCTURE_INVALID',loc,{'observed':m},'无效消息对象或角色');continue
        role=m['role'];content=m.get('content')
        if content is not None and not isinstance(content,str):
            emit('STRUCTURE_INVALID',loc,{'content_type':type(content).__name__},'本数据契约content必须是文本或null')
        chars += len(content) if isinstance(content,str) else 0
        reasoning=m.get('reasoning_content')
        if reasoning is not None and not isinstance(reasoning,str):
            emit('STRUCTURE_INVALID',loc,{'reasoning_type':type(reasoning).__name__},'reasoning_content必须是文本或null')
        payload=canonical(m)
        if '<WITHHELD>' in payload:
            emit('WITHHELD',loc,{'marker':'<WITHHELD>'},'训练消息存在移除文本；不推断被移除内容')
        tc=m.get('tool_calls') or []
        if not isinstance(tc,list):
            emit('STRUCTURE_INVALID',loc,{'tool_calls_type':type(tc).__name__},'tool_calls必须是数组');continue
        if tc and role!='assistant':
            emit('STRUCTURE_INVALID',loc,{'role':role},'工具调用必须来自assistant')
        if role=='tool':
            cid=m.get('tool_call_id')
            if not isinstance(cid,str) or cid not in pending:
                emit('RETURN_ORPHAN',loc,{'tool_call_id':cid},'返回未引用先前有效调用')
            elif cid in returned:
                emit('RETURN_DUPLICATE',loc,{'tool_call_id':cid},'同一调用重复返回',[pending[cid]])
            else: returned.add(cid)
        for k,c in enumerate(tc):
            calls+=1;cloc=source(record,f'/messages/{j}/tool_calls/{k}')
            if not isinstance(c,dict) or not isinstance(c.get('function'),dict):
                emit('STRUCTURE_INVALID',cloc,{'observed':c},'工具调用缺少function对象');continue
            cid=c.get('id');f=c['function'];name=f.get('name')
            if not isinstance(cid,str) or not cid or c.get('type')!='function' or not isinstance(name,str) or not name:
                emit('STRUCTURE_INVALID',cloc,{'id':cid,'type':c.get('type'),'name':name},'调用ID、类型或函数名无效')
            elif cid in pending:
                emit('CALL_ID_DUPLICATE',cloc,{'id':cid},'重复调用ID',[pending[cid]])
            else: pending[cid]=cloc
            try:
                args=f.get('arguments')
                if not isinstance(args,str): raise ValueError('磁盘训练参数应为JSON字符串')
                args=json.loads(args,parse_constant=lambda x:(_ for _ in ()).throw(ValueError('Nonfinite JSON: '+x)))
                if not isinstance(args,dict):raise ValueError('参数解析后不是对象')
            except (ValueError,TypeError) as exc:
                emit('ARGS_INVALID',cloc,{'arguments':f.get('arguments'),'error':str(exc)},'训练调用参数不可消费');continue
            if name not in definitions:
                emit('TOOL_UNDECLARED',cloc,{'function':name},'缺少可验证的工具定义');continue
            validator,dloc=definitions[name]
            errors=sorted(validator.iter_errors(args),key=lambda e:str(list(e.path)))
            if errors:
                emit('SCHEMA_MISMATCH',cloc,{'function':name,'arguments':args,'violations':[{'path':list(e.path),'validator':e.validator,'message':e.message} for e in errors[:5]],'schema_origin':'publisher_reconstructed'},'与发布schema不兼容；未证明原始调用无效',[dloc])
            if name in ('edit','write','apply_patch','multi_edit'):
                # Do not equate an unrecognised vendor shape with missing change content.
                # Only positively identifiable path-only/empty/summary-only shapes qualify.
                path_only={'path','file_path','filePath','relativeWorkspacePath'}
                keys=set(args)
                incomplete=not keys or keys<=path_only or keys<= {'_dirty_files'}
                inp=args.get('input')
                if isinstance(inp,str) and inp.startswith('**Chunk ') and 'Lines added:' in inp and 'lines removed:' in inp:
                    incomplete=True
                if keys=={'parameters'} and isinstance(args['parameters'],str):
                    try:
                        inner=json.loads(args['parameters'])
                        incomplete=isinstance(inner,dict) and set(inner)<={'path','file_path','filePath','relativeWorkspacePath'}
                    except ValueError:
                        pass
                if incomplete:
                    emit('PARAMS_INCOMPLETE',cloc,{'function':name,'keys':sorted(args)},'编辑/写入动作未记录改动内容')
    for cid,loc in pending.items():
        if cid not in returned:emit('CALL_UNRETURNED',loc,{'id':cid},'未找到工具返回')
    first=next((m for m in msgs if isinstance(m,dict) and m.get('role') not in ('system','developer') and not m.get('is_wrapper')),None)
    last=msgs[-1]
    start_ok=bool(first and first.get('role')=='user' and first.get('content'))
    end_ok=isinstance(last,dict) and last.get('role')=='assistant' and bool(last.get('content')) and not last.get('tool_calls')
    if not start_ok or not end_ok:
        emit('OPEN_BOUNDARY',source(record,'/messages'),{'has_user_start':start_ok,'has_text_answer_end':end_ok},'边界不完整不自动否定中间调用价值')
    if not calls:emit('NO_TOOL_TARGET',source(record,'/messages'),{'calls':0},'本轮目标要求至少一次工具调用')
    if r.get('ft_issues'):emit('PUBLISHER_FLAG',source(record,'/ft_issues'),{'flags':r['ft_issues']},'已有标记未作为自动排除真值')
    return {'calls':calls,'chars':chars}


_TRUNCATION_MARKER = re.compile(r'\[TRUNCATED\]|\.\.\.TRUNCATED')
_QUOTED_LITERAL = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`''')
_SOURCE_LINE = re.compile(r'^\s*(?:\d+(?:\t|→)|L\d+[-:])\s*')
_CODE_CONTEXT = re.compile(
    r'^\s*(?:(?:const|let|var|export|return|assert)\b|[A-Za-z_]\w*(?:\.\w+)*\s*(?:=|:=))'
    r'|\b(?:expect|assertEquals|assertEqual)\s*\(|\.to(?:Be|Equal|StrictEqual|Contain)\s*\(')
_STANDALONE_NOTICE = re.compile(r'\s*(?:\.\.\.\s*)?\[TRUNCATED\](?:\s*\.\.\.)?\s*|\s*\.\.\.TRUNCATED(?:\.\.\.)?\s*')


def truncation_contexts(result):
    """Locate markers; exempt only complete quoted literals in source-like context.

    This is a lexical heuristic, not a code parser or proof of data loss.
    Unrecognised contexts remain deferred, including bare/JSON-quoted markers.
    Offsets always refer to the original result, with no decoding/rewriting.
    """
    if not _TRUNCATION_MARKER.search(result):
        return []
    matches=[];offset=0
    for line_no,line in enumerate(result.splitlines(keepends=True),1):
        hits=list(_TRUNCATION_MARKER.finditer(line))
        if hits:
            code_context=bool(_SOURCE_LINE.match(line) or _CODE_CONTEXT.search(line))
            literals=list(_QUOTED_LITERAL.finditer(line)) if code_context else []
            standalone=bool(_STANDALONE_NOTICE.fullmatch(line))
            for hit in hits:
                literal=any(q.start()<hit.start() and hit.end()<q.end() for q in literals)
                context='code_literal' if literal else ('standalone_notice' if standalone else 'ambiguous_marker')
                matches.append({'marker':hit.group(),'offset':offset+hit.start(),
                    'result_line':line_no,'column':hit.start(),'context':context,
                    'excerpt':line[max(0,hit.start()-100):hit.end()+100].rstrip('\r\n')})
        offset+=len(line)
    return matches


def check_trajectory(record, emit):
    r=record['data'];steps=r.get('steps')
    if not isinstance(steps,list) or any(not isinstance(s,dict) for s in steps):
        emit('STRUCTURE_INVALID',source(record,'/steps'),{},'steps必须是对象数组');return None
    calls=[];failed=[];unknown=[]
    for j,s in enumerate(steps):
        loc=source(record,f'/steps/{j}')
        if type(s.get('i')) is not int or s['i']!=j:
            emit('STEP_INDEX',loc,{'expected':j,'actual':s.get('i')},'原始步骤编号与位置不符')
        if s.get('type') not in ('user','assistant',*CALL_TYPES):
            emit('STRUCTURE_INVALID',loc,{'type':s.get('type')},'未知原始步骤类型')
        if s.get('type') in CALL_TYPES:
            calls.append(s)
            if s.get('ok') is False:failed.append(j)
            elif s.get('ok') is None:unknown.append(j)
            elif s.get('ok') is not True:
                emit('STRUCTURE_INVALID',loc,{'ok':s.get('ok')},'状态只允许true/false/null')
            result=s.get('result')
            if isinstance(result,str):
                contexts=truncation_contexts(result)
                for rule,selected,message in (
                    ('RECORDED_TRUNCATION',[x for x in contexts if x['context']!='code_literal'],
                     '结果含截断提示或语境不明的标记；当前未验证是否缺失关键反馈'),
                    ('TRUNCATION_LITERAL',[x for x in contexts if x['context']=='code_literal'],
                     '标记位于明确源码语境的完整带引号常量内；不据此判定结果截断')):
                    if selected:
                        emit(rule,source(record,f'/steps/{j}/result'),
                             {'match_count':len(selected),'matches':selected[:10]},message)
    for field,actual in [('n_steps',len(steps)),('n_tool_calls',len(calls)),('n_errors',len(failed))]:
        if type(r.get(field)) is not int or r[field]!=actual:
            emit('CROSS_COUNT',source(record,'/'+field),{'reported':r.get(field),'derived':actual},'元数据与原始步骤统计不同',[source(record,'/steps')])
    if unknown:emit('STATUS_UNKNOWN',source(record,f'/steps/{unknown[0]}/ok'),{'count':len(unknown),'first_step':unknown[0]},'状态未记录，不计入失败')
    if failed:emit('RECOVERY_HEURISTIC',source(record,f'/steps/{failed[0]}'),{'failed_count':len(failed)},'保留失败及后续步骤；不把窗口成功当作任务成功')
    return {'steps':steps,'calls':calls,'failed':failed}


def result_difference(view,raw,i):
    evidence={'i':i,'view':view,'raw':raw}
    if isinstance(view,str) and isinstance(raw,str):
        offset=next((j for j,(a,b) in enumerate(zip(view,raw)) if a!=b),min(len(view),len(raw)))
        evidence={'i':i,'view_chars':len(view),'raw_chars':len(raw),'first_difference_offset':offset,
                  'view_near_difference':view[max(0,offset-100):offset+200],
                  'raw_near_difference':raw[max(0,offset-100):offset+200]}
    return evidence


def check_cross(t,m,l,views,emit,parsed,msg_stats):
    if parsed is None:return
    steps=parsed['steps'];r=t['data']
    if m and msg_stats and msg_stats['calls']!=len(parsed['calls']):
        emit('CROSS_COUNT',source(m,'/messages'),{'training_calls':msg_stats['calls'],'raw_calls':len(parsed['calls'])},'训练调用数与原始调用数不同',[source(t,'/steps')])
    for other in (m,l):
        if not other:continue
        for field in ('n_steps','is_eval','task_type','session_kind','parent_trajectory_id','template_group','template_group_size'):
            if field in other['data'] and other['data'][field]!=r.get(field):
                emit('CROSS_VALUE',source(other,'/'+field),{'other':other['data'][field],'trajectory':r.get(field)},'主表共享字段冲突',[source(t,'/'+field)])
    if l:
        metrics=l['data'].get('metrics')
        if not isinstance(metrics,dict):raise ValueError('labels.metrics必须是对象')
        expected={'span_count':len(steps),'tool_call_count':len(parsed['calls']),'error_count':len(parsed['failed']),
                  'llm_span_count':sum(s.get('type')=='assistant' for s in steps),
                  'user_turn_count':sum(s.get('type')=='user' for s in steps)}
        for k,v in expected.items():
            if type(metrics.get(k)) is not int or metrics[k]!=v:
                emit('CROSS_COUNT',source(l,'/metrics/'+k),{'label':metrics.get(k),'derived':v},'标注统计与原始步骤不同',[source(t,'/steps')])
    counts=Counter()
    for v in views:
        x=v['data'];i=x.get('i');loc=source(v)
        if type(i) is not int or not 0<=i<len(steps) or steps[i].get('type') not in CALL_TYPES:
            emit('VIEW_REFERENCE',loc,{'i':i},'调用视图未引用有效工具步骤');continue
        counts[i]+=1;s=steps[i]
        for field in ('tool','tool_raw','ok','args'):
            equal=json_equivalent(x.get(field),s.get(field)) if field=='args' else type(x.get(field)) is type(s.get(field)) and x.get(field)==s.get(field)
            if not equal:
                emit('CROSS_VALUE',source(v,'/'+field),{'view':x.get(field),'raw':s.get(field),'i':i},'调用视图与原始步骤字段不同',[source(t,f'/steps/{i}/{field}')])
        a=x.get('result');b=s.get('result')
        if a!=b:
            if isinstance(a,str) and isinstance(b,str) and b.startswith(a):
                emit('VIEW_TRUNCATED',source(v,'/result'),{'view_chars':len(a),'raw_chars':len(b),'i':i},'视图只保留原始结果前缀',[source(t,f'/steps/{i}/result')])
            else:
                emit('CROSS_VALUE',source(v,'/result'),result_difference(a,b,i),'结果不相同且不满足合法前缀关系；可能受脱敏转换影响',[source(t,f'/steps/{i}/result')])
    expected={s['i'] for s in parsed['calls']}
    if set(counts)!=expected or any(n!=1 for n in counts.values()):
        emit('CROSS_COUNT',source(t,'/steps'),{'missing_steps':sorted(expected-set(counts)),'duplicate_steps':[i for i,n in counts.items() if n!=1]},'每个原始调用应有且仅有一行sft_steps')


def check_recovery(t,rows,emit,parsed):
    if parsed is None:return
    steps=parsed['steps'];matched=Counter()
    for row in rows:
        r=row['data'];candidates=[]
        for i in parsed['failed']:
            s=steps[i]
            if s.get('tool')!=r.get('failed_tool') or not json_equivalent(s.get('args'),r.get('args')):continue
            a=r.get('error');b=s.get('result')
            if a==b or isinstance(a,str) and isinstance(b,str) and b.startswith(a):candidates.append(i)
        if len(candidates)!=1:
            emit('RECOVERY_UNCERTAIN',source(row),{'candidate_steps':candidates,'failed_tool':r.get('failed_tool')},'失败视图关联不唯一或无匹配，禁用其成功含义');continue
        i=candidates[0];matched[i]+=1
        success=next((s for s in steps[i+1:i+5] if s.get('type') in ('tool_call','retrieval') and s.get('ok') is True),None)
        kind='none'
        if success:
            kind='other_tool' if success.get('tool')!=steps[i].get('tool') else ('same_call_retried' if json_equivalent(success.get('args'),steps[i].get('args')) else 'same_tool_new_args')
        if type(r.get('recovered')) is not bool or r['recovered']!=bool(success) or r.get('recovery_kind')!=kind:
            emit('RECOVERY_MISMATCH',source(row),{'reported_recovered':r.get('recovered'),'derived_recovered':bool(success),'reported_kind':r.get('recovery_kind'),'derived_kind':kind,'failed_step':i},'恢复标记与后四步原始事实不同',[source(t,f'/steps/{i}')])
    if len(rows)!=len(parsed['failed']):
        emit('CROSS_COUNT',source(t,'/steps'),{'failed_calls':len(parsed['failed']),'recovery_rows':len(rows)},'失败调用数与失败视图行数不同')
    if any(n>1 for n in matched.values()):
        emit('CROSS_COUNT',source(t,'/steps'),{'duplicate_failure_views':[i for i,n in matched.items() if n>1]},'唯一匹配的失败步骤存在重复视图')
