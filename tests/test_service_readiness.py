import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch, AsyncMock

spec = importlib.util.spec_from_file_location('ready', Path(__file__).parents[1] / 'scripts/wait-ready.py')
ready = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ready)


class ReadinessTests(unittest.TestCase):
    def test_wrong_model_not_ready(self):
        with patch.object(ready, 'http_json', return_value={'data': [{'id': 'other'}]}):
            self.assertFalse(ready.check('vllm'))

    def test_ogx_requires_both_registrations(self):
        with patch.object(ready, 'http_json', return_value={'data': [{'id': 'local-vllm/rag-llm'}]}):
            self.assertFalse(ready.check('ogx'))
        with patch.object(ready, 'http_json', return_value={'data': [{'id': x} for x in ['local-vllm/rag-llm', 'cpu-embedding/bge-small-zh']]}):
            self.assertTrue(ready.check('ogx'))

    def test_api_without_worker_not_ready(self):
        with patch.object(ready, 'http_json', return_value={'api': 'ok', 'worker_alive': False}):
            self.assertFalse(ready.check('app'))

    def test_auth_failure_never_passes_or_leaks(self):
        with patch.object(ready, 'check', side_effect=RuntimeError('password=secret')), patch.object(ready.time, 'monotonic', side_effect=[0, 0, 2, 2]), patch.object(ready.time, 'sleep'):
            with self.assertRaises(SystemExit) as caught:
                ready.wait('database', 1)
            self.assertNotIn('secret', str(caught.exception))
            self.assertIn('timeout', str(caught.exception))

    def test_vector_sql_failure_closes_connection(self):
        connection = types.SimpleNamespace(fetchval=AsyncMock(side_effect=RuntimeError('missing vector')), close=AsyncMock())
        module = types.SimpleNamespace(connect=AsyncMock(return_value=connection))
        with patch.dict(sys.modules, {'asyncpg': module}), patch.dict(ready.os.environ, {'PGVECTOR_PASSWORD': 'test'}):
            with self.assertRaises(RuntimeError):
                asyncio.run(ready.database_ready())
        connection.close.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
