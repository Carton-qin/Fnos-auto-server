from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models import SystemConfig
from app.api.auth import get_current_auth
from app.schemas import LLMConfigRequest, LLMTestRequest

router = APIRouter(prefix="/api/llm", tags=["LLM"])

class LLMRouterContext:
    llm_client = None

llm_router_ctx = LLMRouterContext()

@router.get("/config")
async def get_llm_config(db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    cfg_item = await db.get(SystemConfig, "llm")
    hist_item = await db.get(SystemConfig, "llm_history")

    cfg = cfg_item.value if cfg_item and isinstance(cfg_item.value, dict) else {}
    history = hist_item.value if hist_item and isinstance(hist_item.value, list) else []

    return {
        "ok": True,
        "config": cfg,
        "history": history
    }

@router.post("/config")
async def update_llm_config(req: LLMConfigRequest, db: AsyncSession = Depends(get_db), auth: bool = Depends(get_current_auth)):
    cfg_item = await db.get(SystemConfig, "llm")
    if not cfg_item:
        cfg_item = SystemConfig(key="llm", value={})
        db.add(cfg_item)

    new_cfg = {
        "base_url": req.base_url.strip(),
        "api_key": req.api_key.strip(),
        "model": req.model.strip(),
        "temperature": req.temperature,
        "digest_max_kb": max(5, min(256, req.digest_max_kb))
    }
    cfg_item.value = new_cfg

    # 更新历史记录
    hist_item = await db.get(SystemConfig, "llm_history")
    if not hist_item:
        hist_item = SystemConfig(key="llm_history", value=[])
        db.add(hist_item)

    history = list(hist_item.value or [])
    cleaned = [h for h in history if isinstance(h, dict) and h.get("base_url") != req.base_url]
    name = req.name or ""
    if not name:
        b_lower = req.base_url.lower()
        if "deepseek" in b_lower:
            name = "DeepSeek"
        elif "openai" in b_lower:
            name = "OpenAI"
        elif "aliyun" in b_lower or "dashscope" in b_lower:
            name = "通义千问"
        elif "moonshot" in b_lower or "kimi" in b_lower:
            name = "Kimi"
        elif "siliconflow" in b_lower:
            name = "SiliconFlow"
        else:
            name = req.base_url.split("//")[-1].split("/")[0]

    cleaned.insert(0, {
        "name": name,
        "base_url": req.base_url,
        "api_key": req.api_key
    })
    hist_item.value = cleaned[:5]

    await db.commit()
    return {"ok": True, "message": "大模型配置已保存"}

@router.post("/models")
async def fetch_models(data: dict, auth: bool = Depends(get_current_auth)):
    if not llm_router_ctx.llm_client:
        raise HTTPException(status_code=500, detail="LLM 客户端未初始化")

    base_url = data.get("base_url", "")
    api_key = data.get("api_key", "")
    ok, models, err = await llm_router_ctx.llm_client.fetch_models(base_url, api_key)
    return {"ok": ok, "models": models, "error": err}

@router.post("/test")
async def test_llm(req: LLMTestRequest, auth: bool = Depends(get_current_auth)):
    if not llm_router_ctx.llm_client:
        raise HTTPException(status_code=500, detail="LLM 客户端未初始化")

    messages = [{"role": "user", "content": req.prompt or "你好，请用一句话回答：1+1等于几？"}]
    ok, content = await llm_router_ctx.llm_client.chat_completion(messages, timeout=30)
    return {"ok": ok, "reply": content}
