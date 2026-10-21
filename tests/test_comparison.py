"""Synthetic comparison cases; not real-data review labels."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from agent_qc.pipeline import run
from scripts.compare_policies import changes, check_compatible, compare
from test_qc import fixture, save_fixture


class ComparisonTest(unittest.TestCase):
    def manifests(self):
        a={'code_sha256':'code','data_sha256':{'sample/file':'hash'},'dependencies':{'x':'1'},
           'config':{'policy':'balanced','seed':1,'review_fraction':0.1}}
        b=copy.deepcopy(a);b['config']['policy']='conservative'
        return a,b
    def test_only_policy_may_differ(self):
        a,b=self.manifests();check_compatible(a,b)
        for change in ('code','data','seed'):
            a,b=self.manifests()
            if change=='code':b['code_sha256']='other'
            elif change=='data':b['data_sha256']['sample/file']='changed'
            else:b['config']['seed']=2
            with self.assertRaises(ValueError):check_compatible(a,b)
    def test_reason_only_change_not_counted_as_decision_change(self):
        def row(d,reasons):return {'decision':d,'reason_codes':reasons,'review_status':'not_queued','sources':[],'tool_calls':1}
        a={'a':row('admit',['PASS']),'b':row('defer',['SCHEMA_MISMATCH']),'c':row('exclude',['NO_TOOL_TARGET'])}
        b={'a':row('defer',['STRICT_BOUNDARY','STRICT_EVAL']),
           'b':row('defer',['SCHEMA_MISMATCH','STRICT_EVAL']),'c':row('exclude',['NO_TOOL_TARGET'])}
        matrix,changed,reason_only=changes(a,b)
        self.assertEqual(len(changed),1);self.assertEqual(len(reason_only),1)
        self.assertEqual(changed[0]['policy_causes'],['STRICT_BOUNDARY','STRICT_EVAL'])
        self.assertEqual(matrix,{'admit->defer':1,'defer->defer':1,'exclude->exclude':1})
    def test_different_scope_cannot_be_compared(self):
        with self.assertRaises(ValueError):changes({'a':{}},{'b':{}})
    def test_complete_comparison_of_actual_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);raw=root/'raw';values=fixture(raw,n=10)
            # One overlaps both policy restrictions; another was already deferred.
            for table in ('messages','labels','trajectories'):values[table][0]['is_eval']=True
            values['messages'][0]['messages'].pop()
            values['messages'][1]['messages'][1]['tool_calls'][0]['function']['arguments']='{}'
            values['messages'][1]['messages'].pop()
            save_fixture(raw,values)
            cfg={'expected_revision':None,'expected_trajectories':None}
            run(raw,root/'a',cfg);run(raw,root/'b',{**cfg,'policy':'conservative'})
            s=compare(root/'a',root/'b',raw,root/'comparison')
            self.assertEqual(s['changed_trajectory_count'],1)
            self.assertEqual(s['changed_cause_counts'],{'STRICT_BOUNDARY+STRICT_EVAL':1})
            self.assertEqual(s['same_decision_with_additional_blocking_reasons'],1)
            self.assertEqual(s['balanced_minus_conservative_retained']['tool_calls'],1)
            self.assertEqual(s['review']['balanced']['maximum'],1)
            self.assertTrue((root/'comparison/decision_changes.csv').exists())
            # Comparing stale derived outputs to altered raw data must fail.
            p=raw/'sample'/ 'trajectories/trajectories.jsonl.zst';p.write_bytes(b'changed')
            with self.assertRaises(ValueError):compare(root/'a',root/'b',raw,root/'comparison')

if __name__=='__main__':unittest.main()
