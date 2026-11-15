from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.locks import LOCK_LABELS, LockConflict
from app.services import pipeline

router = APIRouter(tags=["分层任务"])


class JobBody(BaseModel):
    domain_id: int = Field(..., description="主题域编号")


class BackfillBody(BaseModel):
    domain_id: int = Field(..., description="主题域编号")
    biz_day: str = Field(..., description="定点回刷业务日，格式 YYYY-MM-DD")


def _conflict_response(exc: LockConflict) -> HTTPException:
    # 冲突必须失败，并写明对端在途类型；记录已在服务层落库
    return HTTPException(
        status_code=409,
        detail={
            "错误": "在途锁互斥冲突",
            "原因": exc.message,
            "对端在途类型": LOCK_LABELS.get(exc.holder_type, exc.holder_type),
            "对端任务编号": exc.holder_job_id,
        },
    )


@router.get("/jobs")
def list_jobs(domain_id: int | None = None):
    return pipeline.list_jobs(domain_id)


@router.get("/jobs/{job_id}")
def get_job(job_id: int):
    job = pipeline.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@router.get("/locks")
def list_locks(domain_id: int | None = None):
    """可查询的在途锁：刷新页面/重启后仍同源可查。"""
    return pipeline.list_locks(domain_id)


@router.post("/jobs/paste-to-detail")
def paste_to_detail(body: JobBody):
    try:
        return pipeline.run_paste_to_detail(body.domain_id)
    except LockConflict as exc:
        raise _conflict_response(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/jobs/detail-to-summary")
def detail_to_summary(body: JobBody):
    try:
        return pipeline.run_detail_to_summary(body.domain_id)
    except LockConflict as exc:
        raise _conflict_response(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/jobs/backfill")
def backfill(body: BackfillBody):
    try:
        return pipeline.run_backfill(body.domain_id, body.biz_day)
    except LockConflict as exc:
        raise _conflict_response(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
