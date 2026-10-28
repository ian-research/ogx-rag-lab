"""版本规则单测：用临时 SQLite，不依赖 GPU/OGX/数据库。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sqlite3
import version_store as store
import version_rules as rules


def fresh():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript(store.SCHEMA)
    return conn


def add(conn, vid, label="v1", scope="all", vf="2026-01-01", vt=None,
        status="ready", pub="draft", policy="it-device-policy", sup=None):
    conn.execute(
        "INSERT INTO versions(version_id,policy_id,version_label,scope,job_status,"
        "publication_status,valid_from,valid_to,supersedes,created_at,store_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'vs_test')",
        (vid, policy, label, scope, status, pub, vf, vt, sup, "2026-01-01T00:00:00"))
    conn.commit()


def test_range_boundaries():
    v = {"valid_from": "2026-01-01", "valid_to": "2026-11-01"}
    assert rules.in_range(v, "2026-01-01")
    assert rules.in_range(v, "2026-10-31")
    assert not rules.in_range(v, "2026-11-01")      # 开区间
    v2 = {"valid_from": "2026-11-01", "valid_to": None}
    assert rules.in_range(v2, "2099-01-01")


def test_publish_requires_ready():
    conn = fresh()
    add(conn, "a", status="parsing")
    ok, reason = rules.check_publishable(conn, store.get(conn, "a"))
    assert not ok and reason.startswith("not_ready")


def test_publish_rejects_overlap_without_supersedes():
    conn = fresh()
    add(conn, "a", pub="published", vf="2026-01-01")
    add(conn, "b", label="v2", vf="2026-11-01")
    ok, reason = rules.check_publishable(conn, store.get(conn, "b"))
    assert not ok and reason.startswith("overlap")


def test_publish_with_supersedes_closes_old():
    conn = fresh()
    add(conn, "a", pub="published", vf="2026-01-01")
    add(conn, "b", label="v2", vf="2026-11-01", sup="a")
    out = rules.publish(conn, "b")
    assert out["result"] == "published"
    old = store.get(conn, "a")
    assert old["valid_to"] == "2026-11-01"


def test_supersedes_must_be_later():
    conn = fresh()
    add(conn, "a", pub="published", vf="2026-11-01")
    add(conn, "b", label="v2", vf="2026-01-01", sup="a")
    try:
        rules.check_publishable(conn, store.get(conn, "b"))
        assert False, "应当报错"
    except rules.RuleError as e:
        assert e.code == "bad_supersedes"


def test_draft_not_in_query():
    conn = fresh()
    add(conn, "a", pub="draft")
    r = rules.resolve(conn, "it-device-policy", "all", "2026-10-27")
    assert r["status"] == "no_data"


def test_failed_not_publishable():
    conn = fresh()
    add(conn, "a", status="failed")
    ok, reason = rules.check_publishable(conn, store.get(conn, "a"))
    assert not ok and reason.startswith("not_ready")


def test_as_of_switches_version():
    conn = fresh()
    add(conn, "a", pub="published", vf="2026-01-01", vt="2026-11-01")
    add(conn, "b", label="v2", pub="published", vf="2026-11-01", sup="a")
    assert rules.resolve(conn, "it-device-policy", "all", "2026-10-27")["versions"][0]["version_id"] == "a"
    assert rules.resolve(conn, "it-device-policy", "all", "2026-11-01")["versions"][0]["version_id"] == "b"
    assert rules.resolve(conn, "it-device-policy", "all", "2026-10-27")["versions"][0]["version_id"] == "a"


def test_missing_scope():
    conn = fresh()
    assert rules.resolve(conn, None, None, "2026-10-27")["status"] == "missing_scope"


def test_cross_policy_requires_selection():
    conn = fresh()
    add(conn, "a", pub="published", policy="p1")
    add(conn, "b", pub="published", policy="p2")
    assert rules.resolve(conn, None, "all", "2026-10-27")["status"] == "needs_selection"


def test_withdraw_removes_from_query():
    conn = fresh()
    add(conn, "a", pub="published")
    rules.withdraw(conn, "a", "test")
    assert rules.resolve(conn, "it-device-policy", "all", "2026-10-27")["status"] == "no_data"


def test_repeat_publish_idempotent():
    conn = fresh()
    add(conn, "a")
    rules.publish(conn, "a")
    assert rules.publish(conn, "a")["result"] == "idempotent"


def test_bad_date_rejected():
    try:
        rules.normalize_date("2026/01/01")
        assert False
    except rules.RuleError as e:
        assert e.code == "bad_date"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); passed += 1; print(f"  ✅ {name}")
            except AssertionError as e:
                failed += 1; print(f"  ❌ {name}: {e}")
            except Exception as e:
                failed += 1; print(f"  ❌ {name}: {type(e).__name__}: {e}")
    print(f"\n通过 {passed} / 失败 {failed}")
    sys.exit(1 if failed else 0)


def test_restore_open_and_finite_end():
    for previous in (None, '2027-01-01'):
        conn = fresh()
        add(conn, 'a', vt=previous); rules.publish(conn, 'a')
        add(conn, 'b', label='v2', vf='2026-11-01', sup='a'); rules.publish(conn, 'b')
        result = rules.withdraw(conn, 'b')
        assert result['restoration']['status'] == 'restored'
        assert store.get(conn, 'a')['valid_to'] == previous
        assert rules.resolve(conn, 'it-device-policy', 'all', '2026-11-01')['versions'][0]['version_id'] == 'a'
        assert [r[0] for r in conn.execute('SELECT action FROM publish_log')] == ['publish','supersede','publish','withdraw','restore']
        assert store.get(conn, 'b')['publication_status'] == 'withdrawn'


def test_restore_blocked_by_successor():
    conn=fresh(); add(conn,'a'); rules.publish(conn,'a')
    add(conn,'b',label='v2',vf='2026-06-01',sup='a');rules.publish(conn,'b')
    add(conn,'c',label='v3',vf='2026-11-01',sup='b');rules.publish(conn,'c')
    assert rules.withdraw(conn,'b')['restoration']['reason']=='overlap'
    assert store.get(conn,'a')['valid_to']=='2026-06-01'
    assert rules.resolve(conn,'it-device-policy','all','2026-11-01')['versions'][0]['version_id']=='c'


def test_legacy_withdraw_never_guesses_null_end():
    conn=fresh();add(conn,'a',pub='published',vt='2026-11-01')
    add(conn,'b',label='v2',pub='published',vf='2026-11-01',sup='a')
    assert rules.withdraw(conn,'b')['restoration']['reason']=='legacy_snapshot_missing'
    assert store.get(conn,'a')['valid_to']=='2026-11-01'


def test_withdrawn_predecessor_is_not_restored():
    conn=fresh();add(conn,'a');rules.publish(conn,'a')
    add(conn,'b',label='v2',vf='2026-11-01',sup='a');rules.publish(conn,'b')
    rules.withdraw(conn,'a')
    assert rules.withdraw(conn,'b')['restoration']['reason']=='predecessor_not_published'


def test_restore_failure_rolls_back_withdrawal():
    conn=fresh();add(conn,'a');rules.publish(conn,'a')
    add(conn,'b',label='v2',vf='2026-11-01',sup='a');rules.publish(conn,'b')
    conn.execute("CREATE TRIGGER reject_restore BEFORE INSERT ON publish_log WHEN NEW.action='restore' BEGIN SELECT RAISE(ABORT,'test'); END")
    import pytest
    with pytest.raises(sqlite3.IntegrityError):rules.withdraw(conn,'b')
    assert store.get(conn,'b')['publication_status']=='published'
    assert store.get(conn,'a')['valid_to']=='2026-11-01'


def test_later_change_prevents_restore_even_when_cutoff_is_same():
    conn=fresh();add(conn,'a');rules.publish(conn,'a')
    add(conn,'b',label='v2',vf='2026-11-01',sup='a');rules.publish(conn,'b')
    store.write_log(conn,'supersede',store.get(conn,'a'),'later change');conn.commit()
    assert rules.withdraw(conn,'b')['restoration']['reason']=='predecessor_changed'
    assert store.get(conn,'a')['valid_to']=='2026-11-01'


def test_republish_takes_new_snapshot_and_clears_withdrawn_time():
    conn=fresh();add(conn,'a');rules.publish(conn,'a')
    add(conn,'b',label='v2',vf='2026-11-01',sup='a');rules.publish(conn,'b')
    rules.withdraw(conn,'b');rules.publish(conn,'b')
    assert store.get(conn,'b')['withdrawn_at'] is None
    assert rules.withdraw(conn,'b')['restoration']['status']=='restored'
    assert store.get(conn,'a')['valid_to'] is None
