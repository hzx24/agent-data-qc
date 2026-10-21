"""Synthetic fixtures only: these cases are not the real-data sample review."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
import zstandard
from agent_qc.rules import check_messages, check_trajectory, check_cross, check_recovery, json_equivalent
from agent_qc.pipeline import Findings, record, run, TABLES, allocate_review


def rec(data,file='fixture.jsonl',line=1):
    return record({'file':file,'line':line,'pointer':''},data)


def training(args=None):
    args={'command':'echo ok'} if args is None else args
    return {'trajectory_id':'t1','n_steps':4,'is_eval':False,'tools':json.dumps([
      {'type':'function','function':{'name':'bash','parameters':{'type':'object','properties':{'command':{'type':'string'}},'required':['command']}}}]),
      'messages':[{'role':'user','content':'run a check'},
       {'role':'assistant','content':None,'tool_calls':[{'id':'c0','type':'function','function':{'name':'bash','arguments':json.dumps(args)}}]},
       {'role':'tool','tool_call_id':'c0','content':'ok'},
       {'role':'assistant','content':'Done'}], 'ft_issues':[]}


def raw():
    return {'trajectory_id':'t1','n_steps':4,'n_tool_calls':1,'n_errors':0,'is_eval':False,
     'steps':[{'i':0,'type':'user','text':'run a check'},
      {'i':1,'type':'assistant','text':'Checking'},
      {'i':2,'type':'tool_call','tool':'bash','tool_raw':'Bash','args':'{"command":"echo ok"}','result':'ok','ok':True},
      {'i':3,'type':'assistant','text':'Done'}]}


def fixture(root, n=1):
    values={k:[] for k in TABLES}
    for i in range(n):
        tid='t'+str(i+1);t=raw();m=training()
        t['trajectory_id']=m['trajectory_id']=tid
        # Distinct user requests prevent duplicate removal.
        m['messages'][0]['content']+=' '+tid
        values['trajectories'].append(t);values['messages'].append(m)
        values['labels'].append({'trajectory_id':tid,'n_steps':4,'is_eval':False,'metrics':{'span_count':4,'tool_call_count':1,'error_count':0,'llm_span_count':2,'user_turn_count':1}})
        values['sft_steps'].append({'trajectory_id':tid,'i':2,**{k:t['steps'][2][k] for k in ('tool','tool_raw','args','result','ok')}})
    values['tool_grammar']=[{'tool':'bash','calls':n,'sessions':n,'errors':0,'error_rate':0.0}]
    save_fixture(root,values)
    return values


def save_fixture(root, values):
    sums={}
    for name,relative in TABLES.items():
        path=root/'sample'/relative;path.parent.mkdir(parents=True,exist_ok=True)
        b=''.join(json.dumps(r)+'\n' for r in values[name]).encode()
        path.write_bytes(zstandard.ZstdCompressor().compress(b))
        sums[relative]=hashlib.sha256(path.read_bytes()).hexdigest()
    (root/'sample/CHECKSUMS.json').write_text(json.dumps(sums))


class RulesTest(unittest.TestCase):
    def scan(self,m):
        f=Findings();stats=check_messages(rec(m),f.emitter('t1'));return f,stats
    def codes(self,f):return {r['rule_id'] for r in f.rows}
    def test_valid_tool_only_assistant(self):
        f,s=self.scan(training());self.assertEqual(f.rows,[]);self.assertEqual(s['calls'],1)
    def test_orphan_return(self):
        m=training();m['messages'][2]['tool_call_id']='missing'
        f,_=self.scan(m);self.assertIn('RETURN_ORPHAN',self.codes(f));self.assertIn('CALL_UNRETURNED',self.codes(f))
    def test_duplicate_call_and_return(self):
        m=training();m['messages'].insert(3,copy.deepcopy(m['messages'][1]));m['messages'].insert(4,copy.deepcopy(m['messages'][2]))
        f,_=self.scan(m);self.assertTrue({'CALL_ID_DUPLICATE','RETURN_DUPLICATE'}<=self.codes(f))
    def test_nonadjacent_parallel_returns_legal(self):
        m=training();call=copy.deepcopy(m['messages'][1]['tool_calls'][0]);call['id']='c1'
        m['messages'][1]['tool_calls'].append(call)
        m['messages'].insert(2,{'role':'assistant','content':'Waiting'})
        m['messages'].insert(3,{'role':'tool','tool_call_id':'c1','content':'ok'})
        f,_=self.scan(m);self.assertFalse(self.codes(f)&{'RETURN_ORPHAN','CALL_UNRETURNED','CALL_ID_DUPLICATE'})
    def test_invalid_arguments_not_schema_ambiguity(self):
        m=training();m['messages'][1]['tool_calls'][0]['function']['arguments']='{broken'
        f,_=self.scan(m);self.assertIn('ARGS_INVALID',self.codes(f))
        self.assertEqual(next(x for x in f.rows if x['rule_id']=='ARGS_INVALID')['suggested_action'],'exclude')
    def test_nonfinite_json_is_invalid(self):
        m=training();m['messages'][1]['tool_calls'][0]['function']['arguments']='{"command":NaN}'
        f,_=self.scan(m);self.assertIn('ARGS_INVALID',self.codes(f))
    def test_schema_mismatch_is_uncertainty(self):
        f,_=self.scan(training({'vendor_command':'echo ok'}))
        self.assertEqual(next(x for x in f.rows if x['rule_id']=='SCHEMA_MISMATCH')['suggested_action'],'defer')
    def test_unknown_tool_and_external_ref(self):
        m=training();tools=json.loads(m['tools']);tools[0]['function']['parameters']={'$ref':'https://example.invalid/schema'};m['tools']=json.dumps(tools)
        f,_=self.scan(m);self.assertIn('SCHEMA_INVALID',self.codes(f));self.assertIn('TOOL_UNDECLARED',self.codes(f))
    def test_schema_empty_string_is_not_omission(self):
        f,_=self.scan(training({'command':''}));self.assertNotIn('SCHEMA_MISMATCH',self.codes(f))
    def test_anonymous_placeholder_not_withheld(self):
        f,_=self.scan(training({'command':'echo <USER_1>'}));self.assertNotIn('WITHHELD',self.codes(f))
    def test_withheld_is_explicit_uncertainty(self):
        f,_=self.scan(training({'command':'<WITHHELD>'}));self.assertIn('WITHHELD',self.codes(f))
    def test_open_boundary_risk_only(self):
        m=training();m['messages'].pop()
        f,_=self.scan(m);self.assertIn('OPEN_BOUNDARY',self.codes(f));self.assertTrue(all(x['suggested_action']=='allow' for x in f.rows))
    def test_unknown_status_not_failure(self):
        t=raw();t['steps'][2]['ok']=None
        f=Findings();p=check_trajectory(rec(t),f.emitter('t1'))
        self.assertEqual(p['failed'],[]);self.assertIn('STATUS_UNKNOWN',self.codes(f));self.assertNotIn('CROSS_COUNT',self.codes(f))
    def test_failed_then_success_not_task_success(self):
        t=raw();t['steps'][2]['ok']=False;t['n_errors']=1
        t['steps'].append({'i':4,'type':'tool_call','tool':'read','args':'{}','ok':True,'result':'different task'})
        t['n_steps']=5;t['n_tool_calls']=2
        f=Findings();tr=rec(t);p=check_trajectory(tr,f.emitter('t1'))
        view=rec({'failed_tool':'bash','args':'{"command":"echo ok"}','error':'ok','recovered':True,'recovery_kind':'other_tool'})
        check_recovery(tr,[view],f.emitter('t1'),p)
        self.assertIn('RECOVERY_HEURISTIC',self.codes(f));self.assertNotIn('RECOVERY_MISMATCH',self.codes(f))
    def test_recovery_flag_disagrees_with_window(self):
        t=raw();t['steps'][2]['ok']=False;t['n_errors']=1
        f=Findings();tr=rec(t);p=check_trajectory(tr,f.emitter('t1'))
        check_recovery(tr,[rec({'failed_tool':'bash','args':'{"command":"echo ok"}','error':'ok','recovered':True,'recovery_kind':'other_tool'})],f.emitter('t1'),p)
        self.assertIn('RECOVERY_MISMATCH',self.codes(f))
    def test_ambiguous_recovery_not_arbitrary_first_match(self):
        t=raw();s=t['steps'][2];s['ok']=False;t['steps'].append({**s,'i':4});t.update(n_steps=5,n_tool_calls=2,n_errors=2)
        f=Findings();tr=rec(t);p=check_trajectory(tr,f.emitter('t1'))
        check_recovery(tr,[rec({'failed_tool':'bash','args':s['args'],'error':'ok','recovered':False,'recovery_kind':'none'})]*2,f.emitter('t1'),p)
        self.assertIn('RECOVERY_UNCERTAIN',self.codes(f));self.assertNotIn('RECOVERY_MISMATCH',self.codes(f))
    def test_sft_prefix_and_json_key_order_are_legal(self):
        t=raw();t['steps'][2].update(args='{"a":1,"b":2}',result='abcdefgh')
        v=rec({'i':2,'tool':'bash','tool_raw':'Bash','args':'{"b":2,"a":1}','ok':True,'result':'abcd'})
        f=Findings();tr=rec(t);p=check_trajectory(tr,f.emitter('t1'))
        check_cross(tr,None,None,[v],f.emitter('t1'),p,None)
        self.assertIn('VIEW_TRUNCATED',self.codes(f));self.assertNotIn('CROSS_VALUE',self.codes(f))
    def test_cross_step_index_and_status_mismatch(self):
        t=raw();v=rec({'i':2,'tool':'bash','tool_raw':'Bash','args':'{"command":"echo ok"}','ok':False,'result':'ok'})
        f=Findings();tr=rec(t);p=check_trajectory(tr,f.emitter('t1'))
        check_cross(tr,None,None,[v],f.emitter('t1'),p,None)
        issue=next(x for x in f.rows if x['rule_id']=='CROSS_VALUE')
        self.assertEqual(issue['source']['pointer'],'/ok');self.assertEqual(issue['related_sources'][0]['pointer'],'/steps/2/ok')
    def test_json_comparison_preserves_boolean_type(self):
        self.assertFalse(json_equivalent('{"x":true}','{"x":1}'))
    def test_edit_vendor_patch_not_missing_content(self):
        m=training();tools=json.loads(m['tools']);tools[0]['function']['name']='edit';tools[0]['function']['parameters']={'type':'object'};m['tools']=json.dumps(tools)
        f=m['messages'][1]['tool_calls'][0]['function'];f['name']='edit';f['arguments']=json.dumps({'input':'*** Begin Patch\n*** Update File: a\n-old\n+new\n*** End Patch'})
        findings,_=self.scan(m);self.assertNotIn('PARAMS_INCOMPLETE',self.codes(findings))
        f['arguments']='{"path":"a"}';findings,_=self.scan(m);self.assertIn('PARAMS_INCOMPLETE',self.codes(findings))


class TruncationRegressionTest(unittest.TestCase):
    """Synthetic boundaries plus two explicitly identified real result excerpts."""
    def check_result(self, result):
        t=raw();t['steps'][2]['result']=result
        f=Findings();check_trajectory(rec(t),f.emitter('t1'))
        return f.rows

    def test_real_source_literal_and_recorded_omission(self):
        cases=json.loads((Path(__file__).parent/'fixtures/truncation_cases.json').read_text())['cases']
        for case in cases:
            with self.subTest(trajectory_id=case['trajectory_id']):
                self.assertEqual(hashlib.sha256(case['result'].encode()).hexdigest(),case['result_sha256'])
                rows=self.check_result(case['result'])
                self.assertEqual(any(r['rule_id']=='RECORDED_TRUNCATION' for r in rows),case['expected_blocking'])

    def test_quoted_constants_are_not_observation_loss(self):
        for result in ("const expected = '[TRUNCATED]';", '123→    return "...TRUNCATED";',
                       "expect(value).toBe('[TRUNCATED]');", '27\t    const text = "before [TRUNCATED] after";',
                       'L12: expected = "[TRUNCATED]"', 'expected = "escaped \\\" [TRUNCATED]"'):
            with self.subTest(result=result):
                rows=self.check_result(result)
                self.assertNotIn('RECORDED_TRUNCATION',{r['rule_id'] for r in rows})
                self.assertIn('TRUNCATION_LITERAL',{r['rule_id'] for r in rows})

    def test_actual_markers_still_block_even_when_reading_source(self):
        for result in ('head\n... [TRUNCATED] ...\ntail', 'head\n[TRUNCATED]',
                       'head\n...TRUNCATED', '12\tconst expected = "ok";\n[TRUNCATED]',
                       'const expected = "[TRUNCATED]";\n... [TRUNCATED] ...'):
            with self.subTest(result=result):
                rows=self.check_result(result)
                issue=next(r for r in rows if r['rule_id']=='RECORDED_TRUNCATION')
                self.assertEqual(issue['suggested_action'],'defer')
                self.assertEqual(issue['category'],'insufficient_evidence')

    def test_unrecognised_context_is_uncertain_not_silently_allowed(self):
        for result in ('prefix [TRUNCATED] suffix', '"[TRUNCATED]"', '{"content":"[TRUNCATED]"}',
                       "const message = '[TRUNCATED]", 'output...TRUNCATED'):
            with self.subTest(result=result):
                issue=next(r for r in self.check_result(result) if r['rule_id']=='RECORDED_TRUNCATION')
                self.assertEqual(issue['evidence']['matches'][0]['context'],'ambiguous_marker')
                self.assertEqual(issue['suggested_action'],'defer')

    def test_literal_does_not_hide_second_marker_on_same_line(self):
        rows=self.check_result("const expected = '[TRUNCATED]'; output [TRUNCATED]")
        self.assertEqual({r['rule_id'] for r in rows},{'TRUNCATION_LITERAL','RECORDED_TRUNCATION'})
        hit=next(r for r in rows if r['rule_id']=='RECORDED_TRUNCATION')['evidence']['matches'][0]
        self.assertGreater(hit['offset'],30)

    def test_evidence_offsets_point_back_to_original_text(self):
        result='line1\n  ... [TRUNCATED] ...\nline3'
        issue=next(r for r in self.check_result(result) if r['rule_id']=='RECORDED_TRUNCATION')
        hit=issue['evidence']['matches'][0]
        self.assertEqual(result[hit['offset']:hit['offset']+len(hit['marker'])],hit['marker'])
        self.assertEqual(hit['result_line'],2)
        self.assertEqual(hit['context'],'standalone_notice')
        self.assertEqual(issue['source']['pointer'],'/steps/2/result')


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'raw';self.out=Path(self.tmp.name)/'out'
        self.config={'expected_revision':None,'expected_trajectories':None}
    def read(self,name):return [json.loads(x) for x in (self.out/name).read_text().splitlines()]
    def test_truncation_literal_admitted_and_notice_deferred_in_both_policies(self):
        values=fixture(self.root,n=20)
        for i,result in enumerate(("208\texpect(value).toBe('[TRUNCATED]');", 'head\n... [TRUNCATED] ...\ntail')):
            values['trajectories'][i]['steps'][2]['result']=result
            values['messages'][i]['messages'][2]['content']=result
            values['sft_steps'][i]['result']=result
        save_fixture(self.root,values)
        original=(self.root/'sample'/TABLES['trajectories']).read_bytes()
        for policy in ('balanced','conservative'):
            with self.subTest(policy=policy):
                run(self.root,self.out,{**self.config,'policy':policy})
                decisions={r['trajectory_id']:r for r in self.read('decisions.jsonl')}
                self.assertEqual(decisions['t1']['decision'],'admit')
                self.assertEqual(decisions['t2']['decision'],'defer')
                self.assertEqual(decisions['t2']['reason_codes'],['RECORDED_TRUNCATION'])
                self.assertEqual((self.root/'sample'/TABLES['trajectories']).read_bytes(),original)
    def test_full_run_repeatability_and_raw_preservation(self):
        fixture(self.root,n=20)
        before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        s=run(self.root,self.out,self.config)
        self.assertEqual(s['decisions'],{'admit':20});self.assertEqual(s['review_budget']['queued'],2)
        fact_names=['issues.jsonl','decisions.jsonl','review_queue.jsonl','summary.json','rules.json','config.json']
        first={n:(self.out/n).read_bytes() for n in fact_names}
        run(self.root,self.out,self.config)
        self.assertEqual(first,{n:(self.out/n).read_bytes() for n in fact_names})
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
    def test_budget_and_unqueued_defer(self):
        values=fixture(self.root,n=30)
        for m in values['messages']:
            m['messages'][1]['tool_calls'][0]['function']['arguments']='{}'
        save_fixture(self.root,values)
        s=run(self.root,self.out,self.config);q=self.read('review_queue.jsonl');d=self.read('decisions.jsonl')
        self.assertEqual(s['decisions'],{'defer':30});self.assertEqual(len(q),3)
        self.assertEqual(sum(x['review_status']=='deferred_over_budget' for x in d),27)
        self.assertEqual(len({x['trajectory_id'] for x in q}),3)
    def test_corrupt_line_is_reported_and_batch_deferred(self):
        fixture(self.root,n=10);p=self.root/'sample'/TABLES['messages']
        original=zstandard.ZstdDecompressor().decompress(p.read_bytes())
        p.write_bytes(zstandard.ZstdCompressor().compress(original+b'{BROKEN\n'))
        s=run(self.root,self.out,self.config)
        self.assertEqual(s['decisions'],{'defer':10,'exclude':1})
        self.assertEqual(s['review_budget']['denominator'],10)
        damaged=[x for x in self.read('decisions.jsonl') if x.get('corrupt')]
        self.assertEqual(damaged[0]['source_status'] if 'source_status' in damaged[0] else damaged[0]['check_status'],'damaged_input')
        self.assertEqual(damaged[0]['sources'][0]['line'],11)
    def test_corrupt_compression_and_empty_batch_reported(self):
        fixture(self.root);(self.root/'sample'/TABLES['messages']).write_bytes(b'not zstd')
        s=run(self.root,self.out,self.config)
        self.assertTrue(s['batch_problems']);self.assertEqual(self.read('decisions.jsonl')[0]['decision'],'defer')
    def test_missing_main_and_outside_parent(self):
        values=fixture(self.root);values['trajectories'][0]['parent_trajectory_id']='outside'
        values['labels']=[];save_fixture(self.root,values)
        run(self.root,self.out,self.config)
        codes={x['rule_id'] for x in self.read('issues.jsonl')}
        self.assertTrue({'MISSING_TABLE_ROW','PARENT_OUTSIDE'}<=codes)
        self.assertEqual(self.read('decisions.jsonl')[0]['decision'],'defer')
    def test_malformed_nested_shape_is_not_silent_pass(self):
        values=fixture(self.root);values['messages'][0]['messages'][1]=23;save_fixture(self.root,values)
        run(self.root,self.out,self.config)
        self.assertEqual(self.read('decisions.jsonl')[0]['decision'],'exclude')
        self.assertIn('STRUCTURE_INVALID',{x['rule_id'] for x in self.read('issues.jsonl')})
    def test_checker_failure_is_explicit(self):
        values=fixture(self.root);values['labels'][0]['metrics']=None;save_fixture(self.root,values)
        s=run(self.root,self.out,self.config)
        self.assertEqual(s['check_status'],{'failed':1});self.assertEqual(self.read('decisions.jsonl')[0]['decision'],'defer')
    def test_duplicate_id_and_duplicate_training_content(self):
        values=fixture(self.root,n=2);values['messages'][1]['messages']=copy.deepcopy(values['messages'][0]['messages'])
        save_fixture(self.root,values);run(self.root,self.out,self.config)
        self.assertEqual(sum(x['decision']=='exclude' for x in self.read('decisions.jsonl')),1)
        values['labels'].append(copy.deepcopy(values['labels'][0]));save_fixture(self.root,values)
        run(self.root,self.out,self.config);self.assertIn('DUPLICATE_ID',{x['rule_id'] for x in self.read('issues.jsonl')})
    def test_checksums_and_illegal_budget(self):
        fixture(self.root);(self.root/'sample/CHECKSUMS.json').write_text('{}')
        s=run(self.root,self.out,self.config);self.assertTrue(s['batch_problems'])
        with self.assertRaises(ValueError):run(self.root,self.out,{'review_fraction':0.11})
    def test_small_batch_does_not_round_budget_up(self):
        fixture(self.root,n=9)
        s=run(self.root,self.out,self.config)
        self.assertEqual(s['review_budget']['maximum'],0)
        self.assertEqual(self.read('review_queue.jsonl'),[])
    def test_no_output_overwrite_raw(self):
        fixture(self.root)
        with self.assertRaises(ValueError):run(self.root,self.root/'output',self.config)
    def test_policy_difference_is_config_driven(self):
        values=fixture(self.root);values['messages'][0]['is_eval']=values['trajectories'][0]['is_eval']=values['labels'][0]['is_eval']=True
        save_fixture(self.root,values)
        self.assertEqual(run(self.root,self.out,self.config)['decisions'],{'admit':1})
        self.assertEqual(run(self.root,self.out,{**self.config,'policy':'conservative'})['decisions'],{'defer':1})

if __name__=='__main__':unittest.main()
