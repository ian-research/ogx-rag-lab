"""冻结片段校验与版本保护回归。

要点：
- 校验发生在创建向量库与调用 Embedding 之前；
- 非法缓存判 failed，不修补字段、不改 chunk_id；
- ready / 已发布版本不因重试或校验失败被改动。
"""
import importlib
import json
import sys
import types
from contextlib import nullcontext
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import version_store as store
import version_rules as rules


class Recorder:
    """记录对 OGX 的调用，用于断言"校验失败前没有建库/没有 Embedding"。"""

    def __init__(self):
        self.calls = []

    def client(self):
        return nullcontext(None)

    def call(self, _conn, method, path, body=None):
        self.calls.append((method, path))
        if path == '/vector_stores':
            return {'id': 'vs-test'}
        if path == '/embeddings':
            count = len(body['input'])
            return {'data': [{'index': i, 'embedding': [0.0] * 512} for i in range(count)]}
        return {}

    def paths(self):
        return [p for _m, p in self.calls]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(store, 'DB', tmp_path / 'version.db')
    store.init()
    rec = Recorder()
    monkeypatch.setitem(sys.modules, 'ogx_client',
                        types.SimpleNamespace(client=rec.client, call=rec.call))
    monkeypatch.setitem(sys.modules, 'settings',
                        types.SimpleNamespace(LLM='test', EMBED_MODEL='embed',
                                              DIM=512, MAX_TOKENS=384, TOKENIZER='test'))
    worker = importlib.import_module('version_worker')
    monkeypatch.setattr(worker, 'DATA', tmp_path)
    monkeypatch.setattr(worker, 'WORK', tmp_path / 'work')
    monkeypatch.setattr(worker, 'ARTIFACTS', tmp_path / 'artifacts')
    monkeypatch.setattr(worker, 'client', rec.client)
    monkeypatch.setattr(worker, 'call', rec.call)
    (tmp_path / 'work').mkdir(parents=True, exist_ok=True)
    return worker, rec, tmp_path


def add_version(vid='a', status='ready', pub='draft', policy='p'):
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO versions(version_id,policy_id,version_label,scope,job_status,"
            "publication_status,valid_from,created_at,store_id,filename,source_path,sha256) "
            "VALUES(?,?,?,'all',?,?,'2026-01-01','now','vs-old','a.md','unused','hash')",
            (vid, policy, vid, status, pub))
        conn.commit()


def status_of(vid):
    with store.connect() as conn:
        return store.get(conn, vid)['job_status']


def freeze(tmp_path, vid, payload):
    target = tmp_path / 'work' / f'{vid}.chunks.json'
    if isinstance(payload, str):
        target.write_text(payload, encoding='utf-8')
    else:
        target.write_text(json.dumps(payload), encoding='utf-8')
    return target


VALID_CHUNK = {
    'chunk_id': 'c1',
    'content': '员工电脑使用满4年可以申请换新。',
    'metadata': {'block': 1},
    'chunk_metadata': {'document_id': 'a', 'chunk_id': 'c1'},
}


# ---------------- 校验函数单测 ----------------

def test_validate_accepts_valid_chunks(env):
    worker, _rec, _tmp = env
    assert worker.validate_chunks([VALID_CHUNK]) == [VALID_CHUNK]


def test_validate_rejects_missing_chunk_metadata(env):
    worker, _rec, _tmp = env
    broken = {k: v for k, v in VALID_CHUNK.items() if k != 'chunk_metadata'}
    with pytest.raises(worker.ChunkValidationError) as exc:
        worker.validate_chunks([broken])
    assert 'chunk_metadata' in str(exc.value)


@pytest.mark.parametrize('field', ['chunk_id', 'content', 'chunk_metadata'])
def test_validate_rejects_each_missing_required_field(env, field):
    worker, _rec, _tmp = env
    broken = {k: v for k, v in VALID_CHUNK.items() if k != field}
    with pytest.raises(worker.ChunkValidationError):
        worker.validate_chunks([broken])


@pytest.mark.parametrize('patch,label', [
    ({'chunk_metadata': 'not-a-dict'}, 'chunk_metadata 非对象'),
    ({'chunk_metadata': []}, 'chunk_metadata 是列表'),
    ({'chunk_id': ''}, 'chunk_id 空串'),
    ({'chunk_id': 123}, 'chunk_id 非字符串'),
    ({'content': None}, 'content 为 None'),
    ({'content': ''}, 'content 空串'),
])
def test_validate_rejects_wrong_types(env, patch, label):
    worker, _rec, _tmp = env
    broken = {**VALID_CHUNK, **patch}
    with pytest.raises(worker.ChunkValidationError):
        worker.validate_chunks([broken])


def test_validate_rejects_non_list_and_empty(env):
    worker, _rec, _tmp = env
    with pytest.raises(worker.ChunkValidationError):
        worker.validate_chunks([])
    with pytest.raises(worker.ChunkValidationError):
        worker.validate_chunks({'chunk_id': 'c1'})
    with pytest.raises(worker.ChunkValidationError):
        worker.validate_chunks('[]')
    with pytest.raises(worker.ChunkValidationError):
        worker.validate_chunks([VALID_CHUNK, 'not-a-dict'])


def test_load_frozen_rejects_corrupt_json(env):
    worker, _rec, tmp = env
    bad = freeze(tmp, 'a', '{"chunk_id": "c1", ')
    with pytest.raises(worker.ChunkValidationError) as exc:
        worker.load_frozen_chunks(bad)
    assert 'JSON' in str(exc.value)


def test_load_frozen_reads_valid_file(env):
    worker, _rec, tmp = env
    good = freeze(tmp, 'a', [VALID_CHUNK])
    assert worker.load_frozen_chunks(good) == [VALID_CHUNK]


# ---------------- 集成：非法缓存必须在建库前失败 ----------------

@pytest.mark.parametrize('payload,label', [
    ('[]', '空列表'),
    ('not json at all', '损坏 JSON'),
    (json.dumps([{k: v for k, v in VALID_CHUNK.items() if k != 'chunk_metadata'}]), '缺 chunk_metadata'),
    (json.dumps([{**VALID_CHUNK, 'chunk_metadata': 'x'}]), 'chunk_metadata 类型错误'),
    (json.dumps({'chunk_id': 'c1'}), '顶层不是列表'),
])
def test_invalid_frozen_cache_fails_without_touching_ogx(env, payload, label):
    worker, rec, tmp = env
    add_version('a', status='queued')
    freeze(tmp, 'a', payload)

    version = None
    with store.connect() as conn:
        version = store.get(conn, 'a')
    worker.process(conn_holder := store.connect(), version)
    conn_holder.close()

    assert status_of('a') == 'failed', f'{label} 应当判 failed'
    assert rec.paths() == [], f'{label} 不得在建库/Embedding 前调用 OGX，实际 {rec.paths()}'
    with store.connect() as conn:
        assert conn.execute(
            'SELECT COUNT(*) FROM ingestion_attempts').fetchone()[0] == 0


def test_invalid_cache_does_not_rewrite_chunk_ids(env):
    worker, _rec, tmp = env
    add_version('a', status='queued')
    payload = [{k: v for k, v in VALID_CHUNK.items() if k != 'chunk_metadata'}]
    target = freeze(tmp, 'a', payload)
    before = target.read_text(encoding='utf-8')

    with store.connect() as conn:
        worker.process(conn, store.get(conn, 'a'))

    assert status_of('a') == 'failed'
    # 缓存文件不被改写：不补字段、不改 chunk_id
    assert target.read_text(encoding='utf-8') == before


# ---------------- 合法缓存恢复 ----------------

def test_valid_frozen_cache_resumes_to_ready(env):
    worker, rec, tmp = env
    add_version('a', status='queued')
    freeze(tmp, 'a', [VALID_CHUNK])

    with store.connect() as conn:
        worker.process(conn, store.get(conn, 'a'))

    assert status_of('a') == 'ready'
    assert '/vector_stores' in rec.paths()
    assert '/embeddings' in rec.paths()
    assert '/vector-io/insert' in rec.paths()


# ---------------- 版本保护 ----------------

def test_validation_failure_keeps_published_version_intact(env):
    worker, rec, tmp = env
    add_version('old', status='ready', pub='published')
    with store.connect() as conn:
        rules.publish(conn, 'old')
    add_version('new', status='queued')
    freeze(tmp, 'new', '[]')

    with store.connect() as conn:
        worker.process(conn, store.get(conn, 'new'))

    assert status_of('new') == 'failed'
    with store.connect() as conn:
        old = store.get(conn, 'old')
        assert old['publication_status'] == 'published'
        assert old['job_status'] == 'ready'
        assert old['valid_to'] is None
        # 旧版本仍可被正常检索
        assert rules.resolve(conn, 'p', 'all', '2026-06-01')['status'] == 'ok'


def test_failed_draft_retry_goes_back_to_ready(env):
    worker, _rec, tmp = env
    add_version('a', status='failed')
    freeze(tmp, 'a', [VALID_CHUNK])

    # 模拟人工重试：把 failed 重新排队
    with store.connect() as conn:
        conn.execute("UPDATE versions SET job_status='queued' WHERE version_id='a'")
        conn.commit()
        worker.process(conn, store.get(conn, 'a'))

    assert status_of('a') == 'ready'
    with store.connect() as conn:
        assert store.get(conn, 'a')['publication_status'] == 'draft'


def test_ready_version_rerun_does_not_change_publication_state(env):
    worker, _rec, tmp = env
    add_version('a', status='ready', pub='published')
    freeze(tmp, 'a', [VALID_CHUNK])          # 冻结片段存在，避免走解析分支
    with store.connect() as conn:
        rules.publish(conn, 'a')
        worker.process(conn, store.get(conn, 'a'))

    assert status_of('a') == 'ready'
    with store.connect() as conn:
        assert store.get(conn, 'a')['publication_status'] == 'published'
