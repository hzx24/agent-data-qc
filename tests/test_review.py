"""Synthetic review-integrity boundaries; these do not count as real reviews."""
import unittest

from scripts.render_review import pointer_value, resolve_evidence, assemble, merge_human_reviews


class ReviewIntegrityTests(unittest.TestCase):
    def test_real_pointer_and_serialized_schema_are_distinguished(self):
        packet = {'messages': {'file': 'messages.jsonl.zst', 'line': 3,
            'data': {'tools': '[{"name":"read"},{"name":"edit"}]'}}}
        e = resolve_evidence(packet, {'table': 'messages', 'pointer': '/tools',
                                     'decoded_pointer': '/1', 'contains': 'edit'})
        self.assertEqual(e['pointer'], '/tools')
        self.assertEqual(e['decoded_pointer'], '/1')
        self.assertEqual(e['line'], 3)
        with self.assertRaises(TypeError):
            resolve_evidence(packet, {'table': 'messages', 'pointer': '/tools/1'})

    def test_absent_claimed_excerpt_is_rejected(self):
        packet = {'trajectories': {'file': 'raw', 'line': 1, 'data': {'result': 'unknown'}}}
        with self.assertRaisesRegex(ValueError, 'not present'):
            resolve_evidence(packet, {'table': 'trajectories', 'pointer': '/result',
                                      'contains': 'all tests passed'})

    def test_ambiguous_view_line_is_rejected(self):
        record = {'line': 7, 'file': 'view', 'data': {'result': 'ok'}}
        for records in ([], [record, record]):
            with self.assertRaisesRegex(ValueError, 'absent or ambiguous'):
                resolve_evidence({'sft_steps': records},
                    {'table': 'sft_steps', 'line': 7, 'pointer': '/result'})

    def test_missing_or_duplicate_review_ids_do_not_count_as_thirty(self):
        groups = [{'trajectory_id': str(i)} for i in range(30)]
        with self.assertRaisesRegex(ValueError, 'exactly'):
            assemble({'groups': groups}, {'items': [{'trajectory_id': '0'}] * 30}, {})
        with self.assertRaisesRegex(ValueError, 'unique'):
            assemble({'groups': [groups[0]] * 30}, {'items': []}, {})

    def test_json_pointer_escapes(self):
        self.assertEqual(pointer_value({'a/b': [{'~': 'literal'}]}, '/a~1b/0/~0'), 'literal')

    def test_human_confirmation_requires_matching_version_and_source(self):
        row={'trajectory_id':'t1','pipeline_decision':'admit','status':'pending_human_confirmation'}
        h={'trajectory_id':'t1','decision':'defer','reviewer':'reviewer','reviewed_at':'2026-10-20'}
        document={'reviews':[h],'annotations_sha256':'old','confirmation_source':'explicit confirmation'}
        with self.assertRaisesRegex(ValueError,'version'):
            merge_human_reviews([row],document,'new')
        document['annotations_sha256']='new'
        merged=merge_human_reviews([row],document,'new')
        self.assertEqual(merged[0]['status'],'human_confirmed')
        self.assertEqual(merged[0]['recommendation'],'defer')
        self.assertTrue(merged[0]['proposal_changes_pipeline_decision'])

    def test_missing_confirmations_stay_pending_and_duplicates_are_rejected(self):
        row={'trajectory_id':'t1','status':'pending_human_confirmation'}
        self.assertEqual(merge_human_reviews([row],{'reviews':[]},'v')[0]['status'],'pending_human_confirmation')
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            merge_human_reviews([row],{'reviews':[{'trajectory_id':'t1'}]*2},'v')


if __name__ == '__main__':
    unittest.main()
