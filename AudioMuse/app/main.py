"""FastAPI 应用入口。

启动方式（开发）::

    .venv/bin/uvicorn app.main:app --reload

或以仓库内脚本一键启动::

    scripts/start.sh
"""

from __future__ import annotations

import asyncio
import logging
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
from app.services.llm import build_llm_client
from app.services.pipeline import build_processor
from app.services.registry import TaskRegistry
from app.worker.consumer import run_consumers
from app.worker.lock import DataDirLock

logger = logging.getLogger(__name__)

__version__ = "0.1.0"


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """服务启动：迁移 → 独占锁 → 遗留清理 → 启动消费者；关闭时优雅停止。

        迁移失败、或数据目录已被其他进程占用（DataDirLockError）都会让
        服务启动失败（快速失败，避免带空库/多实例运行）。
        """
        await run_migrations(settings.database_path)

        # 单进程独占锁：同一数据目录只允许一个服务进程
        lock = DataDirLock(settings.data_dir)
        lock.acquire()  # 拿不到立即抛 DataDirLockError → 启动失败

        try:
            # 启动清理：上次进程中断的 transcribing/summarizing → failed
            async with db_ctx(settings.database_path) as conn:
                await mark_interrupted_tasks(conn)

            # P0-05：任务处理注册表 + 处理器 + N 个消费者
            registry = TaskRegistry()
            llm_client = build_llm_client(settings)
            processor = build_processor(
                db_path=settings.database_path,
                llm_client=llm_client,
                asr_params={
                    "min_seconds": settings.asr_min_seconds,
                    "max_seconds": settings.asr_max_seconds,
                    "failure_threshold": settings.asr_failure_threshold,
                },
            )
            stop = asyncio.Event()
            app.state.registry = registry   # 供未来 stop/删除接口访问
            app.state.processor = processor

            consumers_task = asyncio.create_task(
                run_consumers(
                    settings.max_concurrency,
                    db_path=settings.database_path,
                    processor=processor,
                    registry=registry,
                    stop=stop,
                ),
                name="consumers",
            )

            try:
                yield
            finally:
                # 关闭：先不再领取（stop），等待进行中任务自然完成；
                # 超时则强停全部进行中任务（写回 PROCESSING_CANCELLED）
                stop.set()
                try:
                    await asyncio.wait_for(consumers_task, timeout=10)
                except asyncio.TimeoutError:
                    logger.warning("消费者未在限时内退出，强停进行中任务")
                    registry.cancel_all()
                    await asyncio.wait_for(consumers_task, timeout=5)
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
