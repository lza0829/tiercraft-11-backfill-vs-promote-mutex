"""主题域在途锁。

互斥规则（同一主题域内）：
- 回刷在途时，禁止启动任一类提升（贴源到明细 / 明细到汇总）；
- 任一提升在途时，禁止启动回刷，也禁止启动另一类提升。

锁行持久化在 domain_locks 表中，是可查询的在途事实，不依赖进程内状态，
刷新页面或重启后仍然可查（重启时由 seed.recover_stale_locks 回收残留锁）。
"""
from __future__ import annotations

import sqlite3

LOCK_PASTE_TO_DETAIL = "paste_to_detail"
LOCK_DETAIL_TO_SUMMARY = "detail_to_summary"
LOCK_BACKFILL = "backfill"

LOCK_LABELS = {
    LOCK_PASTE_TO_DETAIL: "提升·贴源到明细",
    LOCK_DETAIL_TO_SUMMARY: "提升·明细到汇总",
    LOCK_BACKFILL: "定点回刷",
}


class LockConflict(Exception):
    """抢锁失败；message 必须写明对端在途类型与对端任务编号。"""

    def __init__(self, message: str, holder_type: str, holder_job_id: int):
        super().__init__(message)
        self.message = message
        self.holder_type = holder_type
        self.holder_job_id = holder_job_id


def conflict_message(domain_name: str, holder_type: str, job_id: int, request_type: str) -> str:
    holder_label = LOCK_LABELS.get(holder_type, holder_type)
    request_label = LOCK_LABELS.get(request_type, request_type)
    return (
        f"主题域「{domain_name}」存在在途任务（{holder_label}，任务#{job_id}），"
        f"禁止启动{request_label}；本次请求已拒绝并记录为互斥失败"
    )


def find_lock(conn: sqlite3.Connection, domain_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM domain_locks WHERE domain_id=?", (domain_id,)
    ).fetchone()


def acquire_lock(
    conn: sqlite3.Connection,
    domain_id: int,
    lock_type: str,
    job_id: int,
    domain_name: str,
) -> None:
    """在已开启的写事务内抢锁；抢到则插入锁行，否则抛 LockConflict。"""
    holder = find_lock(conn, domain_id)
    if holder is not None:
        raise LockConflict(
            conflict_message(
                domain_name, holder["lock_type"], int(holder["job_id"]), lock_type
            ),
            holder["lock_type"],
            int(holder["job_id"]),
        )
    try:
        conn.execute(
            "INSERT INTO domain_locks(domain_id, lock_type, job_id) VALUES (?, ?, ?)",
            (domain_id, lock_type, job_id),
        )
    except sqlite3.IntegrityError as exc:
        holder = find_lock(conn, domain_id)
        if holder is not None:
            raise LockConflict(
                conflict_message(
                    domain_name,
                    holder["lock_type"],
                    int(holder["job_id"]),
                    lock_type,
                ),
                holder["lock_type"],
                int(holder["job_id"]),
            ) from exc
        raise


def release_lock(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute("DELETE FROM domain_locks WHERE job_id=?", (job_id,))


def list_locks(conn: sqlite3.Connection, domain_id: int | None = None) -> list[dict]:
    sql = (
        "SELECT l.*, d.name AS domain_name, j.status AS job_status "
        "FROM domain_locks l "
        "JOIN domains d ON d.id = l.domain_id "
        "JOIN layer_jobs j ON j.id = l.job_id"
    )
    args: tuple = ()
    if domain_id is not None:
        sql += " WHERE l.domain_id=?"
        args = (domain_id,)
    sql += " ORDER BY l.id"
    out = []
    for r in conn.execute(sql, args).fetchall():
        d = dict(r)
        d["lock_type_label"] = LOCK_LABELS.get(d["lock_type"], d["lock_type"])
        out.append(d)
    return out


def recover_stale_locks(conn: sqlite3.Connection) -> int:
    """进程重启后回收残留在途锁：对应任务判失败，主题域置不可用待重跑。"""
    stale = conn.execute(
        """
        SELECT l.job_id, l.domain_id, l.lock_type
        FROM domain_locks l
        JOIN layer_jobs j ON j.id = l.job_id
        WHERE j.status = '在途'
        """
    ).fetchall()
    for row in stale:
        job_id = int(row["job_id"])
        domain_id = int(row["domain_id"])
        label = LOCK_LABELS.get(row["lock_type"], row["lock_type"])
        reason = f"进程中断，{label}（任务#{job_id}）未正常结束，在途锁已回收，主题域标记不可用待重跑"
        conn.execute(
            "UPDATE layer_jobs SET status='失败', message=?, finished_at=datetime('now') WHERE id=?",
            (reason, job_id),
        )
        conn.execute("DELETE FROM domain_locks WHERE job_id=?", (job_id,))
        conn.execute(
            "UPDATE domains SET available=0, unavailable_reason=? WHERE id=?",
            (reason, domain_id),
        )
    return len(stale)
