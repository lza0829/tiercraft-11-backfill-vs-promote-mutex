from __future__ import annotations

import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect
from app.locks import LOCK_LABELS, recover_stale_locks


SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  biz_key TEXT NOT NULL,
  fixture_file TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  available INTEGER NOT NULL DEFAULT 1,
  unavailable_reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS layer_paste (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  store_name TEXT NOT NULL,
  category TEXT NOT NULL,
  amount REAL NOT NULL,
  qty INTEGER NOT NULL,
  raw_json TEXT NOT NULL,
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS layer_detail (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  store_name TEXT NOT NULL,
  category TEXT NOT NULL,
  amount REAL NOT NULL,
  qty INTEGER NOT NULL,
  clean_flag TEXT NOT NULL,
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS layer_summary (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  amount_sum REAL NOT NULL,
  qty_sum INTEGER NOT NULL,
  line_count INTEGER NOT NULL,
  UNIQUE(domain_id, biz_day, store_code),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS layer_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  step TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  in_count INTEGER NOT NULL DEFAULT 0,
  out_count INTEGER NOT NULL DEFAULT 0,
  drop_count INTEGER NOT NULL DEFAULT 0,
  conflict_holder_type TEXT NOT NULL DEFAULT '',
  conflict_holder_job_id INTEGER,
  conflict_holder_label TEXT NOT NULL DEFAULT '',
  target_biz_day TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  started_at TEXT,
  finished_at TEXT,
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS domain_locks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL UNIQUE,
  lock_type TEXT NOT NULL,
  job_id INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(domain_id) REFERENCES domains(id),
  FOREIGN KEY(job_id) REFERENCES layer_jobs(id)
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  paste_count INTEGER NOT NULL,
  detail_count INTEGER NOT NULL,
  summary_line_sum INTEGER NOT NULL,
  key_mismatch INTEGER NOT NULL,
  amount_drift REAL NOT NULL,
  status TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);
"""


def _columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column_if_missing(conn, table: str, column: str, ddl_type: str) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


def _migrate(conn) -> None:
    """对旧库做幂等补列，保证升级后可启动。"""
    job_cols = {
        "conflict_holder_type": "TEXT NOT NULL DEFAULT ''",
        "conflict_holder_job_id": "INTEGER",
        "conflict_holder_label": "TEXT NOT NULL DEFAULT ''",
        "target_biz_day": "TEXT NOT NULL DEFAULT ''",
        "started_at": "TEXT",
        "finished_at": "TEXT",
    }
    for col, ddl in job_cols.items():
        _add_column_if_missing(conn, "layer_jobs", col, ddl)
    _add_column_if_missing(conn, "domains", "available", "INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(conn, "domains", "unavailable_reason", "TEXT NOT NULL DEFAULT ''")
    # 旧库里历史任务状态保持原值，但需要能被列表正常展示
    conn.execute(
        "UPDATE layer_jobs SET finished_at=COALESCE(finished_at, created_at) "
        "WHERE status IN ('成功', '失败') AND finished_at IS NULL"
    )


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        row = conn.execute("SELECT COUNT(*) AS c FROM domains").fetchone()
        if int(row["c"]) == 0:
            _seed_domain(conn)
        # 启动时回收进程中断留下的残留在途锁
        recover_stale_locks(conn)
        conn.commit()
    finally:
        conn.close()


def _seed_domain(conn) -> None:
    fixture = "门店日销售-贴源.jsonl"
    cur = conn.execute(
        """
        INSERT INTO domains(name, biz_key, fixture_file, description)
        VALUES (?, ?, ?, ?)
        """,
        (
            "门店日销售",
            "业务日+门店编码",
            fixture,
            "演示贴源装载、明细清洗与按门店日汇总",
        ),
    )
    domain_id = int(cur.lastrowid)
    path = FIXTURES_DIR / fixture
    if not path.exists():
        # 仓库内相对路径兜底
        alt = Path(__file__).resolve().parents[2] / "fixtures" / fixture
        path = alt
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        conn.execute(
            """
            INSERT INTO layer_paste(
              domain_id, biz_day, store_code, store_name, category, amount, qty, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain_id,
                obj.get("业务日") or "",
                obj.get("门店编码") or "",
                obj.get("门店名称") or "",
                obj.get("品类") or "",
                float(obj.get("金额") or 0),
                int(obj.get("件数") or 0),
                json.dumps(obj, ensure_ascii=False),
            ),
        )
