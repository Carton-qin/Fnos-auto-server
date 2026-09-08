from datetime import datetime
from sqlalchemy import Column, String, Integer, Boolean, Text, JSON, DateTime
from app.database import Base

class SystemConfig(Base):
    __tablename__ = "system_configs"

    key = Column(String(50), primary_key=True)
    value = Column(JSON, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Task(Base):
    __tablename__ = "tasks"

    id = Column(String(36), primary_key=True)
    name = Column(String(100), nullable=False)
    type = Column(String(30), default="checkin")  # checkin, ai_digest, uptime, custom_http
    enabled = Column(Boolean, default=True)
    schedule_type = Column(String(20), default="cron")  # cron, window, interval
    cron_expr = Column(String(50), nullable=True)
    window_start = Column(String(10), nullable=True)
    window_end = Column(String(10), nullable=True)
    jitter_mins = Column(Integer, default=0)
    retry_count = Column(Integer, default=1)
    retry_delay = Column(Integer, default=3)
    notify_on_success = Column(Boolean, default=False)
    notify_on_failure = Column(Boolean, default=True)
    ai_diagnose = Column(Boolean, default=True)
    use_headless_browser = Column(Boolean, default=False)  # 智能无头浏览器模式
    auth_mode = Column(String(30), default="cookie")  # 'cookie' 或 'playwright_account'
    account_config = Column(JSON, default=dict)  # 账号密码自动登录配置 (加密存储)
    params = Column(JSON, default=dict)
    stats = Column(JSON, default=dict)
    last_run = Column(String(30), nullable=True)
    last_status = Column(String(20), nullable=True)
    last_result = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class TaskLog(Base):
    __tablename__ = "task_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(36), index=True, nullable=True)
    task_name = Column(String(100), nullable=True)
    category = Column(String(20), default="task", index=True)  # task, system
    status = Column(String(20), nullable=False)  # success, fail, warning, info
    message = Column(Text, nullable=False)
    ai_diag = Column(Text, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    time = Column(String(30), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Reward(Base):
    __tablename__ = "rewards"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(36), index=True, nullable=True)
    task_name = Column(String(100), nullable=True)
    date = Column(String(10), index=True, nullable=False)  # YYYY-MM-DD
    amount = Column(Integer, nullable=False)
    unit = Column(String(20), default="点")
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
