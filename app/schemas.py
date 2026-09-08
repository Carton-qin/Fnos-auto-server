from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from datetime import datetime

# --- Task Schemas ---
class TaskBase(BaseModel):
    name: str
    type: str = "checkin"
    enabled: bool = True
    schedule_type: str = "cron"
    cron_expr: Optional[str] = "0 8 * * *"
    window_start: Optional[str] = "08:00"
    window_end: Optional[str] = "09:00"
    jitter_mins: int = 0
    retry_count: int = 1
    retry_delay: int = 3
    notify_on_success: bool = False
    notify_on_failure: bool = True
    ai_diagnose: bool = True
    use_headless_browser: bool = False
    auth_mode: str = "cookie"  # 'cookie' 或 'playwright_account'
    account_config: Dict[str, Any] = Field(default_factory=dict)
    params: Dict[str, Any] = Field(default_factory=dict)
    stats: Dict[str, Any] = Field(default_factory=dict)

class TaskCreate(TaskBase):
    id: Optional[str] = None

class TaskUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    enabled: Optional[bool] = None
    schedule_type: Optional[str] = None
    cron_expr: Optional[str] = None
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    jitter_mins: Optional[int] = None
    retry_count: Optional[int] = None
    retry_delay: Optional[int] = None
    notify_on_success: Optional[bool] = None
    notify_on_failure: Optional[bool] = None
    ai_diagnose: Optional[bool] = None
    use_headless_browser: Optional[bool] = None
    auth_mode: Optional[str] = None
    account_config: Optional[Dict[str, Any]] = None
    params: Optional[Dict[str, Any]] = None
    stats: Optional[Dict[str, Any]] = None

class TaskOut(TaskBase):
    id: str
    last_run: Optional[str] = None
    last_status: Optional[str] = None
    last_result: Optional[str] = None
    cookie_days: Optional[float] = None
    cookie_exp_date: Optional[str] = None
    cookie_status: Optional[str] = None
    today_target_time: Optional[str] = None
    session_exists: Optional[bool] = False
    session_updated_at: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

# --- Log Schemas ---
class TaskLogOut(BaseModel):
    id: int
    task_id: Optional[str] = None
    task_name: Optional[str] = None
    category: str = "task"
    status: str
    message: str
    ai_diag: Optional[str] = None
    latency_ms: Optional[int] = None
    time: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True

# --- Reward Schemas ---
class RewardOut(BaseModel):
    id: int
    task_id: Optional[str] = None
    task_name: Optional[str] = None
    date: str
    amount: int
    unit: str = "点"
    note: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True

# --- LLM & Push Schemas ---
class LLMConfigRequest(BaseModel):
    base_url: str
    api_key: str
    model: str = "deepseek-chat"
    temperature: float = 0.7
    digest_max_kb: int = 2048
    name: Optional[str] = ""


class LLMTestRequest(BaseModel):
    prompt: Optional[str] = "你好，请用一句话回答：1+1等于几？"

class PushTestRequest(BaseModel):
    channel: str = "all"
    override_cfg: Optional[Dict[str, Any]] = None

class HttpDebugRequest(BaseModel):
    task_id: Optional[str] = None
    method: str = "GET"
    url: Optional[str] = ""
    headers: Dict[str, str] = Field(default_factory=dict)
    body: Optional[str] = ""

class AIGenerateTaskRequest(BaseModel):
    url: Optional[str] = ""
    prompt: Optional[str] = ""
    curl: Optional[str] = ""

class CookieUpdateRequest(BaseModel):
    cookie: str

class LoginRequest(BaseModel):
    password: str

class LegacyImportRequest(BaseModel):
    tasks: Optional[List[Dict[str, Any]]] = None
    config: Optional[Dict[str, Any]] = None
