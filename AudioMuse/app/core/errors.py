"""统一的错误响应结构与异常处理。

对外错误响应固定为::

    {"error": {"code": "...", "message": "...", "request_id": "..."}}

业务代码抛出的异常均继承 :class:`AppError`；未捕获异常由 FastAPI
兜底处理器转换为 500，避免直接暴露内部细节。
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# 建议的 HTTP 状态码约定（与需求文档一致）：
# 400 参数/格式错误；404 资源不存在；409 状态冲突；
# 413 文件过大；415 扩展名不支持；422 统一为请求校验错误；500 内部错误


class AppError(Exception):
    """业务异常的基类。"""

    status_code: int = 500
    code: str = "INTERNAL_ERROR"
    message: str = "内部错误"

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        if message is not None:
            self.message = message
        if code is not None:
            self.code = code
        super().__init__(self.message)


class BadRequestError(AppError):
    status_code = 400
    code = "BAD_REQUEST"
    message = "请求参数错误"


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"
    message = "资源不存在"


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"
    message = "资源状态冲突，无法执行该操作"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "PAYLOAD_TOO_LARGE"
    message = "文件大小超出限制"


class UnsupportedMediaTypeError(AppError):
    status_code = 415
    code = "UNSUPPORTED_MEDIA_TYPE"
    message = "不支持的文件类型"


def _error_payload(code: str, message: str, request_id: str) -> dict:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def _request_id(request: Request) -> str:
    """优先取中间件注入的请求编号，缺省时回退新生成。"""
    return getattr(request.state, "request_id", None) or uuid.uuid4().hex


def register_exception_handlers(app: FastAPI) -> None:
    """把异常处理器注册到应用上。"""

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(
                exc.code,
                exc.message,
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_error_payload(
                "VALIDATION_ERROR",
                "请求校验失败，请检查参数",
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(
                "HTTP_ERROR", str(exc.detail), _request_id(request)
            ),
        )
