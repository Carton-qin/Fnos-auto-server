import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.config import STATIC_DIR
from app.database import init_db, AsyncSessionLocal
from app.llm import LLMClient
from app.notifier import Notifier
from app.executor import TaskExecutor
from app.scheduler import TaskScheduler
from app.api import auth, tasks, logs, llm, push, system

# 配置日志输出格式
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("fnos.main")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 正在启动飞牛私有云 (fnOS) 自动化中枢服务...")

    # 1. 初始化 SQLite 数据库与数据表
    await init_db()
    logger.info("📦 SQLite 数据库与数据模型初始化完成")

    # 2. 实例化业务逻辑与中枢调度组件
    llm_client = LLMClient(db_session_factory=AsyncSessionLocal)
    notifier = Notifier(db_session_factory=AsyncSessionLocal)
    executor = TaskExecutor(
        db_session_factory=AsyncSessionLocal,
        llm_client=llm_client,
        notifier=notifier
    )
    scheduler = TaskScheduler(
        db_session_factory=AsyncSessionLocal,
        executor=executor,
        notifier=notifier
    )

    # 3. 依赖注入到 API 路由上下文
    tasks.task_ctx.executor = executor
    tasks.task_ctx.scheduler = scheduler
    tasks.task_ctx.llm_client = llm_client

    llm.llm_router_ctx.llm_client = llm_client
    push.push_router_ctx.notifier = notifier
    system.sys_router_ctx.scheduler = scheduler
    system.sys_router_ctx.notifier = notifier

    # 4. 启动 APScheduler 调度引擎
    await scheduler.start()
    logger.info("⏰ 工业级定时任务调度器已就绪 (UTC+8 亚洲/上海)")

    yield

    # 优雅停机
    logger.info("🛑 正在关闭调度引擎与后台服务...")
    if scheduler.scheduler.running:
        scheduler.scheduler.shutdown()
    logger.info("👋 服务已安全退出")

app = FastAPI(
    title="飞牛私有云 (fnOS) 自动化中枢",
    description="专为飞牛 NAS 设计的高性能自动化打卡、智能学术订阅与全景监控服务",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    lifespan=lifespan
)

# 配置 CORS 允许跨域调试
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册 API 路由组
app.include_router(auth.router)
app.include_router(tasks.router)
app.include_router(logs.router)
app.include_router(llm.router)
app.include_router(push.router)
app.include_router(system.router)

# 挂载静态文件目录
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
async def root_index():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"message": "飞牛私有云 (fnOS) 自动化中枢服务正在运行中，前端文件加载中..."}

@app.get("/manifest.json")
async def manifest():
    manifest_file = os.path.join(STATIC_DIR, "manifest.json")
    if os.path.exists(manifest_file):
        return FileResponse(manifest_file, media_type="application/manifest+json")
    return {
        "name": "飞牛自动化控制台",
        "short_name": "fnOS自动化",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f172a",
        "theme_color": "#0f172a"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8836, reload=True)
