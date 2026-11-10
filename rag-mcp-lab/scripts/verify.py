"""Collect authenticated smoke checks. Semantic answers remain pending human review."""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import settings
from security import private_text, redact, write_private
from business_data import TICKETS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8091')
    parser.add_argument('--phase', choices=['baseline', 'mcp-down', 'recovered'], default='baseline')
    parser.add_argument('--as-of', required=True)
    args = parser.parse_args()
    from datetime import date
    assert date.fromisoformat(args.as_of).isoformat() == args.as_of
    tokens = json.loads(private_text(settings.PRIVATE / 'credentials-for-delivery.json'))
    own = next(t['ticket_id'] for t in TICKETS.values() if t['owner_email'] == 'alice.johnson@company.com')
    other = next(t['ticket_id'] for t in TICKETS.values() if t['owner_email'] != 'alice.johnson@company.com')
    stamp = datetime.now(ZoneInfo('UTC')).strftime('%Y%m%dT%H%M%SZ')
    root = settings.ROOT / 'evidence' / f'{stamp}-{args.phase}'
    cases = [('device-a', 'lab-a', '我的笔记本是什么型号，什么时候买的？'),
             ('device-b', 'lab-b', '我的笔记本是什么型号？'),
             ('device-empty', 'lab-empty', '我的笔记本是什么型号？'),
             ('ticket-own', 'lab-a', f'查询工单 {own} 当前进展，有预计完成时间吗？'),
             ('ticket-other', 'lab-a', f'查询工单 {other} 当前进展'),
             ('ticket-missing', 'lab-a', '查询工单 MISSING-000 当前进展'),
             ('ticket-clarify', 'lab-a', '帮我查一下工单进展'),
             ('write-denied', 'lab-a', f'关闭工单 {own}'),
             ('document', 'lab-a', '上海出差住宿每晚最多报多少？'),
             ('combined', 'lab-a', '查我的设备并对照设备文档，能否确认符合换新条件？')]
    if args.phase == 'mcp-down': cases = cases[:1]
    results = []
    with httpx.Client(base_url=args.url, timeout=settings.REQUEST_TIMEOUT + 15) as client:
        assert client.get('/api/sessions').status_code == 404, 'credential enumeration is still exposed'
        for auth in ['', 'Bearer ' + 'invalid-' * 8]:
            response = client.post('/api/ask', headers={'Authorization': auth}, json={'question': '我的设备'})
            assert response.status_code == 401
        for case_id, session, question in cases:
            response = client.post('/api/ask', headers={'Authorization': 'Bearer ' + tokens[session]},
                                   json={'question': question, 'as_of': args.as_of})
            body = response.json()
            record = {'case_id': case_id, 'phase': args.phase, 'session_id': session,
                      'http_status': response.status_code, 'response': body,
                      'semantic_review': 'pending'}
            write_private(root / (case_id + '.json'), json.dumps(redact(record, tokens.values()), ensure_ascii=False, indent=2))
            if args.phase == 'mcp-down':
                assert response.status_code == 503 and body.get('status') == 'backend_unavailable'
            else:
                assert response.status_code == 200 and body.get('status') == 'ok', f'{case_id}: execution failed; see saved evidence'
            assert body.get('run_id'), 'missing full run ID'
            results.append({'case_id': case_id, 'run_id': body['run_id'], 'control_flow': 'pass', 'semantic_review': 'pending'})
    write_private(root / 'summary.json', json.dumps(results, indent=2))
    print(f'控制流检查完成，原始记录：{root}。回答语义仍待人工逐项核对。')


if __name__ == '__main__': main()
