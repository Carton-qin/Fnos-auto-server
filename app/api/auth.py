import time
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models import SystemConfig, TaskLog
from app.config import format_now
from app.schemas import LoginRequest

router = APIRouter(prefix="/api", tags=["Auth"])

# 防暴力破解登录锁定状态
login_lock_state = {
    "failed_attempts": 0,
    "lockout_until": 0
}

async def get_current_auth(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db)
) -> bool:
    cfg_item = await db.get(SystemConfig, "auth")
    auth_cfg = cfg_item.value if cfg_item and isinstance(cfg_item.value, dict) else {"enabled": True, "password": "admin"}

    if not auth_cfg.get("enabled", True):
        return True

    correct_pwd = auth_cfg.get("password", "admin")

    provided_token = None
    if authorization and authorization.startswith("Bearer "):
        provided_token = authorization[7:].strip()
    elif token:
        provided_token = token.strip()

    if provided_token == correct_pwd:
        return True

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized: Token 无效或未登录"
    )

@router.api_route("/auth/verify", methods=["GET", "POST"])
async def verify_auth(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db)
):
    cfg_item = await db.get(SystemConfig, "auth")
    auth_cfg = cfg_item.value if cfg_item and isinstance(cfg_item.value, dict) else {"enabled": True, "password": "admin"}

    if not auth_cfg.get("enabled", True):
        return {"ok": True, "auth_enabled": False, "authenticated": True}

    correct_pwd = auth_cfg.get("password", "admin")
    provided_token = None
    if authorization and authorization.startswith("Bearer "):
        provided_token = authorization[7:].strip()
    elif token:
        provided_token = token.strip()

    if provided_token == correct_pwd:
        return {"ok": True, "auth_enabled": True, "authenticated": True}
    return {"ok": False, "auth_enabled": True, "authenticated": False, "error": "Unauthorized"}

@router.get("/auth/status")
async def auth_status():
    now_ts = time.time()
    is_locked = (now_ts < login_lock_state["lockout_until"])
    rem_sec = int(login_lock_state["lockout_until"] - now_ts) if is_locked else 0
    return {
        "ok": True,
        "locked": is_locked,
        "retry_after": rem_sec,
        "failed_attempts": login_lock_state["failed_attempts"]
    }

@router.post("/login")
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    now_ts = time.time()
    if now_ts < login_lock_state["lockout_until"]:
        rem_sec = int(login_lock_state["lockout_until"] - now_ts)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"输错密码过多，控制台已临时锁定！请等待 {rem_sec} 秒后重试。"
        )

    cfg_item = await db.get(SystemConfig, "auth")
    auth_cfg = cfg_item.value if cfg_item and isinstance(cfg_item.value, dict) else {"enabled": True, "password": "admin"}
    correct_pwd = auth_cfg.get("password", "admin")

    if req.password == correct_pwd:
        login_lock_state["failed_attempts"] = 0
        login_lock_state["lockout_until"] = 0
        log_entry = TaskLog(
            category="system",
            status="success",
            message="管理员登录成功",
            time=format_now()
        )
        db.add(log_entry)
        await db.commit()
        return {"ok": True, "token": correct_pwd}

    login_lock_state["failed_attempts"] += 1
    fails = login_lock_state["failed_attempts"]
    if fails >= 5:
        login_lock_state["lockout_until"] = now_ts + 300
        log_entry = TaskLog(
            category="system",
            status="fail",
            message="密码连续错误 5 次，控制台触发防爆破锁定 5 分钟",
            time=format_now()
        )
        db.add(log_entry)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="连续输错 5 次密码！系统已触发防暴力破解锁定，请等待 300 秒后再试。"
        )
    else:
        rem = 5 - fails
        log_entry = TaskLog(
            category="system",
            status="warning",
            message=f"密码错误 (已累计 {fails} 次)",
            time=format_now()
        )
        db.add(log_entry)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"密码错误！还剩 {rem} 次尝试机会，连续 5 次将锁定 5 分钟。"
        )

@router.post("/system/password")
async def update_password(data: dict, db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    enabled = bool(data.get("enabled", True))
    pwd = (data.get("password") or "admin").strip()
    if not pwd and enabled:
        raise HTTPException(status_code=400, detail="密码不能为空")

    cfg_item = await db.get(SystemConfig, "auth")
    if not cfg_item:
        cfg_item = SystemConfig(key="auth", value={})
        db.add(cfg_item)
    cfg_item.value = {"enabled": enabled, "password": pwd}
    await db.commit()
    return {"ok": True, "message": "管理员密码已更新"}
