import unittest
from evaluate import audit, citation_check


class AuditTests(unittest.TestCase):
    def test_sources_do_not_imply_citation(self):
        self.assertEqual(citation_check('资料未提供。', [{'n': 1}], False)[0], 'N/A')

    def test_missing_required_citation(self):
        self.assertEqual(citation_check('答案', [{'n': 1}], True)[0], 'FAIL')

    def test_invalid_number(self):
        self.assertEqual(citation_check('答案 [2]', [{'number': 1}], True)[0], 'FAIL')

    def test_multiple_citations(self):
        self.assertEqual(citation_check('答案 [1][2]，另一句[1, 2]', [{'n': 1}, {'n': 2}], True)[0], 'PASS')

    def setUp(self):
        self.q = {'case_id': 'test', 'expected_status': 'ok', 'expected_version_label': 'v1',
                  'expected_citation': True, 'required_facts': [{}, {}, {}]}
        self.r = {'http': 200, 'response': {'status': 'ok', 'version': {'version_label': 'v1'},
                  'answer': '回答[1]', 'sources': [{'number': i} for i in range(1, 5)]}}

    def test_no_fake_semantic_score(self):
        result = audit(self.q, self.r)
        self.assertEqual(result['coverage3'], 'REVIEW')
        self.assertEqual(result['hit3'], 'REVIEW')

    def test_fact_denominator_and_thirds(self):
        result = audit(self.q, self.r, {'facts_covered': [True, False, False], 'reviewer': 'r', 'notes': 'n'})
        self.assertAlmostEqual(result['coverage3'], 1/3)

    def test_reject_wrong_annotation_count(self):
        with self.assertRaises(ValueError):
            audit(self.q, self.r, {'facts_covered': [True]})

    def test_fourth_source_not_top_three(self):
        with self.assertRaises(ValueError):
            audit(self.q, self.r, {'relevant_source_numbers': [4], 'reviewer': 'r', 'notes': 'n'})

    def test_http_failure_not_pass(self):
        self.r['http'] = 500
        result = audit(self.q, self.r)
        self.assertEqual(result['status_match'], 'FAIL')
        self.assertEqual(result['version_label_match'], 'FAIL')

    def test_label_not_identity(self):
        self.assertEqual(audit(self.q, self.r)['version_identity_match'], 'NOT_CHECKED')


if __name__ == '__main__':
    unittest.main()
