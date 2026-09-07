"""统一的成功响应结构。

与 app/core/errors.py 的错误结构对称，成功响应固定为::

    {"code": "OK", "message": "ok", "data": {...}, "request_id": "..."}

业务接口通过 data 携带负载；data 内容可按接口自定义，例如上传接口::

    ok(data={"recording_id": ..., "task_id": ..., "status": "pending"},
       status_code=202)

request_id 由全局中间件注入请求状态；传入 None 时自动生成。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi.responses import JSONResponse


def ok(
    data: Any = None,
    *,
    code: str = "OK",
    message: str = "ok",
    status_code: int = 200,
    request_id: str | None = None,
) -> JSONResponse:
    """构造统一成功响应。"""
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "data": data,
            "request_id": request_id or uuid.uuid4().hex,
        },
    )
