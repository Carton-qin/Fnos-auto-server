import os
import time
import shutil
import psutil
from typing import Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models import Task, SystemConfig, TaskLog
from app.api.auth import get_current_auth
from app.config import format_now, DATA_DIR, DB_PATH
from app.crawler import crawler
import httpx

router = APIRouter(prefix="/api", tags=["System"])

SERVER_START_TIME = time.time()

class SystemRouterContext:
    scheduler = None
    notifier = None

sys_router_ctx = SystemRouterContext()

@router.get("/status")
async def get_system_status(db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    uptime_sec = int(time.time() - SERVER_START_TIME)

    # 硬件指标收集
    cpu_percent = psutil.cpu_percent(interval=None)
    vmem = psutil.virtual_memory()

    # 磁盘指标
    try:
        disk = shutil.disk_usage(DATA_DIR)
        disk_total_gb = round(disk.total / (1024 ** 3), 1)
        disk_free_gb = round(disk.free / (1024 ** 3), 1)
    except Exception:
        disk_total_gb = 0
        disk_free_gb = 0

    # 数据库文件大小
    db_size_kb = 0
    if os.path.exists(DB_PATH):
        db_size_kb = round(os.path.getsize(DB_PATH) / 1024, 1)

    task_count = (await db.execute(select(func.count()).select_from(Task))).scalar() or 0
    auth_cfg = (await db.get(SystemConfig, "auth"))
    auth_enabled = (auth_cfg.value.get("enabled", True)) if auth_cfg and isinstance(auth_cfg.value, dict) else True

    return {
        "ok": True,
        "status": {
            "uptime_seconds": uptime_sec,
            "time": format_now(),
            "platform": "飞牛私有云 (fnOS / Linux x86_64)",
            "cpu_percent": cpu_percent,
            "memory": {
                "total_mb": round(vmem.total / (1024 * 1024), 1),
                "used_mb": round(vmem.used / (1024 * 1024), 1),
                "free_mb": round(vmem.available / (1024 * 1024), 1),
                "percent": vmem.percent
            },
            "disk": {
                "total_gb": disk_total_gb,
                "free_gb": disk_free_gb
            },
            "db_size_kb": db_size_kb,
            "crawler": {
                "playwright_available": crawler.playwright_available,
                "engine": "智能双模 (Httpx 极速异步 + Playwright Chromium 无头浏览器)"
            },
            "task_count": task_count,
            "auth_enabled": auth_enabled,
            "port": 8836
        }
    }

@router.get("/system/net_ping")
async def net_ping(auth: bool = Depends(get_current_auth)):
    t_start = time.time()
    try:
        async with httpx.AsyncClient(timeout=6.0, verify=False) as client:
            resp = await client.get("http://connect.rom.miui.com/generate_204")
            latency_ms = int((time.time() - t_start) * 1000)
            return {"ok": True, "online": True, "latency_ms": latency_ms, "status": resp.status_code}
    except Exception as e:
        latency_ms = int((time.time() - t_start) * 1000)
        return {"ok": True, "online": False, "latency_ms": latency_ms, "error": str(e)}

@router.post("/system/trigger_report")
async def trigger_report(auth: bool = Depends(get_current_auth)):
    if not sys_router_ctx.scheduler or not sys_router_ctx.notifier:
        raise HTTPException(status_code=500, detail="调度器或通知中心未就绪")

    report_text = await sys_router_ctx.scheduler.generate_weekly_report()
    res = await sys_router_ctx.notifier.send("【飞牛 NAS 系统健康周报】", report_text, level="urgent")
    return {
        "ok": True,
        "message": "体检周报已生成并推送！",
        "report": report_text,
        "push_result": res
    }

@router.get("/backup/export")
async def backup_export(db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    # 导出系统配置
    configs_res = await db.execute(select(SystemConfig))
    all_configs = {c.key: c.value for c in configs_res.scalars().all()}

    # 导出所有任务
    tasks_res = await db.execute(select(Task))
    all_tasks = [
        {
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
            "ai_diagnose": t.ai_diagnose,
            "use_headless_browser": t.use_headless_browser,
            "params": t.params or {},
            "stats": t.stats or {}
        }
        for t in tasks_res.scalars().all()
    ]

    import json
    bundle = {
        "version": "2.0-fnos",
        "timestamp": format_now(),
        "configs": all_configs,
        "tasks": all_tasks
    }
    return Response(
        content=json.dumps(bundle, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=fnos_backup.json"}
    )

@router.post("/backup/import")
async def backup_import(data: dict, db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    configs = data.get("configs") or data.get("config")
    tasks = data.get("tasks")

    if not isinstance(configs, dict) and not isinstance(tasks, list):
        raise HTTPException(status_code=400, detail="备份文件不合法，需包含有效的 configs 或 tasks！")

    if isinstance(configs, dict):
        for k, v in configs.items():
            existing = await db.get(SystemConfig, k)
            if existing:
                existing.value = v
            else:
                db.add(SystemConfig(key=k, value=v))

    if isinstance(tasks, list):
        for t_data in tasks:
            t_id = t_data.get("id")
            if not t_id:
                continue
            existing = await db.get(Task, t_id)
            if existing:
                for attr, val in t_data.items():
                    if hasattr(existing, attr) and attr != "id":
                        setattr(existing, attr, val)
            else:
                db.add(Task(**t_data))

    await db.commit()

    if sys_router_ctx.scheduler:
        await sys_router_ctx.scheduler.reload_all_tasks()

    return {"ok": True, "message": "配置与自动化任务已成功恢复！"}
