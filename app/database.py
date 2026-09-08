import json
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base
from app.config import DATABASE_URL, DEFAULT_CONFIG

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False}
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)

Base = declarative_base()

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

async def init_db():
    from app.models import SystemConfig, Task, TaskLog, Reward  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        def _migrate_schema(connection):
            cursor = connection.connection.cursor()
            cursor.execute("PRAGMA table_info(tasks)")
            columns = [row[1] for row in cursor.fetchall()]
            if columns:
                if "auth_mode" not in columns:
                    cursor.execute("ALTER TABLE tasks ADD COLUMN auth_mode VARCHAR(32) DEFAULT 'cookie'")
                if "account_config" not in columns:
                    cursor.execute("ALTER TABLE tasks ADD COLUMN account_config JSON DEFAULT '{}'")
            cursor.close()

        await conn.run_sync(_migrate_schema)

    # 预置默认系统配置
    async with AsyncSessionLocal() as session:
        for key, val in DEFAULT_CONFIG.items():
            existing = await session.get(SystemConfig, key)
            if not existing:
                cfg_item = SystemConfig(key=key, value=val)
                session.add(cfg_item)
        await session.commit()
