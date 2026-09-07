"""请求编号中间件。

为每个 HTTP 请求读取 X-Request-ID 头（缺失则生成），
存入 scope state 供日志/错误/成功响应共用，并在响应头回写。
选用纯 ASGI 实现，避免后续 SSE 流式响应被 BaseHTTPMiddleware 缓冲。
"""

from __future__ import annotations

import uuid

from starlette.types import ASGIApp, Receive, Scope, Send


class RequestIDMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        for name, value in scope.get("headers") or []:
            if name == b"x-request-id":
                request_id = value.decode("latin-1")
                break

        scope.setdefault("state", {})["request_id"] = request_id
        rid_bytes = request_id.encode("latin-1")

        async def send_with_rid(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                if not any(k == b"x-request-id" for k, _ in headers):
                    headers.append((b"x-request-id", rid_bytes))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_rid)
