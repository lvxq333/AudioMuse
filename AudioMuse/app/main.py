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
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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
FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """管理数据库迁移、进程锁、遗留任务清理和消费者生命周期。

        迁移失败、或数据目录已被其他进程占用（DataDirLockError）都会让
        服务启动失败（快速失败，避免带空库/多实例运行）。
        """
        # 流程 1：执行数据库迁移，保证运行所需表结构可用。
        await run_migrations(settings.database_path)

        # 流程 2：获取数据目录独占锁，防止同一目录启动多个消费者进程。
        lock = DataDirLock(settings.data_dir)
        lock.acquire()  # 获取失败时立即终止启动，避免多个进程共用同一 SQLite 文件。

        try:
            # 流程 3：将上次中断的处理中任务标记为可手动重试的失败状态。
            async with db_ctx(settings.database_path) as conn:
                await mark_interrupted_tasks(conn)

            # 流程 4：构造任务注册表、处理器和指定数量的消费者。
            registry = TaskRegistry()
            llm_client = build_llm_client(settings)
            processor = build_processor(
                db_path=settings.database_path,
                llm_client=llm_client,
                asr_params={
                    "data_dir": settings.data_dir,
                    "api_key": settings.asr_api_key,
                    "base_url": settings.asr_base_url,
                    "model": settings.asr_model,
                    "timeout_seconds": settings.asr_timeout_seconds,
                    "language": settings.asr_language,
                    "prompt": settings.asr_prompt,
                    "min_seconds": settings.asr_min_seconds,
                    "max_seconds": settings.asr_max_seconds,
                    "failure_threshold": settings.asr_failure_threshold,
                },
                retry_max_retries=settings.auto_retry_max_retries,
                retry_base_delay=settings.auto_retry_base_delay_seconds,
            )
            stop = asyncio.Event()
            app.state.registry = registry  # 供删除接口取消正在执行的单个任务。
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
                # 流程 5：停止领取新任务并等待在途任务完成，超时后统一取消。
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
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="frontend-assets")

    @app.get("/", include_in_schema=False)
    async def frontend():
        """AudioMuse Web 工作台。"""
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/healthz", tags=["meta"])
    async def healthz(request: Request):
        """存活探针。"""
        return ok(
            data={"status": "ok", "version": __version__},
            request_id=getattr(request.state, "request_id", None),
        )

    return app


app = create_app()
