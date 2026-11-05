import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import collect


class CaptureTests(unittest.TestCase):
    def test_complete_response_and_unique_runs(self):
        body = {'status': 'ok', 'answer': '完整答案' * 600,
                'sources': [{'text': '片段' * 500, 'chunk_id': 'f' * 64, 'score': 0.1}],
                'version': {'version_id': 'id-1', 'store_id': 'store-1'}}
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(body).encode()
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            (p/'q.jsonl').write_text(json.dumps({'case_id': 'a', 'question': '问', 'scope': 'all',
                                              'as_of': '2026-03-01', 'policy_id': 'eval'}))
            (p/'m.json').write_text('{}')
            argv = ['collect.py', '--questions', str(p/'q.jsonl'), '--manifest', str(p/'m.json'),
                    '--output-root', str(p/'runs')]
            with patch('sys.argv', argv), patch('urllib.request.urlopen', return_value=Response()), contextlib.redirect_stdout(io.StringIO()):
                collect.main()
                collect.main()
            runs = list((p/'runs').iterdir())
            self.assertEqual(len(runs), 2)
            for run in runs:
                r = json.loads((run/'responses.jsonl').read_text())
                self.assertEqual(r['response'], body)
                self.assertTrue((run/'completed.json').exists())

    def test_http_error_preserves_body(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            (p/'q.jsonl').write_text(json.dumps({'case_id': 'a', 'question': 'q', 'scope': 'all',
                                              'as_of': '2026-03-01', 'policy_id': 'eval'}))
            (p/'m.json').write_text('{}')
            argv = ['collect.py', '--questions', str(p/'q.jsonl'), '--manifest', str(p/'m.json'),
                    '--output-root', str(p/'runs')]
            error = urllib.error.HTTPError('http://localhost', 500, 'error', {}, io.BytesIO(b'{"detail":"failure"}'))
            with patch('sys.argv', argv), patch('urllib.request.urlopen', side_effect=error), contextlib.redirect_stdout(io.StringIO()):
                collect.main()
            r = json.loads(next((p/'runs').glob('*/responses.jsonl')).read_text())
            self.assertEqual(r['http'], 500)
            self.assertEqual(r['response']['detail'], 'failure')


if __name__ == '__main__':
    unittest.main()
