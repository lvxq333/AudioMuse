"""SQLite 连接管理与应用迁移执行。

- 连接统一开启外键约束、WAL 日志与忙等待超时；
- run_migrations 幂等：schema_migrations 表记录已应用的迁移文件名，
  重复启动只应用缺失的迁移，不会重复执行已应用的 DDL；
- 迁移文件内 DDL 均使用 IF NOT EXISTS，即使中途失败，重跑也无副作用。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import logging
from pathlib import Path

import aiosqlite

from app.config import get_settings
from app.db.constants import now_utc_ms

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


async def connect(db_path: Path | None = None) -> aiosqlite.Connection:
    """创建并初始化一个 SQLite 连接。"""
    path = db_path or get_settings().database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA journal_mode = WAL")
    await conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@asynccontextmanager
async def db(db_path: Path | None = None) -> AsyncGenerator[aiosqlite.Connection, None]:
    """提供连接的异步上下文（仅负责连接生命周期与关闭）。

    注意：aiosqlite 的 ``async with conn`` 并非事务语义（每次都会
    thread.start()，同连接第二次起抛 RuntimeError），需要事务时请使用
    显式 BEGIN / COMMIT / ROLLBACK（参见 app/db/repository.py）。
    """
    conn = await connect(db_path)
    try:
        yield conn
    finally:
        await conn.close()


async def run_migrations(db_path: Path | None = None) -> None:
    """按文件名顺序应用未执行的迁移（幂等，可安全重复调用）。"""
    conn = await connect(db_path)
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name       TEXT PRIMARY KEY,
                applied_at INTEGER NOT NULL
            )
            """
        )
        await conn.commit()
        cursor = await conn.execute("SELECT name FROM schema_migrations")
        applied = {row["name"] for row in await cursor.fetchall()}

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            await conn.executescript(sql)
            await conn.execute(
                "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                (path.name, now_utc_ms()),
            )
            await conn.commit()
            logger.info("已应用迁移 %s", path.name)
    finally:
        await conn.close()
