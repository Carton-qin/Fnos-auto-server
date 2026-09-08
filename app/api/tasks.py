import uuid
import time
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from app.database import get_db
from app.models import Task, TaskLog
from app.schemas import TaskCreate, TaskUpdate, HttpDebugRequest, AIGenerateTaskRequest, CookieUpdateRequest, LegacyImportRequest
from app.api.auth import get_current_auth
from app.crawler import crawler, get_cookie_expiration_days
from app.config import get_now_beijing

router = APIRouter(prefix="/api", tags=["Tasks"])

# 全局依赖注入占位，在 main.py 启动时绑定 executor, scheduler, llm_client
class TaskContext:
    executor = None
    scheduler = None
    llm_client = None

task_ctx = TaskContext()

@router.get("/tasks")
async def list_tasks(db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    result = await db.execute(select(Task).order_by(Task.created_at.desc()))
    tasks = result.scalars().all()

    enriched_tasks = []
    for t in tasks:
        tc = {
            "id": t.id,
            "name": t.name,
            "type": t.type,
            "enabled": t.enabled,
            "schedule_type": t.schedule_type,
            "cron_expr": t.cron_expr,
            "window_start": t.window_start,
            "window_end": t.window_end,
            "jitter_mins": t.jitter_mins,
            "retry_count": t.retry_count,
            "retry_delay": t.retry_delay,
            "notify_on_success": t.notify_on_success,
            "notify_on_failure": t.notify_on_failure,
            "ai_diagnose": t.ai_diagnose,
            "use_headless_browser": t.use_headless_browser,
            "params": t.params or {},
            "stats": t.stats or {},
            "last_run": t.last_run,
            "last_status": t.last_status,
            "last_result": t.last_result,
            "created_at": t.created_at,
            "updated_at": t.updated_at
        }

        # Cookie / JWT 过期天数计算
        headers = (t.params or {}).get("headers") or {}
        has_jwt, rem_days, exp_ts, exp_date = get_cookie_expiration_days(headers)
        if has_jwt and rem_days is not None:
            tc["cookie_days"] = rem_days
            tc["cookie_exp_date"] = exp_date
            if rem_days <= 0:
                tc["cookie_status"] = "expired"
            elif rem_days <= 3:
                tc["cookie_status"] = "expiring_soon"
            else:
                tc["cookie_status"] = "valid"
        else:
            tc["cookie_status"] = "none"
            tc["cookie_days"] = None
            tc["cookie_exp_date"] = None

        if t.schedule_type == "window" and task_ctx.scheduler:
            tc["today_target_time"] = task_ctx.scheduler.task_window_targets.get(t.id, "")

        # 账密模式与 StorageState 状态
        from app.crawler import has_valid_session, get_session_updated_at
        tc["auth_mode"] = t.auth_mode or "cookie"
        tc["session_exists"] = has_valid_session(t.id)
        tc["session_updated_at"] = get_session_updated_at(t.id)

        # 敏感密码掩码保护
        ac = dict(t.account_config or {})
        if "password" in ac and ac["password"]:
            ac["password"] = "******"
        tc["account_config"] = ac

        enriched_tasks.append(tc)

    return {"ok": True, "tasks": enriched_tasks}

@router.post("/tasks")
async def upsert_task(task_data: dict, db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    from app.security import encrypt_credential
    task_id = task_data.get("id") or str(uuid.uuid4())
    existing = await db.get(Task, task_id)

    # 处理账号密码加密
    auth_mode = task_data.get("auth_mode", "cookie")
    raw_ac = dict(task_data.get("account_config") or {})
    if "password" in raw_ac and raw_ac["password"]:
        if raw_ac["password"] == "******":
            if existing and existing.account_config:
                raw_ac["password"] = existing.account_config.get("password", "")
        else:
            raw_ac["password"] = encrypt_credential(raw_ac["password"])

    if existing:
        for k, v in task_data.items():
            if hasattr(existing, k) and k not in ["id", "account_config"]:
                setattr(existing, k, v)
        existing.auth_mode = auth_mode
        existing.account_config = raw_ac
        task_obj = existing
    else:
        task_obj = Task(
            id=task_id,
            name=task_data.get("name", "未命名任务"),
            type=task_data.get("type", "checkin"),
            enabled=task_data.get("enabled", True),
            schedule_type=task_data.get("schedule_type", "cron"),
            cron_expr=task_data.get("cron_expr", "0 8 * * *"),
            window_start=task_data.get("window_start", "08:00"),
            window_end=task_data.get("window_end", "09:00"),
            jitter_mins=task_data.get("jitter_mins", 0),
            retry_count=task_data.get("retry_count", 1),
            retry_delay=task_data.get("retry_delay", 3),
            notify_on_success=task_data.get("notify_on_success", False),
            notify_on_failure=task_data.get("notify_on_failure", True),
            ai_diagnose=task_data.get("ai_diagnose", True),
            use_headless_browser=task_data.get("use_headless_browser", False),
            auth_mode=auth_mode,
            account_config=raw_ac,
            params=task_data.get("params", {}),
            stats=task_data.get("stats", {})
        )
        db.add(task_obj)

    await db.commit()
    await db.refresh(task_obj)

    # 动态同步到 APScheduler
    if task_ctx.scheduler:
        await task_ctx.scheduler.schedule_task(task_obj)

    return {"ok": True, "task": {"id": task_obj.id, "name": task_obj.name}}

@router.post("/tasks/{task_id}/relogin")
async def relogin_task(task_id: str, db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.auth_mode != "playwright_account":
        raise HTTPException(status_code=400, detail="该任务未启用账密自动托管模式")

    ok, msg = await crawler.perform_auto_login(task.id, task.account_config or {}, task_ctx.llm_client)
    return {"ok": ok, "message": msg}

@router.get("/tasks/{task_id}/login_screen")
async def login_screen(task_id: str, db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    ac = task.account_config or {}
    login_url = ac.get("login_url") or (task.params or {}).get("url") or ""
    if not login_url:
        raise HTTPException(status_code=400, detail="未配置登录页 URL")

    img_bytes = await crawler.capture_page_screenshot(login_url)
    if not img_bytes:
        raise HTTPException(status_code=500, detail="截屏失败")

    return Response(content=img_bytes, media_type="image/png")

@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task_ctx.scheduler:
        task_ctx.scheduler.remove_task(task_id)

    await db.delete(task)
    await db.commit()
    return {"ok": True, "message": "任务已删除"}

@router.post("/tasks/{task_id}/toggle")
async def toggle_task(task_id: str, db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    task.enabled = not task.enabled
    await db.commit()

    if task_ctx.scheduler:
        if task.enabled:
            await task_ctx.scheduler.schedule_task(task)
        else:
            task_ctx.scheduler.remove_task(task_id)

    return {"ok": True, "enabled": task.enabled}

@router.post("/tasks/{task_id}/run")
async def run_task(task_id: str, db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if not task_ctx.executor:
        raise HTTPException(status_code=500, detail="执行器未就绪")

    success, msg, ai_diag = await task_ctx.executor.run_task(task_id, "手动立即触发")
    return {
        "ok": True,
        "success": success,
        "result": msg,
        "ai_diag": ai_diag
    }

@router.post("/tasks/{task_id}/cookie")
async def update_task_cookie(task_id: str, req: CookieUpdateRequest, db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    params = dict(task.params or {})
    headers = dict(params.get("headers") or {})
    headers["Cookie"] = req.cookie.strip()
    params["headers"] = headers
    task.params = params

    # 若之前熔断，重置熔断状态
    stats = dict(task.stats or {})
    if stats.get("circuit_tripped"):
        stats["circuit_tripped"] = False
        stats["consecutive_fails"] = 0
        stats["circuit_tripped_until"] = 0
    task.stats = stats

    await db.commit()

    has_jwt, rem_days, exp_ts, exp_date = get_cookie_expiration_days(headers)
    return {
        "ok": True,
        "message": "Cookie 更新成功！",
        "cookie_days": rem_days,
        "cookie_exp_date": exp_date
    }

@router.post("/tasks/{task_id}/reset_circuit")
async def reset_circuit(task_id: str, db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    stats = dict(task.stats or {})
    stats["circuit_tripped"] = False
    stats["consecutive_fails"] = 0
    stats["circuit_tripped_until"] = 0
    task.stats = stats
    await db.commit()
    return {"ok": True, "message": "防封熔断状态已清除，任务恢复调度！"}

@router.get("/tasks/heatmap")
async def get_heatmap(db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    now = get_now_beijing()
    days_map = {}
    for i in range(59, -1, -1):
        d_str = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        days_map[d_str] = {"success": 0, "fail": 0}

    result = await db.execute(select(Task))
    tasks = result.scalars().all()
    for t in tasks:
        hist = (t.stats or {}).get("history") or {}
        for d_str, info in hist.items():
            if d_str in days_map:
                if info.get("status") == "success":
                    days_map[d_str]["success"] += 1
                elif info.get("status") == "fail":
                    days_map[d_str]["fail"] += 1

    return {"ok": True, "heatmap": days_map}

@router.post("/http/debug")
async def http_debug(req: HttpDebugRequest, db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    url = req.url
    method = req.method
    headers = req.headers
    body = req.body

    if req.task_id:
        task = await db.get(Task, req.task_id)
        if task:
            p = task.params or {}
            url = p.get("url") or p.get("source_url") or p.get("target_url") or url
            method = p.get("method") or method
            headers = p.get("headers") or headers
            body = p.get("body") or body

    if not url:
        raise HTTPException(status_code=400, detail="URL 不能为空")

    res = await crawler.fetch(url, method=method, headers=headers, body=body, timeout=20)
    return {
        "ok": res.ok,
        "status_code": res.status_code,
        "latency_ms": res.latency_ms,
        "mode_used": res.mode_used,
        "headers": res.headers,
        "set_cookies": res.set_cookies,
        "body": res.text[:4096],
        "body_length": len(res.text),
        "truncated": len(res.text) > 4096,
        "error": res.error
    }

@router.post("/tasks/ai_generate")
async def ai_generate_task(req: AIGenerateTaskRequest, auth: bool = Depends(get_current_auth)):
    if not task_ctx.llm_client:
        raise HTTPException(status_code=500, detail="大模型客户端未初始化")

    if not req.url and not req.prompt and not req.curl:
        raise HTTPException(status_code=400, detail="请至少输入目标网址、需求描述或 cURL！")

    ok, res = await task_ctx.llm_client.analyze_and_generate_task(req.url, req.prompt, req.curl)
    if ok:
        return {"ok": True, "task": res}
    return {"ok": False, "message": res}

@router.post("/tasks/import-legacy")
async def import_legacy_tasks(req: LegacyImportRequest, db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    """
    无缝导入旧版 ESP32 单片机导出的 tasks.json 自动化任务
    """
    raw_tasks = req.tasks or []
    if not raw_tasks:
        raise HTTPException(status_code=400, detail="导入任务列表为空")

    imported_count = 0
    for t_data in raw_tasks:
        t_id = t_data.get("id") or str(uuid.uuid4())
        existing = await db.get(Task, t_id)
        if existing:
            existing.name = t_data.get("name", existing.name)
            existing.type = t_data.get("type", existing.type)
            existing.enabled = t_data.get("enabled", existing.enabled)
            existing.schedule_type = t_data.get("schedule_type", existing.schedule_type)
            existing.cron_expr = t_data.get("cron_expr", existing.cron_expr)
            existing.window_start = t_data.get("window_start", existing.window_start)
            existing.window_end = t_data.get("window_end", existing.window_end)
            existing.jitter_mins = t_data.get("jitter_mins", existing.jitter_mins)
            existing.retry_count = t_data.get("retry_count", existing.retry_count)
            existing.notify_on_success = t_data.get("notify_on_success", existing.notify_on_success)
            existing.notify_on_failure = t_data.get("notify_on_failure", existing.notify_on_failure)
            existing.params = t_data.get("params", existing.params)
            existing.stats = t_data.get("stats", existing.stats)
        else:
            new_task = Task(
                id=t_id,
                name=t_data.get("name", "未命名任务"),
                type=t_data.get("type", "checkin"),
                enabled=t_data.get("enabled", True),
                schedule_type=t_data.get("schedule_type", "cron"),
                cron_expr=t_data.get("cron_expr", "0 8 * * *"),
                window_start=t_data.get("window_start", "08:00"),
                window_end=t_data.get("window_end", "09:00"),
                jitter_mins=t_data.get("jitter_mins", 0),
                retry_count=t_data.get("retry_count", 1),
                notify_on_success=t_data.get("notify_on_success", False),
                notify_on_failure=t_data.get("notify_on_failure", True),
                params=t_data.get("params", {}),
                stats=t_data.get("stats", {})
            )
            db.add(new_task)
        imported_count += 1

    await db.commit()

    if task_ctx.scheduler:
        await task_ctx.scheduler.reload_all_tasks()

    return {"ok": True, "imported_count": imported_count, "message": f"成功平滑导入并同步 {imported_count} 个自动化任务！"}

@router.get("/tasks/export-legacy")
async def export_legacy_tasks(db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    result = await db.execute(select(Task))
    tasks = result.scalars().all()
    out = []
    for t in tasks:
        out.append({
            "id": t.id,
            "name": t.name,
            "type": t.type,
            "enabled": t.enabled,
            "schedule_type": t.schedule_type,
            "cron_expr": t.cron_expr,
            "window_start": t.window_start,
            "window_end": t.window_end,
            "jitter_mins": t.jitter_mins,
            "retry_count": t.retry_count,
            "notify_on_success": t.notify_on_success,
            "notify_on_failure": t.notify_on_failure,
            "params": t.params or {},
            "stats": t.stats or {}
        })
    return {"ok": True, "tasks": out}
