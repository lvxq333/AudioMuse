"""验证应用健康检查接口及统一响应结构。"""

from fastapi.testclient import TestClient

from app.main import create_app

client = TestClient(create_app())


def test_healthz_returns_unified_success() -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "OK"
    assert body["message"] == "ok"
    assert body["data"] == {"status": "ok", "version": "0.1.0"}
    assert "request_id" in body


def test_x_request_id_is_echoed() -> None:
    resp = client.get("/healthz", headers={"X-Request-ID": "req-abc"})
    assert resp.headers["x-request-id"] == "req-abc"
    assert resp.json()["request_id"] == "req-abc"


def test_unknown_v1_route_returns_error_shape() -> None:
    """真正未实现的路由返回统一错误结构（404），且带同一请求编号。"""
    resp = client.get("/v1/not-a-route")
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body
    assert {"code", "message", "request_id"} <= set(body["error"])
    assert resp.headers["x-request-id"] == body["error"]["request_id"]
