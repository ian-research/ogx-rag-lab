"""Capture complete responses in a new immutable run directory; no automatic grading."""
import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--questions', type=Path, required=True)
    p.add_argument('--output-root', type=Path, required=True)
    p.add_argument('--api', default='http://127.0.0.1:8089')
    p.add_argument('--manifest', type=Path, required=True,
                   help='Frozen environment/versions/model revisions and generation settings')
    p.add_argument('--timeout', type=float, default=300)
    a = p.parse_args()
    qb = a.questions.read_bytes()
    qs = [json.loads(l) for l in qb.decode('utf-8').splitlines() if l.strip()]
    if not qs or len({q['case_id'] for q in qs}) != len(qs):
        raise ValueError('Empty dataset or duplicate IDs')
    manifest = json.loads(a.manifest.read_text())
    run = a.output_root / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
    run.mkdir(parents=True, exist_ok=False)
    (run / 'questions.jsonl').write_bytes(qb)
    (run / 'manifest.json').write_text(json.dumps({
        'api': a.api, 'question_sha256': hashlib.sha256(qb).hexdigest(),
        'environment': manifest, 'collector_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'started_at': datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2))
    total_start = time.perf_counter()
    with (run / 'responses.jsonl').open('x') as f:
        for q in qs:
            body = {k: q[k] for k in ('question', 'scope', 'as_of', 'policy_id')}
            req = urllib.request.Request(a.api.rstrip('/') + '/api/ask',
                data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'}, method='POST')
            started = datetime.now(timezone.utc).isoformat()
            t0 = time.perf_counter()
            status, raw, error = 0, '', None
            try:
                with urllib.request.urlopen(req, timeout=a.timeout) as r:
                    status, raw = r.status, r.read().decode('utf-8')
            except urllib.error.HTTPError as e:
                status, raw = e.code, e.read().decode('utf-8', errors='replace')
            except Exception as e:
                error = f'{type(e).__name__}: {e}'
            elapsed = round((time.perf_counter() - t0) * 1000, 3)
            try:
                response = json.loads(raw)
                if not isinstance(response, dict):
                    error = 'Response was not a JSON object'
                    response = {}
            except ValueError:
                response = {}
                error = error or 'Response was not valid JSON'
            rec = {'case_id': q['case_id'], 'request': body, 'started_at': started,
                   'http': status, 'elapsed_ms': elapsed, 'response': response,
                   'raw_body': raw, 'transport_error': error}
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
            print(q['case_id'], status, response.get('status'), flush=True)
    (run / 'completed.json').write_text(json.dumps({
        'completed_requests': len(qs), 'wall_elapsed_ms': round((time.perf_counter() - total_start) * 1000, 3),
        'completed_at': datetime.now(timezone.utc).isoformat()}))
    print(run)


if __name__ == '__main__':
    main()
