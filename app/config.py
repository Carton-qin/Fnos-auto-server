import os
import json
from datetime import datetime, timezone, timedelta

# 目录路径定义
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 如果在 Docker 容器内部 (/app/data 存在) 则使用 /app/data，否则在项目根目录下的 data 文件夹
if os.path.exists("/app/data") or os.environ.get("RUNNING_IN_DOCKER") == "true":
    DATA_DIR = "/app/data"
else:
    DATA_DIR = os.path.join(BASE_DIR, "data")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "backups"), exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "app.db")
DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"
SYNC_DATABASE_URL = f"sqlite:///{DB_PATH}"

STATIC_DIR = os.path.join(BASE_DIR, "app", "static")

# 时区常量 (北京时间 UTC+8)
BEIJING_TZ = timezone(timedelta(hours=8))

def get_now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)

def format_now(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return get_now_beijing().strftime(fmt)

# 默认系统配置模板
DEFAULT_CONFIG = {
    "auth": {
        "enabled": True,
        "password": "admin"
    },
    "llm": {
        "base_url": "",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.7,
        "digest_max_kb": 128
    },
    "llm_history": [],
    "notify": {
        "policy": {
            "mode": "all",
            "dnd_enabled": True,
            "dnd_start": 23,
            "dnd_end": 8
        },
        "pushplus": {"enabled": False, "token": ""},
        "feishu": {"enabled": False, "webhook": ""},
        "dingtalk": {"enabled": False, "webhook": "", "secret": ""},
        "wechat_work": {"enabled": False, "webhook": ""},
        "bark": {"enabled": False, "server_url": "https://api.day.app", "device_key": ""},
        "custom": {
            "enabled": False,
            "url": "",
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "template": '{"title": "{title}", "content": "{content}"}'
        }
    },
    "crawler": {
        "headless_enabled": True,
        "timeout_seconds": 30,
        "max_kb": 128
    }
}
