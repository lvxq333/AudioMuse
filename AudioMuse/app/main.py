"""FastAPI 应用入口。

启动方式（开发）::

    .venv/bin/uvicorn app.main:app --reload

或以仓库内脚本一键启动::

    scripts/start.sh
"""

from __future__ import annotations

from fastapi import FastAPI, Request

from app.api.router import api_router
from app.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import setup_logging
from app.core.request_id import RequestIDMiddleware
from app.core.responses import ok

__version__ = "0.1.0"


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        debug=settings.debug,
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
