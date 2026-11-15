from __future__ import annotations

import json
import re

from app import locks
from app.db import connect, rows_to_dicts
from app.locks import (
    LOCK_BACKFILL,
    LOCK_DETAIL_TO_SUMMARY,
    LOCK_LABELS,
    LOCK_PASTE_TO_DETAIL,
    LockConflict,
)

STEP_LABEL = {
    "paste_to_detail": "贴源到明细",
    "detail_to_summary": "明细到汇总",
    "backfill": "定点回刷",
}

_BIZ_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# 测试缝：在数据事务内、对应分层写入完成后调用。默认空操作；
# 测试可 monkeypatch 成抛异常（模拟失败）或 sleep（模拟长任务持锁）。
# ---------------------------------------------------------------------------
def detail_phase_hook(conn, domain_id: int, biz_day: str) -> None:
    return None


def summary_phase_hook(conn, domain_id: int, biz_day: str) -> None:
    return None


def list_domains() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM domains ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            lock = locks.find_lock(conn, d["id"])
            if lock is not None:
                d["in_flight_lock"] = LOCK_LABELS.get(lock["lock_type"], lock["lock_type"])
                d["in_flight_job_id"] = int(lock["job_id"])
            else:
                d["in_flight_lock"] = ""
                d["in_flight_job_id"] = None
            d["alignment"] = _domain_alignment(conn, d["id"])
            out.append(d)
        return out
    finally:
        conn.close()


def get_domain(domain_id: int) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM domains WHERE id=?", (domain_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _domain_alignment(conn, domain_id: int) -> str:
    """基于已提交数据实时计算对齐状态，不允许把未对齐展示成已对齐。"""
    detail_count = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM layer_detail WHERE domain_id=?", (domain_id,)
        ).fetchone()["c"]
    )
    summary = conn.execute(
        "SELECT COALESCE(SUM(line_count),0) AS s, COALESCE(SUM(amount_sum),0) AS a "
        "FROM layer_summary WHERE domain_id=?",
        (domain_id,),
    ).fetchone()
    detail_amount = float(
        conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS a FROM layer_detail WHERE domain_id=?",
            (domain_id,),
        ).fetchone()["a"]
    )
    aligned = (
        int(summary["s"]) == detail_count
        and abs(float(summary["a"]) - detail_amount) < 0.0001
    )
    return "已对齐" if aligned else "未对齐"


def layer_rows(table: str, domain_id: int | None = None) -> list[dict]:
    allowed = {
        "paste": "layer_paste",
        "detail": "layer_detail",
        "summary": "layer_summary",
    }
    if table not in allowed:
        raise ValueError("未知分层")
    sql = f"SELECT * FROM {allowed[table]}"
    args: tuple = ()
    if domain_id is not None:
        sql += " WHERE domain_id=?"
        args = (domain_id,)
    sql += " ORDER BY id"
    conn = connect()
    try:
        return rows_to_dicts(conn.execute(sql, args).fetchall())
    finally:
        conn.close()


def layer_counts(domain_id: int | None = None) -> dict:
    conn = connect()
    try:
        where = " WHERE domain_id=?" if domain_id is not None else ""
        args = (domain_id,) if domain_id is not None else ()
        paste = conn.execute(f"SELECT COUNT(*) AS c FROM layer_paste{where}", args).fetchone()["c"]
        detail = conn.execute(f"SELECT COUNT(*) AS c FROM layer_detail{where}", args).fetchone()["c"]
        summary = conn.execute(f"SELECT COUNT(*) AS c FROM layer_summary{where}", args).fetchone()["c"]
        return {
            "贴源层": int(paste),
            "明细层": int(detail),
            "汇总层": int(summary),
        }
    finally:
        conn.close()


def list_locks(domain_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        return locks.list_locks(conn, domain_id)
    finally:
        conn.close()


def _decorate_job(d: dict) -> dict:
    d["step_label"] = STEP_LABEL.get(d["step"], d["step"])
    d["in_flight"] = d["status"] == "在途"
    if d.get("conflict_holder_type") and not d.get("conflict_holder_label"):
        d["conflict_holder_label"] = LOCK_LABELS.get(
            d["conflict_holder_type"], d["conflict_holder_type"]
        )
    return d


def get_job(job_id: int) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM layer_jobs WHERE id=?", (job_id,)).fetchone()
        return _decorate_job(dict(row)) if row else None
    finally:
        conn.close()


def list_jobs(domain_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        if domain_id is None:
            rows = conn.execute("SELECT * FROM layer_jobs ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM layer_jobs WHERE domain_id=? ORDER BY id DESC", (domain_id,)
            ).fetchall()
        return [_decorate_job(dict(r)) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 在途锁与任务生命周期
# ---------------------------------------------------------------------------
def _precheck_lock(domain_id: int, lock_type: str) -> None:
    """只读预检：让并发冲突在写事务外快速失败，避免被长任务整段阻塞。"""
    conn = connect()
    try:
        holder = locks.find_lock(conn, domain_id)
    finally:
        conn.close()
    if holder is not None:
        domain = get_domain(domain_id)
        name = domain["name"] if domain else str(domain_id)
        raise LockConflict(
            locks.conflict_message(
                name, holder["lock_type"], int(holder["job_id"]), lock_type
            ),
            holder["lock_type"],
            int(holder["job_id"]),
        )


def _start_job(domain_id: int, step: str, lock_type: str, biz_day: str = ""):
    """第一阶段短事务：落「在途任务 + 在途锁」并提交，返回 (job_id, conn)。"""
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    _precheck_lock(domain_id, lock_type)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                """
                INSERT INTO layer_jobs(
                  domain_id, step, status, message, target_biz_day, started_at
                ) VALUES (?, ?, '在途', ?, ?, datetime('now'))
                """,
                (
                    domain_id,
                    step,
                    f"{LOCK_LABELS[lock_type]}已启动，持有在途锁",
                    biz_day,
                ),
            )
            job_id = int(cur.lastrowid)
            # 事务内复检，防止两个请求同时通过只读预检
            locks.acquire_lock(conn, domain_id, lock_type, job_id, domain["name"])
        except Exception:
            conn.rollback()
            raise
        conn.commit()
    except Exception:
        conn.close()
        raise
    return job_id, conn


def _record_conflict(
    domain_id: int, step: str, exc: LockConflict, biz_day: str = ""
) -> None:
    """互斥失败同样落一条任务记录，写明对端在途类型，刷新后仍可查。"""
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO layer_jobs(
              domain_id, step, status, message,
              conflict_holder_type, conflict_holder_job_id, conflict_holder_label,
              target_biz_day, started_at, finished_at
            ) VALUES (?, ?, '互斥失败', ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (
                domain_id,
                step,
                exc.message,
                exc.holder_type,
                exc.holder_job_id,
                LOCK_LABELS.get(exc.holder_type, exc.holder_type),
                biz_day,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _guarded(domain_id: int, step: str, lock_type: str, worker, biz_day: str = ""):
    try:
        return worker()
    except LockConflict as exc:
        _record_conflict(domain_id, step, exc, biz_day)
        raise


def _mark_domain_available(conn, domain_id: int) -> None:
    conn.execute(
        "UPDATE domains SET available=1, unavailable_reason='' WHERE id=?",
        (domain_id,),
    )


def _mark_domain_unavailable(conn, domain_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE domains SET available=0, unavailable_reason=? WHERE id=?",
        (reason, domain_id),
    )


def _finish_ok(
    conn,
    job_id: int,
    domain_id: int,
    message: str,
    in_count: int,
    out_count: int,
    drop_count: int,
) -> None:
    conn.execute(
        """
        UPDATE layer_jobs
        SET status='成功', message=?, in_count=?, out_count=?, drop_count=?,
            finished_at=datetime('now')
        WHERE id=?
        """,
        (message, in_count, out_count, drop_count, job_id),
    )
    # 成功跑完即解除不可用标记（是否对齐由实时对齐状态如实反映）
    _mark_domain_available(conn, domain_id)
    locks.release_lock(conn, job_id)
    conn.commit()


def _abort(conn, job_id: int, domain_id: int, reason: str) -> None:
    """数据事务失败：先回滚本趟全部数据变更，再记失败、置主题域不可用、释放锁。"""
    conn.rollback()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE layer_jobs SET status='失败', message=?, finished_at=datetime('now') WHERE id=?",
        (reason, job_id),
    )
    _mark_domain_unavailable(conn, domain_id, reason)
    locks.release_lock(conn, job_id)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 分层重算（全部在调用方开启的数据事务内执行，未提交前对外不可见）
# ---------------------------------------------------------------------------
def _insert_detail_row(conn, domain_id: int, row) -> None:
    conn.execute(
        """
        INSERT INTO layer_detail(
          domain_id, biz_day, store_code, store_name, category, amount, qty, clean_flag
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            domain_id,
            row["biz_day"],
            row["store_code"],
            (row["store_name"] or "").strip(),
            row["category"],
            float(row["amount"]),
            int(row["qty"]),
            "已清洗",
        ),
    )


def _rebuild_detail_all(conn, domain_id: int) -> tuple[int, int, int]:
    conn.execute("DELETE FROM layer_detail WHERE domain_id=?", (domain_id,))
    paste = conn.execute(
        "SELECT * FROM layer_paste WHERE domain_id=? ORDER BY id", (domain_id,)
    ).fetchall()
    out = drop = 0
    for row in paste:
        store_name = (row["store_name"] or "").strip()
        amount = float(row["amount"])
        if not store_name or amount < 0:
            drop += 1
            continue
        _insert_detail_row(conn, domain_id, row)
        out += 1
    detail_phase_hook(conn, domain_id, "")
    return len(paste), out, drop


def _rebuild_detail_day(conn, domain_id: int, biz_day: str) -> tuple[int, int, int]:
    conn.execute(
        "DELETE FROM layer_detail WHERE domain_id=? AND biz_day=?",
        (domain_id, biz_day),
    )
    paste = conn.execute(
        "SELECT * FROM layer_paste WHERE domain_id=? AND biz_day=? ORDER BY id",
        (domain_id, biz_day),
    ).fetchall()
    out = drop = 0
    for row in paste:
        store_name = (row["store_name"] or "").strip()
        amount = float(row["amount"])
        if not store_name or amount < 0:
            drop += 1
            continue
        _insert_detail_row(conn, domain_id, row)
        out += 1
    detail_phase_hook(conn, domain_id, biz_day)
    return len(paste), out, drop


def _write_summary_buckets(conn, domain_id: int, detail_rows) -> tuple[int, int]:
    buckets: dict[tuple, dict] = {}
    for row in detail_rows:
        key = (row["biz_day"], row["store_code"])
        b = buckets.setdefault(
            key,
            {
                "biz_day": row["biz_day"],
                "store_code": row["store_code"],
                "amount": 0.0,
                "qty": 0,
                "n": 0,
            },
        )
        b["amount"] += float(row["amount"])
        b["qty"] += int(row["qty"])
        b["n"] += 1
    for b in buckets.values():
        conn.execute(
            """
            INSERT INTO layer_summary(
              domain_id, biz_day, store_code, amount_sum, qty_sum, line_count
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (domain_id, b["biz_day"], b["store_code"], b["amount"], b["qty"], b["n"]),
        )
    return len(detail_rows), len(buckets)


def _rebuild_summary_all(conn, domain_id: int) -> tuple[int, int]:
    conn.execute("DELETE FROM layer_summary WHERE domain_id=?", (domain_id,))
    detail = conn.execute(
        "SELECT * FROM layer_detail WHERE domain_id=? ORDER BY id", (domain_id,)
    ).fetchall()
    in_count, out_count = _write_summary_buckets(conn, domain_id, detail)
    summary_phase_hook(conn, domain_id, "")
    return in_count, out_count


def _rebuild_summary_day(conn, domain_id: int, biz_day: str) -> tuple[int, int]:
    conn.execute(
        "DELETE FROM layer_summary WHERE domain_id=? AND biz_day=?",
        (domain_id, biz_day),
    )
    detail = conn.execute(
        "SELECT * FROM layer_detail WHERE domain_id=? AND biz_day=? ORDER BY id",
        (domain_id, biz_day),
    ).fetchall()
    in_count, out_count = _write_summary_buckets(conn, domain_id, detail)
    summary_phase_hook(conn, domain_id, biz_day)
    return in_count, out_count


# ---------------------------------------------------------------------------
# 对外任务入口
# ---------------------------------------------------------------------------
def _run_paste_to_detail(domain_id: int) -> dict:
    job_id, conn = _start_job(domain_id, "paste_to_detail", LOCK_PASTE_TO_DETAIL)
    try:
        conn.execute("BEGIN IMMEDIATE")
        in_count, out_count, drop_count = _rebuild_detail_all(conn, domain_id)
        _finish_ok(
            conn,
            job_id,
            domain_id,
            f"清洗完成，剔除门店名为空或金额为负的行；在途锁已释放",
            in_count,
            out_count,
            drop_count,
        )
    except Exception as exc:
        _abort(
            conn,
            job_id,
            domain_id,
            f"贴源到明细失败，本趟明细变更已整体回滚，主题域标记不可用待重跑：{exc}",
        )
    finally:
        conn.close()
    return get_job(job_id)


def run_paste_to_detail(domain_id: int) -> dict:
    return _guarded(
        domain_id,
        "paste_to_detail",
        LOCK_PASTE_TO_DETAIL,
        lambda: _run_paste_to_detail(domain_id),
    )


def _run_detail_to_summary(domain_id: int) -> dict:
    job_id, conn = _start_job(domain_id, "detail_to_summary", LOCK_DETAIL_TO_SUMMARY)
    try:
        conn.execute("BEGIN IMMEDIATE")
        in_count, out_count = _rebuild_summary_all(conn, domain_id)
        _finish_ok(
            conn,
            job_id,
            domain_id,
            "按业务日+门店编码汇总；在途锁已释放",
            in_count,
            out_count,
            0,
        )
    except Exception as exc:
        _abort(
            conn,
            job_id,
            domain_id,
            f"明细到汇总失败，汇总变更已回滚，主题域标记不可用待重跑：{exc}",
        )
    finally:
        conn.close()
    return get_job(job_id)


def run_detail_to_summary(domain_id: int) -> dict:
    return _guarded(
        domain_id,
        "detail_to_summary",
        LOCK_DETAIL_TO_SUMMARY,
        lambda: _run_detail_to_summary(domain_id),
    )


def _run_backfill(domain_id: int, biz_day: str) -> dict:
    job_id, conn = _start_job(domain_id, "backfill", LOCK_BACKFILL, biz_day=biz_day)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 重跑指定日明细，再重算该日汇总键：同一事务，任一失败整体回滚
        d_in, d_out, d_drop = _rebuild_detail_day(conn, domain_id, biz_day)
        s_in, s_out = _rebuild_summary_day(conn, domain_id, biz_day)
        _finish_ok(
            conn,
            job_id,
            domain_id,
            (
                f"定点回刷业务日 {biz_day}：重跑明细（输入{d_in}行/写入{d_out}行/剔除{d_drop}行），"
                f"重算对应汇总键{s_out}个；在途锁已释放"
            ),
            d_in,
            s_out,
            d_drop,
        )
    except Exception as exc:
        _abort(
            conn,
            job_id,
            domain_id,
            (
                f"业务日{biz_day}定点回刷失败，本趟明细与汇总变更已整体回滚，"
                f"主题域标记不可用待重跑：{exc}"
            ),
        )
    finally:
        conn.close()
    return get_job(job_id)


def run_backfill(domain_id: int, biz_day: str) -> dict:
    if not biz_day or not _BIZ_DAY_RE.match(biz_day):
        raise ValueError("业务日格式应为 YYYY-MM-DD")
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        paste_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM layer_paste WHERE domain_id=? AND biz_day=?",
                (domain_id, biz_day),
            ).fetchone()["c"]
        )
    finally:
        conn.close()
    if paste_count == 0:
        raise ValueError(f"业务日 {biz_day} 在贴源层没有数据，无法定点回刷")
    return _guarded(
        domain_id,
        "backfill",
        LOCK_BACKFILL,
        lambda: _run_backfill(domain_id, biz_day),
        biz_day=biz_day,
    )


# ---------------------------------------------------------------------------
# 对账与总览
# ---------------------------------------------------------------------------
def run_reconcile(domain_id: int) -> dict:
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        paste_count = int(
            conn.execute("SELECT COUNT(*) AS c FROM layer_paste WHERE domain_id=?", (domain_id,)).fetchone()[
                "c"
            ]
        )
        detail_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM layer_detail WHERE domain_id=?", (domain_id,)
            ).fetchone()["c"]
        )
        summary = conn.execute(
            "SELECT COALESCE(SUM(line_count),0) AS s, COALESCE(SUM(amount_sum),0) AS a FROM layer_summary WHERE domain_id=?",
            (domain_id,),
        ).fetchone()
        summary_line_sum = int(summary["s"])
        summary_amount = float(summary["a"])
        detail_amount = float(
            conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS a FROM layer_detail WHERE domain_id=?",
                (domain_id,),
            ).fetchone()["a"]
        )
        key_mismatch = 1 if summary_line_sum != detail_count else 0
        amount_drift = round(summary_amount - detail_amount, 4)
        domain_unavailable = int(domain.get("available", 1)) == 0
        if domain_unavailable:
            status = "主题域不可用待重跑"
        elif key_mismatch == 0 and abs(amount_drift) < 0.0001:
            status = "一致"
        else:
            status = "存在差异"
        detail = {
            "贴源行数": paste_count,
            "明细行数": detail_count,
            "汇总行数合计": summary_line_sum,
            "明细金额合计": detail_amount,
            "汇总金额合计": summary_amount,
            "键对齐": key_mismatch == 0,
            "金额漂移": amount_drift,
            "主题域可用": not domain_unavailable,
            "不可用原因": domain.get("unavailable_reason", ""),
        }
        cur = conn.execute(
            """
            INSERT INTO reconcile_reports(
              domain_id, paste_count, detail_count, summary_line_sum, key_mismatch, amount_drift, status, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain_id,
                paste_count,
                detail_count,
                summary_line_sum,
                key_mismatch,
                amount_drift,
                status,
                json.dumps(detail, ensure_ascii=False),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM reconcile_reports WHERE id=?", (int(cur.lastrowid),)).fetchone()
        d = dict(row)
        d["detail"] = json.loads(d.pop("detail_json"))
        return d
    finally:
        conn.close()


def list_reconcile(domain_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        if domain_id is None:
            rows = conn.execute("SELECT * FROM reconcile_reports ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reconcile_reports WHERE domain_id=? ORDER BY id DESC",
                (domain_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out
    finally:
        conn.close()


def dashboard() -> dict:
    domains = list_domains()
    counts = layer_counts()
    jobs = list_jobs()[:5]
    reports = list_reconcile()[:5]
    in_flight = list_locks()
    return {
        "主题域数": len(domains),
        "分层计数": counts,
        "在途任务": [
            {
                "任务编号": l["job_id"],
                "主题域": l["domain_name"],
                "在途类型": l["lock_type_label"],
                "开始时间": l["created_at"],
            }
            for l in in_flight
        ],
        "最近任务": [
            {
                "编号": j["id"],
                "步骤": STEP_LABEL.get(j["step"], j["step"]),
                "状态": j["status"],
                "写入": j["out_count"],
                "剔除": j["drop_count"],
            }
            for j in jobs
        ],
        "最近对账": [
            {
                "编号": r["id"],
                "状态": r["status"],
                "明细行数": r["detail_count"],
                "汇总行数合计": r["summary_line_sum"],
            }
            for r in reports
        ],
        "主题域": [
            {
                "编号": d["id"],
                "名称": d["name"],
                "业务键": d["biz_key"],
                "可用": bool(d["available"]),
                "不可用原因": d.get("unavailable_reason", ""),
                "对齐状态": d["alignment"],
                "在途类型": d["in_flight_lock"],
            }
            for d in domains
        ],
    }
