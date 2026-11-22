import sqlite3,json,os
from pathlib import Path
from app.errors import DomainError

def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    # 裁决记录按 (批次, 桶) 唯一：补正重试只命中同一行，不新增裁决记录。
    db.execute('CREATE TABLE IF NOT EXISTS adjudications (run_id TEXT NOT NULL, bucket TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(run_id,bucket))')
    # 正式日汇补正确认，与 runs 的乐观版本联动；失败时不写入，旧版保持不变。
    db.execute('CREATE TABLE IF NOT EXISTS promotions (run_id TEXT NOT NULL, bucket TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(run_id,bucket))')
    return db

def save(run_id, body):
    with connection() as db:
        db.execute('INSERT OR IGNORE INTO runs(id,body) VALUES (?,?)',(run_id,json.dumps(body,ensure_ascii=False)))
        # 裁决记录与原证据随批次持久化；INSERT OR IGNORE 保证重试不新增、不覆盖。
        for adj in body.get('adjudications',[]):
            db.execute('INSERT OR IGNORE INTO adjudications(run_id,bucket,body) VALUES (?,?,?)',
                (run_id,adj['bucket'],json.dumps(adj,ensure_ascii=False)))
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
    return get(run_id)

def _adjudications(db, run_id):
    return {r['bucket']:json.loads(r['body']) for r in
        db.execute('SELECT bucket,body FROM adjudications WHERE run_id=?',(run_id,)).fetchall()}

def _promotions(db, run_id):
    return {r['bucket']:json.loads(r['body']) for r in
        db.execute('SELECT bucket,body FROM promotions WHERE run_id=?',(run_id,)).fetchall()}

def get(run_id):
    with connection() as db:r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    body=json.loads(r['body']);body.update(run_id=r['id'],review_version=r['version'],decision=r['decision'])
    with connection() as db:
        body['adjudications']=list(_adjudications(db,run_id).values())
        promotions=_promotions(db,run_id)
        body['promotions']=promotions
    # 已确认补正在读取时即并入正式日汇视图；原始 business 与隔离保持不变。
    if promotions:
        from app.modules.m6_reconcile.verification import apply_promotions
        from app.modules.m6_reconcile.compare import balance
        body=apply_promotions(body,promotions)
        body['reconcile']=balance(body)
    return body

def history():
    with connection() as db:rows=db.execute("SELECT id,version,decision FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]

def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:
            if not db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():raise DomainError('NOT_FOUND','批次不存在',404)
            raise DomainError('STALE','复核版本已更新，请刷新',409)
    return get(run_id)

def adjudication(run_id, bucket):
    with connection() as db:
        r=db.execute('SELECT body FROM adjudications WHERE run_id=? AND bucket=?',(run_id,bucket)).fetchone()
    if not r:raise DomainError('NOT_FOUND','该桶不存在候选修正裁决',404)
    return json.loads(r['body'])

def promote(run_id, bucket, version, record):
    """事务化补正确认：版本陈旧或核对失败时整体回退，旧版与旧补正保持不变。

    重试同一 (批次, 桶) 只 UPSERT 同一行并复用既有裁决，不新增裁决记录；
    runs.version 仅在首次确认时递增，重复重试不制造新版本。
    已确认桶的重试是幂等操作：重新核对通过即返回当前状态，不触发版本锁。
    """
    with connection() as db:
        row=db.execute('SELECT version FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row:raise DomainError('NOT_FOUND','批次不存在',404)
        exists=db.execute('SELECT 1 FROM promotions WHERE run_id=? AND bucket=?',(run_id,bucket)).fetchone()
        if not exists and row['version']!=version:
            raise DomainError('STALE','复核版本已更新，请刷新后重新核对',409)
        db.execute('INSERT INTO promotions(run_id,bucket,body) VALUES (?,?,?) ON CONFLICT(run_id,bucket) DO UPDATE SET body=excluded.body',
            (run_id,bucket,json.dumps(record,ensure_ascii=False)))
        if not exists:
            db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=?',('补正确认',run_id))
    return get(run_id)
