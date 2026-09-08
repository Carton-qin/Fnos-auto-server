from typing import Optional
from fastapi import APIRouter, Depends, Response, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func
from app.database import get_db
from app.models import TaskLog
from app.api.auth import get_current_auth

router = APIRouter(prefix="/api", tags=["Logs"])

@router.get("/logs")
async def list_logs(
    category: str = Query("all"),
    status: Optional[str] = None,
    task_id: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(150, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    auth: bool = Depends(get_current_auth)
):
    stmt = select(TaskLog)

    # 分类筛选
    if category == "fail":
        stmt = stmt.where(TaskLog.status == "fail")
    elif category == "success":
        stmt = stmt.where(TaskLog.status == "success")
    elif category == "ai":
        stmt = stmt.where((TaskLog.ai_diag.is_not(None)) | (TaskLog.task_name.like("%AI%")))
    elif category in ["task", "system"]:
        stmt = stmt.where(TaskLog.category == category)

    if status and status != "all":
        stmt = stmt.where(TaskLog.status == status)

    if task_id and task_id != "all":
        if task_id == "SYSTEM":
            stmt = stmt.where(TaskLog.category == "system")
        else:
            stmt = stmt.where(TaskLog.task_id == task_id)

    if q and q.strip():
        search_kw = f"%{q.strip()}%"
        stmt = stmt.where(
            (TaskLog.message.like(search_kw)) |
            (TaskLog.task_name.like(search_kw)) |
            (TaskLog.ai_diag.like(search_kw))
        )

    stmt = stmt.order_by(TaskLog.id.desc()).offset(offset).limit(limit)
    result = await db.execute(stmt)
    logs = result.scalars().all()

    # 各维度统计数量
    count_all = (await db.execute(select(func.count()).select_from(TaskLog))).scalar() or 0
    count_task = (await db.execute(select(func.count()).select_from(TaskLog).where(TaskLog.category == "task"))).scalar() or 0
    count_sys = (await db.execute(select(func.count()).select_from(TaskLog).where(TaskLog.category == "system"))).scalar() or 0
    count_fail = (await db.execute(select(func.count()).select_from(TaskLog).where(TaskLog.status == "fail"))).scalar() or 0
    count_succ = (await db.execute(select(func.count()).select_from(TaskLog).where(TaskLog.status == "success"))).scalar() or 0
    count_ai = (await db.execute(select(func.count()).select_from(TaskLog).where((TaskLog.ai_diag.is_not(None)) | (TaskLog.task_name.like("%AI%"))))).scalar() or 0

    return {
        "ok": True,
        "logs": [
            {
                "id": l.id,
                "task_id": l.task_id,
                "task_name": l.task_name,
                "category": l.category,
                "status": l.status,
                "message": l.message,
                "ai_diag": l.ai_diag,
                "latency_ms": l.latency_ms,
                "time": l.time
            }
            for l in logs
        ],
        "counts": {
            "all": count_all,
            "task": count_task,
            "system": count_sys,
            "fail": count_fail,
            "success": count_succ,
            "ai": count_ai
        }
    }


@router.delete("/logs")
async def clear_logs(db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    await db.execute(delete(TaskLog))
    await db.commit()
    return {"ok": True, "message": "审计日志已清空"}

@router.get("/logs/download")
async def download_logs(db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    result = await db.execute(select(TaskLog).order_by(TaskLog.id.asc()))
    logs = result.scalars().all()

    lines = [
        "=======================================================================",
        "飞牛私有云 (fnOS) 自动化中枢 - 运行审计日志导出",
        "======================================================================="
    ]
    for l in logs:
        diag = f" | 🤖 AI诊断: {l.ai_diag}" if l.ai_diag else ""
        lines.append(f"[{l.time}] [{l.category.upper()}] [{l.status.upper()}] [{l.task_name or l.task_id or 'SYSTEM'}] {l.message}{diag}")

    content = "\n".join(lines)
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=fnos_system_logs.txt"
        }
    )
