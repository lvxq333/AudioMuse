"""FastAPI 应用入口。

启动方式（开发）::

    .venv/bin/uvicorn app.main:app --reload

或以仓库内脚本一键启动::

    scripts/start.sh
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.api.router import api_router
from app.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import setup_logging
from app.core.request_id import RequestIDMiddleware
from app.core.responses import ok
from app.db.connection import db as db_ctx
from app.db.connection import run_migrations
from app.db.repository import mark_interrupted_tasks
from app.worker.lock import DataDirLock

__version__ = "0.1.0"


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """服务启动流程：迁移 → 数据目录独占锁 → 遗留任务清理。

        迁移失败、或数据目录已被其他进程占用（DataDirLockError）都会让
        服务启动失败（快速失败，避免带空库/多实例运行）。
        P0-05 将在此处追加：启动 count 个消费者（processor=ASR+LLM）
        并在关闭时置位 stop、等待进行中任务处理完成。
        """
        await run_migrations(settings.database_path)

        # P0-04：单进程独占锁——同一数据目录只允许一个服务进程
        lock = DataDirLock(settings.data_dir)
        lock.acquire()  # 拿不到立即抛 DataDirLockError → 启动失败

        try:
            # P0-04：启动清理——上次进程中断的 transcribing/summarizing → failed
            async with db_ctx(settings.database_path) as conn:
                await mark_interrupted_tasks(conn)
            yield
        finally:
            lock.release()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        debug=settings.debug,
        lifespan=lifespan,
    )
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router)

    @app.get("/healthz", tags=["meta"])
    async def healthz(request: Request):
        """存活探针。"""
        return ok(
            data={"status": "ok", "version": __version__},
            request_id=getattr(request.state, "request_id", None),
        )

    return app


app = create_app()
