from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models import SystemConfig
from app.api.auth import get_current_auth
from app.config import format_now
from app.schemas import PushTestRequest

router = APIRouter(prefix="/api/push", tags=["Push"])

class PushRouterContext:
    notifier = None

push_router_ctx = PushRouterContext()

@router.get("/config")
async def get_push_config(db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    cfg_item = await db.get(SystemConfig, "notify")
    cfg = cfg_item.value if cfg_item and isinstance(cfg_item.value, dict) else {}
    return {"ok": True, "config": cfg}

@router.post("/config")
async def update_push_config(data: dict, db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    cfg_item = await db.get(SystemConfig, "notify")
    if not cfg_item:
        cfg_item = SystemConfig(key="notify", value={})
        db.add(cfg_item)

    cfg_item.value = data
    await db.commit()
    return {"ok": True, "message": "推送配置已保存"}

@router.post("/test")
async def test_push(req: PushTestRequest, auth: bool = Depends(get_current_auth)):
    if not push_router_ctx.notifier:
        raise HTTPException(status_code=500, detail="通知管理器未就绪")

    title = "【飞牛 NAS 测试推送】"
    content = f"恭喜！飞牛私有云 (fnOS) 自动化中枢推送配置测试成功！\n当前测试时间：{format_now()}"

    res = await push_router_ctx.notifier.send(
        title=title,
        content=content,
        level="urgent",  # 测试消息穿透免打扰
        channel=req.channel,
        override_cfg=req.override_cfg
    )
    return {"ok": True, "results": res}
