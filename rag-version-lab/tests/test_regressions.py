import importlib
import json
import os
import sqlite3
import sys
import types
from contextlib import nullcontext
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import version_store as store
import version_rules as rules


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(store, 'DB', tmp_path / 'version.db')
    store.init()
    # No models, network, or real database are used in these regression tests.
    monkeypatch.setitem(sys.modules, 'ogx_client', types.SimpleNamespace(client=lambda: nullcontext(None), call=lambda *a: None))
    monkeypatch.setitem(sys.modules, 'settings', types.SimpleNamespace(LLM='test', EMBED_MODEL='embed', DIM=512, MAX_TOKENS=384, TOKENIZER='test'))
    app = importlib.import_module('version_app')
    worker = importlib.import_module('version_worker')
    monkeypatch.setattr(app, 'FILES', tmp_path / 'files')
    monkeypatch.setattr(worker, 'DATA', tmp_path)
    monkeypatch.setattr(worker, 'WORK', tmp_path / 'work')
    monkeypatch.setattr(worker, 'ARTIFACTS', tmp_path / 'artifacts')
    with TestClient(app.app, raise_server_exceptions=False) as client:
        yield app, worker, client


def add(vid, start='2026-01-01', end=None, sup=None, policy='p', status='ready', pub='draft'):
    with store.connect() as conn:
        conn.execute("INSERT INTO versions(version_id,policy_id,version_label,scope,job_status,publication_status,valid_from,valid_to,supersedes,created_at,store_id,filename,source_path,sha256) VALUES(?,?,?,'all',?,?,?,?,?,'now','vs','test.pdf','unused','hash')", (vid, policy, vid, status, pub, start, end, sup))


def publish(vid):
    with store.connect() as conn:
        return rules.publish(conn, vid)


def query(**kwargs):
    return dict(question='电脑使用3年能申请换新吗？', scope='all', policy_id='p', as_of='2026-10-27', **kwargs)


def test_no_extension_or_partial_publish(env):
    add('a', end='2026-03-01'); publish('a')
    add('b', start='2026-03-01', end='2026-06-01'); publish('b')
    add('c', start='2026-07-01', sup='a')
    with pytest.raises(rules.RuleError): publish('c')
    with store.connect() as conn:
        assert store.get(conn, 'a')['valid_to'] == '2026-03-01'
        assert store.get(conn, 'c')['publication_status'] == 'draft'
        assert rules.resolve(conn, 'p', 'all', '2026-03-15')['versions'][0]['version_id'] == 'b'
        assert conn.execute("SELECT COUNT(*) FROM publish_log WHERE version_id='c'").fetchone()[0] == 0


def test_transaction_rollback_after_old_version_update(env):
    add('a'); publish('a'); add('b', start='2026-11-01', sup='a')
    with store.connect() as conn:
        conn.execute("CREATE TRIGGER fail_publish BEFORE UPDATE OF publication_status ON versions WHEN NEW.version_id='b' BEGIN SELECT RAISE(ABORT,'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError): publish('b')
    with store.connect() as conn:
        assert store.get(conn, 'a')['valid_to'] is None
        assert store.get(conn, 'b')['publication_status'] == 'draft'
        assert conn.execute("SELECT COUNT(*) FROM publish_log WHERE action='supersede'").fetchone()[0] == 0


@pytest.mark.parametrize('value', ['2026/11/01', '20261101', '2026-02-30', '2026-W44-7', '   '])
def test_upload_bad_dates_are_400(env, value):
    _, _, client = env
    response = client.post('/api/versions', data=dict(policy_id='p', version_label='v1', scope='all', valid_from=value), files={'file': ('a.md', b'policy')})
    assert response.status_code == 400
    assert not list(store.DATA.glob('files/*'))


def test_upload_duplicate_cleanup_and_limits(env, monkeypatch):
    app, _, client = env
    body = dict(policy_id='p', version_label='v1', scope='all', valid_from='2026-01-01')
    assert client.post('/api/versions', data=body, files={'file': ('a.md', b'policy')}).status_code == 202
    assert client.post('/api/versions', data=body, files={'file': ('a.md', b'policy')}).status_code == 409
    assert len(list(app.FILES.iterdir())) == 1
    body['version_label'] = 'v2'
    monkeypatch.setattr(app, 'MAX_BYTES', 3)
    assert client.post('/api/versions', data=body, files={'file': ('a.md', b'large')}).status_code == 413
    assert len(list(app.FILES.iterdir())) == 1


@pytest.mark.parametrize('field', ['admin_version_id', 'document_ids'])
def test_no_hidden_preview_or_unknown_fields(env, field):
    _, _, client = env
    assert client.post('/api/ask', json=query(**{field: 'a'})).status_code == 422


def test_multiple_policies_are_selection_not_conflict(env):
    _, _, client = env
    add('a'); publish('a'); add('b', policy='holiday'); publish('b')
    body = query(); body.pop('policy_id')
    result = client.post('/api/ask', json=body).json()
    assert result['status'] == 'needs_selection'
    assert result['answer'] == '存在多个适用文档，请从候选中指定 policy_id 后再查询；这不表示正文存在冲突。'
    assert len(result['candidates']) == 2
    assert result['sources'] == []


def test_dates_and_sources_generation_parity(env, monkeypatch):
    app, _, client = env
    add('a'); publish('a'); add('b', start='2026-11-01', sup='a'); publish('b')
    requests = []
    def call(c, method, path, body):
        requests.append((path, body))
        if path == '/vector-io/query':
            return {'chunks': [{'content': '员工电脑使用满4年可以申请换新。', 'chunk_id': 'one', 'metadata': {'source': 'test.pdf', 'page': '7', 'block': 2}}], 'scores': [0.8]}
        return {'choices': [{'message': {'content': '未达到换新年限 [1]。'}, 'finish_reason': 'stop'}]}
    monkeypatch.setattr(app, 'call', call)
    result = client.post('/api/ask', json=query()).json()
    assert result['version']['version_id'] == 'a'
    assert result['sources'][0]['page'] == '7'
    generated = requests[-1][1]
    assert generated['temperature'] == 0.7 and generated['top_p'] == 0.8
    assert generated['messages'][0]['role'] == 'system'
    body = query(); body['as_of'] = '2026-11-01'
    assert client.post('/api/search', json=body).json()['version']['version_id'] == 'b'


def test_empty_retrieval_never_calls_generation(env, monkeypatch):
    app, _, client = env
    add('a'); publish('a')
    def call(c, method, path, body):
        assert path == '/vector-io/query'
        return {'chunks': [], 'scores': []}
    monkeypatch.setattr(app, 'call', call)
    assert client.post('/api/ask', json=query()).json()['answer'] == '未检索到资料。'


def test_recovery_and_heartbeat_and_retry(env):
    _, worker, client = env
    for status in worker.ACTIVE:
        add(status, status=status)
    add('completed', pub='published')
    with store.connect() as conn:
        worker.recover(conn)
        for status in worker.ACTIVE:
            assert store.get(conn, status)['job_status'] == 'queued'
        assert store.get(conn, 'completed')['job_status'] == 'ready'
        worker.heartbeat(conn)
    assert client.get('/api/health').json()['worker_alive'] is True
    with store.connect() as conn:
        conn.execute('UPDATE worker_status SET heartbeat=0')
    assert client.get('/api/health').json()['worker_alive'] is False
    add('failure', status='failed')
    assert client.post('/api/versions/failure/retry').status_code == 202
    assert client.post('/api/versions/completed/retry').status_code == 409


def test_partial_insert_retry_uses_new_store_and_frozen_chunks(env, monkeypatch):
    _, worker, _ = env
    add('a', status='parsing')
    worker.WORK.mkdir()
    # 真实 chunking.make_chunks 会输出 chunk_metadata；冻结片段必须同样具备，
    # 否则 worker 的复用前校验会（正确地）判失败。
    chunks = [{'chunk_id': str(i), 'content': 'policy', 'metadata': {},
               'chunk_metadata': {'document_id': 'a', 'chunk_id': str(i)}}
              for i in range(17)]
    (worker.WORK / 'a.chunks.json').write_text(json.dumps(chunks))
    writes, stores = [], []
    def call(c, method, path, body):
        if path == '/vector_stores':
            sid = 'vs' + str(len(stores)); stores.append(sid); return {'id': sid}
        if path == '/embeddings':
            return {'data': [{'index': i, 'embedding': [0.] * 512} for i in range(len(body['input']))]}
        if path == '/vector-io/insert':
            writes.append((body['vector_store_id'], [v['chunk_id'] for v in body['chunks']]))
            if len(writes) == 2: raise RuntimeError('interrupted after partial insert')
            return {}
        raise AssertionError(path)
    monkeypatch.setattr(worker, 'call', call)
    with store.connect() as conn:
        worker.process(conn, store.get(conn, 'a'))
        assert store.get(conn, 'a')['job_status'] == 'failed'
        worker.update(conn, 'a', job_status='parsing')
        worker.process(conn, store.get(conn, 'a'))
        final = store.get(conn, 'a')
        assert final['job_status'] == 'ready' and final['store_id'] == 'vs1' and final['done'] == 17
        assert conn.execute('SELECT COUNT(*) FROM ingestion_attempts').fetchone()[0] == 2
    assert stores == ['vs0', 'vs1']
    assert [cid for sid, ids in writes if sid == 'vs1' for cid in ids] == [str(i) for i in range(17)]


def test_worker_shutdown_requeues_and_restart_finishes(tmp_path):
    """真实父子进程：验证单实例锁、中断回队、重启续跑与新库隔离。

    导入隔离：worker 用 sys.path.append 挂第一阶段目录，脚本自身目录优先，
    因此夹具里的 ogx_client 一定胜出；这里显式断言，并让桩阻断真实网络。
    """
    import subprocess
    import shutil
    import time

    folder = tmp_path / 'runtime'; folder.mkdir()
    data = folder / 'data'; (data / 'work').mkdir(parents=True)
    for name in ('version_worker.py', 'version_store.py'):
        shutil.copyfile(ROOT / name, folder / name)
    (folder / 'settings.py').write_text(
        "EMBED_MODEL='test'\nDIM=512\nMAX_TOKENS=384\nTOKENIZER='test'\n")

    stub_source = '''import os, socket, time
from contextlib import nullcontext
from pathlib import Path

DATA = Path(os.environ['VERSION_DATA_DIR'])
(DATA / 'stub_loaded.txt').write_text(__file__)

def _blocked(*args, **kwargs):
    raise RuntimeError('unmocked network access in test: ' + repr(args[:1]))

socket.socket.connect = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked

client = lambda: nullcontext(None)

def call(c, method, path, body):
    if path == '/vector_stores':
        (DATA / 'started').touch()
        with (DATA / 'stores.txt').open('a') as fh:
            fh.write(body['name'] + '\\n')
        if os.environ.get('SLOW') == '1':
            time.sleep(60)
        return {'id': 'vs-' + body['name']}
    if path == '/embeddings':
        return {'data': [{'index': i, 'embedding': [0.0]*512}
                         for i in range(len(body['input']))]}
    return {}
'''
    (folder / 'ogx_client.py').write_text(stub_source)

    conn = sqlite3.connect(data / 'version.db'); conn.executescript(store.SCHEMA)
    conn.execute("INSERT INTO versions(version_id,policy_id,version_label,scope,valid_from,"
                 "created_at,filename,source_path,sha256) VALUES"
                 "('a','p','v1','all','2026-01-01','now','a.md','unused','hash')")
    conn.commit()
    (data / 'work/a.chunks.json').write_text(json.dumps([{
        'chunk_id': 'one', 'content': 'test', 'metadata': {},
        'chunk_metadata': {'document_id': 'a', 'chunk_id': 'one'},
    }]))

    log_path = data / 'worker.err'

    def wait_until(predicate, label):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        detail = ''
        try:
            detail = log_path.read_text()[-800:]
        except OSError:
            pass
        raise AssertionError('等待 ' + label + ' 超时；worker 日志尾部：' + detail)

    # 用"毒桩"模拟第一阶段目录：只要 worker 把第一阶段路径排到夹具目录之前，
    # 就会导入这里并立刻报错；这样测试与本机是否安装了第一阶段无关。
    phase1 = tmp_path / 'fake-phase1'
    phase1.mkdir()
    (phase1 / 'ogx_client.py').write_text(
        "raise RuntimeError('worker imported phase1 ogx_client; fixture stub must win')\n")
    (phase1 / 'settings.py').write_text(
        "raise RuntimeError('worker imported phase1 settings; fixture stub must win')\n")

    env = {**os.environ,
           'VERSION_DATA_DIR': str(data),
           'RAG_PHASE1_DIR': str(phase1),
           'SLOW': '1'}
    log = log_path.open('wb')
    proc = subprocess.Popen([sys.executable, str(folder / 'version_worker.py')],
                            env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        wait_until(lambda: (data / 'stub_loaded.txt').exists(), '桩模块加载')
        loaded = (data / 'stub_loaded.txt').read_text().strip()
        assert Path(loaded) == folder / 'ogx_client.py', (
            'worker 未加载夹具桩，实际 ' + loaded + '；说明第一阶段真实模块仍被优先导入')

        wait_until(lambda: (data / 'started').exists(), '进入建库')

        duplicate = subprocess.run([sys.executable, str(folder / 'version_worker.py')],
                                   env=env, capture_output=True, timeout=20)
        assert duplicate.returncode != 0, '第二个 Worker 应因单实例锁退出'

        proc.terminate(); proc.wait(timeout=20)
        assert conn.execute("SELECT job_status FROM versions WHERE version_id='a'"
                            ).fetchone()[0] == 'queued'
    finally:
        if proc.poll() is None:
            proc.terminate(); proc.wait(timeout=20)
        log.close()

    env['SLOW'] = '0'
    log2 = log_path.open('ab')
    proc = subprocess.Popen([sys.executable, str(folder / 'version_worker.py')],
                            env=env, stdout=log2, stderr=subprocess.STDOUT)
    try:
        wait_until(lambda: conn.execute(
            "SELECT job_status FROM versions WHERE version_id='a'"
        ).fetchone()[0] == 'ready', '续跑至 ready')

        attempts = conn.execute(
            'SELECT store_id FROM ingestion_attempts ORDER BY id').fetchall()
        assert len(attempts) == 2, '应有 2 次尝试，实际 ' + repr(attempts)
        assert len({a[0] for a in attempts}) == 2, '两次尝试必须用不同库，实际 ' + repr(attempts)

        stores = (data / 'stores.txt').read_text().strip().splitlines()
        assert len(stores) == 2 and len(set(stores)) == 2, '建库记录应两次不同：' + repr(stores)
    finally:
        if proc.poll() is None:
            proc.terminate(); proc.wait(timeout=20)
        log2.close()
        conn.close()


def test_web_entry_and_assets(env):
    _, _, client = env
    response = client.get('/')
    assert response.status_code == 200
    assert 'text/html' in response.headers['content-type']
    assert 'name="valid_from" type="date" required' in response.text
    assert 'name="supersedes"' in response.text
    script = client.get('/ui.js')
    assert script.status_code == 200
    assert 'text/javascript' in script.headers['content-type']
    assert client.get('/version_store.py').status_code == 404


def test_web_form_metadata_round_trip(env):
    _, _, client = env
    fields = {'policy_id': 'it-policy', 'version_label': 'v1',
              'scope': 'all', 'valid_from': '2026-01-01'}
    missing = dict(fields)
    missing.pop('valid_from')
    assert client.post('/api/versions', data=missing,
                       files={'file': ('policy.md', b'policy')}).status_code == 422
    response = client.post('/api/versions', data=fields,
                           files={'file': ('policy.md', b'policy')})
    assert response.status_code == 202
    v = response.json()
    assert v['publication_status'] == 'draft'
    assert v['supersedes'] is None and v['valid_to'] is None
    assert client.post(f"/api/versions/{v['version_id']}/publish").status_code == 409
    listed = client.get('/api/versions').json()['versions']
    assert listed[0]['policy_id'] == fields['policy_id']
    assert listed[0]['valid_from'] == fields['valid_from']
    assert 'source_path' not in listed[0]
