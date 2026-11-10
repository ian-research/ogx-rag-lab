"""Real local MCP HTTP transport; document/model dependencies are controlled doubles.

These are not server vLLM/OGX quality tests or systemd/reboot tests.
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import orchestrator
import settings
import transports
from business_data import TICKETS
from tool_contracts import RunFailure, SCHEMAS


@pytest.fixture
def live(private_config, monkeypatch, tmp_path):
    document_requests = []
    class Documents(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            document_requests.append(data)
            response = {'status': 'ok', 'as_of': data['as_of'], 'scope': 'all',
                'version': {'version_id': 'version-test', 'version_label': 'v1', 'valid_from': '2026-01-01'},
                'sources': [{'number': 1, 'source': 'test.md', 'chunk_id': 'chunk-test',
                    'version_id': 'version-test', 'text': '完整条款' * 150 + '末尾条件必须保留。'}]}
            raw = json.dumps(response).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(raw)
    doc_server = ThreadingHTTPServer(('127.0.0.1', 0), Documents)
    thread = threading.Thread(target=doc_server.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    monkeypatch.setattr(settings, 'MCP_URL', f'http://127.0.0.1:{port}/mcp')
    env = {**os.environ, 'PRIVATE_DIR': str(settings.PRIVATE),
           'DOC_SERVICE': f'http://127.0.0.1:{doc_server.server_port}'}
    process = None
    logfile = (tmp_path / 'mcp.log').open('w+')
    def stop():
        nonlocal process
        if process is not None and process.poll() is None:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
    def start():
        nonlocal process
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'mcp_business_server:app',
            '--host', '127.0.0.1', '--port', str(port), '--no-access-log'],
            cwd=settings.ROOT, env=env, stdout=logfile, stderr=logfile)
        for _ in range(100):
            try:
                if httpx.get(f'http://127.0.0.1:{port}/health', timeout=.2).status_code == 200: return
            except httpx.HTTPError: pass
            if process.poll() is not None:
                logfile.seek(0); pytest.fail(logfile.read())
            time.sleep(.05)
        pytest.fail('local MCP startup timed out')
    try:
        start()
        yield {'tokens': private_config, 'documents': document_requests, 'start': start, 'stop': stop}
    finally:
        stop(); doc_server.shutdown(); doc_server.server_close(); thread.join(timeout=2); logfile.close()


def test_real_mcp_auth_discovery_and_ownership(live):
    with httpx.Client() as client:
        response = client.post(settings.MCP_URL, headers={'X-Authoritative-User-Id': 'alice.johnson@company.com'})
        assert response.status_code == 401  # trusted identity header alone is not authentication
    async def run():
        tools = await transports.discover('alice.johnson@company.com', '2026-11-09')
        assert {t['function']['name'] for t in tools} == set(SCHEMAS)
        a = await transports.call_tool('get_employee_laptop_info', {}, 'alice.johnson@company.com', '2026-11-09')
        b = await transports.call_tool('get_employee_laptop_info', {}, 'john.doe@company.com', '2026-11-09')
        assert a['serial_number'] != b['serial_number']
        empty = await transports.call_tool('get_employee_laptop_info', {}, 'empty@example.invalid', '2026-11-09')
        assert empty['status'] == 'no_record'
        ticket = next(iter(TICKETS.values()))
        owner = ticket['owner_email']
        other = 'john.doe@company.com' if owner != 'john.doe@company.com' else 'alice.johnson@company.com'
        good = await transports.call_tool('get_ticket', {'ticket_id': ticket['ticket_id']}, owner, '2026-11-09')
        denied = await transports.call_tool('get_ticket', {'ticket_id': ticket['ticket_id']}, other, '2026-11-09')
        missing = await transports.call_tool('get_ticket', {'ticket_id': 'DOES-NOT-EXIST'}, other, '2026-11-09')
        assert good['status'] == 'ok' and denied == missing
    asyncio.run(run())


def test_real_document_date_and_full_source(live):
    async def run():
        result = await transports.call_tool('search_documents', {'question': '设备条件', 'policy_id': 'eval3-device'},
                                            'alice.johnson@company.com', '2026-03-01')
        assert result['as_of'] == '2026-03-01'
        assert result['sources'][0]['text'].endswith('末尾条件必须保留。')
        assert len(result['sources'][0]['text']) > 400
        assert result['sources'][0]['chunk_id'] == 'chunk-test'
        assert result['version']['version_id'] == 'version-test'
    asyncio.run(run())
    assert live['documents'][0]['as_of'] == '2026-03-01'


def test_real_mcp_error_is_not_success(live):
    async def run():
        with pytest.raises(RunFailure) as error:
            await transports.call_tool('unknown_tool', {}, 'alice.johnson@company.com', '2026-11-09')
        assert error.value.status == 'tool_error'
    asyncio.run(run())


def test_api_through_real_mcp_stop_and_recovery(live, monkeypatch):
    fail_during_request = False
    async def model(messages, tools):
        nonlocal fail_during_request
        if fail_during_request:
            live['stop']()  # discovery succeeded; fail before the actual tool call
            fail_during_request = False
        if messages[-1]['role'] == 'tool':
            payload = json.loads(messages[-1]['content'])
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': payload['laptop_model']}}]}
        return {'choices': [{'finish_reason': 'tool_calls', 'message': {'content': '', 'tool_calls': [
            {'id': 'real-call', 'type': 'function', 'function': {'name': 'get_employee_laptop_info', 'arguments': '{}'}}]}}]}
    monkeypatch.setattr(transports, 'ask_model', model)
    with TestClient(orchestrator.app) as client:
        def ask(): return client.post('/api/ask', headers={'Authorization': 'Bearer ' + live['tokens']['a']},
                                      json={'question': '我的笔记本', 'as_of': '2026-11-09'})
        assert ask().json()['answer'] == 'Latitude 7420'
        live['stop']()
        failed = ask()
        assert failed.status_code == 503 and failed.json()['status'] == 'backend_unavailable'
        assert (settings.RUN_ROOT / (failed.json()['run_id'] + '.json')).exists()
        live['start']()
        assert ask().json()['answer'] == 'Latitude 7420'
        fail_during_request = True
        failed_mid_request = ask()
        assert failed_mid_request.status_code == 503
        assert failed_mid_request.json()['steps'][-1]['kind'] == 'tool'
        assert failed_mid_request.json()['steps'][-1]['status'] == 'backend_unavailable'
        live['start']()
        assert ask().json()['answer'] == 'Latitude 7420'
