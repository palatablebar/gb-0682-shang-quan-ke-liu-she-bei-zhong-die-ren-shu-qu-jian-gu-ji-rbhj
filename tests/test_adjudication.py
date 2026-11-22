import pytest
from app.errors import DomainError
from app.workflow import run, load
from app import store
from app.modules.m5_calculate.adjudication import candidates, _retract_bounds
from app.modules.m5_calculate.rules import apply
from app.modules.m6_reconcile.verification import recheck, build_promotion_record, apply_promotions

CONTRA = {'zones': {'A': 100, 'B': 100, 'C': 100},
          'pairs': {'A|B': 80, 'A|C': 80, 'B|C': 0}, 'triple': None}


def test_retract_each_pair_alternative_bounds():
    # 撤下 B|C（保留两条 80，pivot=A）：T∈[60,80]，U=140+T∈[200,220]
    alt = _retract_bounds(CONTRA['zones'], CONTRA['pairs'], 'B|C')
    assert (alt['lower'], alt['upper']) == (200, 220)
    # 撤下 A|B：B|C=0 迫使 T=0，联合 220（单点）
    alt = _retract_bounds(CONTRA['zones'], CONTRA['pairs'], 'A|B')
    assert (alt['lower'], alt['upper']) == (220, 220)
    # 撤下 A|C 同理 220
    alt = _retract_bounds(CONTRA['zones'], CONTRA['pairs'], 'A|C')
    assert (alt['lower'], alt['upper']) == (220, 220)


def test_envelope_is_conservative_not_false_precision():
    cand = candidates(CONTRA)
    assert cand['envelope_lower'] == 200 and cand['envelope_upper'] == 220
    # 三条替代区间都被包络覆盖，且包络保留存疑宽度，不退化成单点
    for alt in cand['alternatives']:
        assert cand['envelope_lower'] <= alt['lower'] <= alt['upper'] <= cand['envelope_upper']
    assert cand['uncertainty_width'] == 20


def test_dirty_sample_produces_one_adjudication(raw):
    s = run(raw, {})
    assert len(s['adjudications']) == 1
    a = s['adjudications'][0]
    assert a['status'] == '待复核' and a['code'] == 'CANDIDATE'
    assert (a['envelope_lower'], a['envelope_upper']) == (200, 220)
    assert len(a['alternatives']) == 3 and {x['dropped'] for x in a['alternatives']} == {'A|B', 'A|C', 'B|C'}


def test_contradiction_bucket_still_quarantined_with_evidence(raw):
    # 整桶隔离不解除：六条原始观测（含三条交集）全部保留原因码与原值
    s = run(raw, {})
    bucket = s['adjudications'][0]['bucket']
    qs = [q for q in s['quarantine'] if q['code'] == 'CONSTRAINT']
    assert len(qs) == 6 and all(q['raw'] for q in qs)
    ev = s['adjudications'][0]['evidence']
    assert len(ev) == 3 and all(e['line'] and e['id'] and e['raw'] for e in ev)
    assert all(r['bucket'] != bucket for r in s['rows'])


def test_candidate_excluded_from_official_totals_until_confirmed(raw):
    s = run(raw, {})
    # 未确认前正式日汇只含 12 个可估界桶，矛盾桶不贡献客流
    assert s['business']['buckets'] == 12
    assert 'business_official' not in s or s.get('business_official') is None
    assert s['reconcile']['pending_adjudication_count'] == 1


def test_recheck_passes_on_untouched_record(raw):
    s = run(raw, {})
    adj = s['adjudications'][0]
    ok, fingerprint, cand = recheck(adj)
    assert ok and adj['fingerprint'] == fingerprint


def test_recheck_fails_when_evidence_tampered(raw):
    s = run(raw, {})
    adj = dict(s['adjudications'][0])
    adj['pairs'] = dict(adj['pairs']); adj['pairs']['B|C'] = 5  # 篡改原证据
    ok, _, _ = recheck(adj)
    assert ok is False


def test_source_recheck_rejects_self_consistent_tamper(client, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    from app.modules.m6_reconcile.verification import derive_fingerprint, rederive_from_source
    posted = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()
    run_id = posted['run_id']; bucket = posted['adjudications'][0]['bucket']
    # 篡改后重算指纹使其“自洽”，但与批次原始 CSV 推导出的证据不符
    import sqlite3, json as _json, os
    with sqlite3.connect(os.environ['DB_PATH']) as db:
        row = db.execute('SELECT body FROM adjudications WHERE run_id=? AND bucket=?', (run_id, bucket)).fetchone()
        tampered = _json.loads(row[0])
        tampered['zones'] = dict(tampered['zones']); tampered['zones']['A'] = 130
        fp, cand = derive_fingerprint({'zones': tampered['zones'], 'pairs': tampered['pairs'], 'triple': None})
        tampered['fingerprint'] = fp
        tampered['envelope_lower'] = cand['envelope_lower']; tampered['envelope_upper'] = cand['envelope_upper']
        db.execute('UPDATE adjudications SET body=? WHERE run_id=? AND bucket=?',
                   (_json.dumps(tampered, ensure_ascii=False), run_id, bucket))
    state = client.get(f'/api/runs/{run_id}').get_json()
    fresh = rederive_from_source(state)
    ok, _, _ = recheck(tampered, fresh)
    assert ok is False
    r = client.post(f'/api/runs/{run_id}/adjudication/{bucket}/confirm', json={'version': 1})
    assert r.status_code == 409 and r.get_json()['code'] == 'RECHECK_FAILED'


def test_confirm_endpoint_happy_path_promotes_to_official(client):
    import json
    posted = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()
    run_id = posted['run_id']; bucket = posted['adjudications'][0]['bucket']
    r = client.post(f'/api/runs/{run_id}/adjudication/{bucket}/confirm', json={'version': 1})
    assert r.status_code == 200
    body = r.get_json()
    assert body['review_version'] == 2 and body['decision'] == '补正确认'
    assert body['promotions'][bucket]['status'] == '已确认'
    official = body['reconcile']['business_official']
    # 正式日汇 = 原 12 桶 + 候选包络 200–220
    assert official['total'] == posted['business']['total'] + 200
    assert official['upper_total'] == posted['business']['upper_total'] + 220
    assert official['corrected_buckets'] == 1
    # 原始隔离与裁决记录仍然存在，证据未丢失
    assert any(q['code'] == 'CONSTRAINT' for q in body['quarantine'])
    assert body['reconcile']['pending_adjudication_count'] == 0


def test_retry_is_idempotent_and_creates_no_new_records(client):
    posted = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()
    run_id = posted['run_id']; bucket = posted['adjudications'][0]['bucket']
    first = client.post(f'/api/runs/{run_id}/adjudication/{bucket}/confirm', json={'version': 1}).get_json()
    # 用新版本重试：不新增裁决记录、不再递增版本
    second = client.post(f'/api/runs/{run_id}/adjudication/{bucket}/confirm', json={'version': 2})
    assert second.status_code == 200
    assert second.get_json()['review_version'] == 2
    # 用陈旧版本号重试已确认桶：幂等返回，同样不新增版本或记录
    stale = client.post(f'/api/runs/{run_id}/adjudication/{bucket}/confirm', json={'version': 1})
    assert stale.status_code == 200 and stale.get_json()['review_version'] == 2
    full = client.get(f'/api/runs/{run_id}').get_json()
    assert len(full['adjudications']) == 1 and len(full['promotions']) == 1


def test_stale_version_rejected_and_old_version_kept(client):
    posted = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()
    run_id = posted['run_id']; bucket = posted['adjudications'][0]['bucket']
    r = client.post(f'/api/runs/{run_id}/adjudication/{bucket}/confirm', json={'version': 99})
    assert r.status_code == 409 and r.get_json()['code'] == 'STALE'
    full = client.get(f'/api/runs/{run_id}').get_json()
    assert full['review_version'] == 1 and full['promotions'] == {}


def test_recheck_failure_blocks_promotion_and_keeps_old(client, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    posted = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()
    run_id = posted['run_id']; bucket = posted['adjudications'][0]['bucket']
    # 直接篡改存储中的裁决证据，模拟补正重试时证据已对不上
    import sqlite3, json as _json, os
    db_path = os.environ['DB_PATH']
    with sqlite3.connect(db_path) as db:
        row = db.execute('SELECT body FROM adjudications WHERE run_id=? AND bucket=?', (run_id, bucket)).fetchone()
        tampered = _json.loads(row[0]); tampered['pairs']['B|C'] = 9
        db.execute('UPDATE adjudications SET body=? WHERE run_id=? AND bucket=?',
                   (_json.dumps(tampered, ensure_ascii=False), run_id, bucket))
    r = client.post(f'/api/runs/{run_id}/adjudication/{bucket}/confirm', json={'version': 1})
    assert r.status_code == 409 and r.get_json()['code'] == 'RECHECK_FAILED'
    full = client.get(f'/api/runs/{run_id}').get_json()
    # 失败切换：版本、正式日汇、补正表全部保持旧版
    assert full['review_version'] == 1 and full['promotions'] == {}
    assert full['reconcile'].get('business_official') is None


def test_unknown_bucket_returns_404(client):
    posted = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()
    r = client.post(f"/api/runs/{posted['run_id']}/adjudication/nope/confirm", json={'version': 1})
    assert r.status_code == 404 and r.get_json()['code'] == 'NOT_FOUND'


def test_export_contains_adjudication_and_promotion_rows(client):
    import csv, io
    posted = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()
    run_id = posted['run_id']; bucket = posted['adjudications'][0]['bucket']
    client.post(f'/api/runs/{run_id}/adjudication/{bucket}/confirm', json={'version': 1})
    text = client.get(f'/api/archive/{run_id}/export.csv').data.decode('utf-8-sig')
    assert '候选裁决' in text and '补正确认' in text and 'PROMOTED' in text
    rows = list(csv.reader(io.StringIO(text)))
    kinds = [r[0] for r in rows]
    assert kinds.count('候选裁决') == 1 and kinds.count('补正确认') == 1
    # 隔离行未因补正而消失
    assert kinds.count('隔离') == len(posted['quarantine'])


def test_reconcile_balance_holds_with_adjudication(state):
    r = state['reconcile']
    assert r['balanced']
    assert r['input'] == r['accepted'] + r['isolated'] + r['blank'] + r['headers'] + r['duplicates']


def test_apply_promotions_keeps_raw_business_unchanged(raw):
    s = run(raw, {})
    adj = s['adjudications'][0]
    _, fp, _ = recheck(adj)
    record = build_promotion_record(adj, fp)
    merged = apply_promotions(run(raw, {}), {adj['bucket']: record})
    # 原始 business 不被改写，正式口径单独承载
    assert merged['business']['buckets'] == 12
    assert merged['business_official']['buckets'] == 13
