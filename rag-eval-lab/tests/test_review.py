import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import preflight
import review

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'examples/diagnostic-v3'


class ReviewTests(unittest.TestCase):
    def test_published_scores_reproduce_confirmed_counts(self):
        result = review.summarize(RUN, RUN / 'initial-review.json')
        self.assertEqual(result['groups']['answerable']['fact_accuracy'], {'PASS': 14, 'FAIL': 9})
        self.assertEqual(result['groups']['insufficient']['refusal_appropriateness'], {'APPROPRIATE': 5})
        self.assertEqual(result['groups']['boundary_no_data']['count'], 2)
        self.assertEqual(result['fact_coverage']['covered_facts'], 24)
        self.assertEqual(result['fact_coverage']['required_facts'], 41)
        self.assertEqual({p for p, fields in result['paired_field_differences'].items() if fields}, {'K4', 'K8', 'X1', 'X3'})

    def test_frozen_selection_reproduces_six_cases(self):
        selected = review.select(RUN, '20261104T170909Z-7595b62b')
        self.assertEqual(selected['selected_case_ids'], ['K5-a', 'K5-b', 'K4-a', 'X1-a', 'N2', 'B3'])
        self.assertEqual(selected['seed'], '5a897f8069735b6c140d05034b4b6ce935ff120f7baf62e623113c564c7e7a59')

    def test_packet_is_blank_and_keeps_complete_answers(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / 'review'
            review.prepare(RUN, ROOT / 'dataset/docs', out)
            blank = json.loads((out / 'review.json').read_text())
            self.assertEqual(blank['cases'][0]['fact_accuracy'], 'PENDING')
            self.assertIsNone(blank['cases'][0]['evidence'][0]['covered'])
            self.assertEqual(blank['cases'][-1]['fact_accuracy'], 'N/A')
            for line in (RUN / 'responses.jsonl').read_text().splitlines():
                self.assertIn(json.loads(line)['response']['answer'], (out / 'review.md').read_text())
            with self.assertRaises(FileExistsError):
                review.prepare(RUN, ROOT / 'dataset/docs', out)

    def setUp(self):
        self.qs, self.rs, self.hashes, _ = review.inputs(RUN)
        self.grades = json.loads((RUN / 'initial-review.json').read_text())

    def test_reject_stale_or_incomplete_review(self):
        self.grades['responses_sha256'] = 'old'
        with self.assertRaises(ValueError):
            review.validate_review(self.qs, self.rs, self.hashes, self.grades)
        self.grades['responses_sha256'] = self.hashes['responses_sha256']
        self.grades['cases'][0]['fact_accuracy'] = 'PENDING'
        with self.assertRaises(ValueError):
            review.validate_review(self.qs, self.rs, self.hashes, self.grades)

    def test_cannot_mark_covered_with_unreturned_source(self):
        self.grades['cases'][0]['evidence'][0]['source_numbers'] = [4]
        with self.assertRaises(ValueError):
            review.validate_review(self.qs, self.rs, self.hashes, self.grades)

    def test_boundary_does_not_enter_generated_answer_count(self):
        self.grades['cases'][-1]['fact_accuracy'] = 'PASS'
        with self.assertRaises(ValueError):
            review.validate_review(self.qs, self.rs, self.hashes, self.grades)

    def test_source_hash_mismatch_stops_packet(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch('review.sha', return_value='wrong'):
                with self.assertRaises(ValueError):
                    review.prepare(RUN, ROOT / 'dataset/docs', Path(temp) / 'out')


class PreflightTests(unittest.TestCase):
    def test_server_file_and_publication_state_must_match(self):
        env = json.loads((ROOT / 'environment.example.json').read_text())
        files = {('eval3-device','v1'): 'eval-device.md', ('eval3-security','v1'): 'eval-security.md',
                 ('eval3-travel','v1'): 'eval-travel-v1.md', ('eval3-travel','v2'): 'eval-travel-v2.md',
                 ('eval3-vendor','v1'): 'eval-vendor.md'}
        rows=[]
        for index, v in enumerate(env['published_versions']):
            name=files[v['policy_id'],v['version_label']]
            rows.append({**v, 'filename':name,'sha256':env['documents'][name],
                         'job_status':'ready','version_id':f'v{index}','store_id':f's{index}'})
        def fake_get(api,path):
            from urllib.parse import parse_qs,urlsplit
            policy=parse_qs(urlsplit(path).query)['policy_id'][0]
            return {'versions':[x for x in rows if x['policy_id']==policy]}
        with patch('preflight.get', side_effect=fake_get):
            self.assertEqual(len(preflight.verify_versions(env,ROOT/'dataset/docs','http://local')),5)
            rows[0]['sha256']='different'
            with self.assertRaises(ValueError):
                preflight.verify_versions(env,ROOT/'dataset/docs','http://local')
            rows[0]['sha256']=env['documents'][rows[0]['filename']]
            rows[0]['publication_status']='draft'
            with self.assertRaises(ValueError):
                preflight.verify_versions(env,ROOT/'dataset/docs','http://local')


if __name__ == '__main__':
    unittest.main()
