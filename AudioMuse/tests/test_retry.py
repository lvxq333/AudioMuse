"""P0-07 失败任务重试测试。

主 fixture 让 ASR 100% 失败 → 上传的任务自动流转到 failed，
从而通过真实链路制造可重试状态；再用 API/DB 断言各分支。
"""

import asyncio
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.connection import db as db_ctx
from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """ASR 100% 失败 + LLM mock：任务上传后自动变成 failed。"""
    monkeypatch.setenv("AUDIOMUSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUDIOMUSE_LLM_API_KEY", "")
    monkeypatch.setenv("AUDIOMUSE_ASR_MIN_SECONDS", "0.001")
    monkeypatch.setenv("AUDIOMUSE_ASR_MAX_SECONDS", "0.002")
    monkeypatch.setenv("AUDIOMUSE_ASR_FAILURE_THRESHOLD", "1.0")
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        yield c


def _upload(client):
    return client.post("/v1/recordings", files={"file": ("a.wav", b"data")})


def _wait_status(client, task_id, status, tries=100):
    for _ in range(tries):
        resp = client.get(f"/v1/tasks/{task_id}")
        assert resp.status_code == 200
        body = resp.json()["data"]
        if body["status"] == status:
            return body
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} 未达到状态 {status}")


def _set_task_status(task_id, status):
    """直改 DB 状态（模拟其它阶段），返回受影响行数。"""
    settings = get_settings()
    from app.db.constants import now_utc_ms

    async def _do():
        async with db_ctx(settings.database_path) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await conn.execute(
                    "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                    (status, now_utc_ms(), task_id),
                )
                await conn.commit()
                return cur.rowcount
            except Exception:
                await conn.rollback()
                raise

    return asyncio.run(_do())


def _make_failed(client):
    """上传并等待任务变 failed，返回 task_id。"""
    tid = _upload(client).json()["data"]["task_id"]
    body = _wait_status(client, tid, "failed")
    assert body["error_code"] == "ASR_FAILED"
    return tid


# ---------- 成功路径 ----------

def test_retry_accepts_failed_task(client):
    tid = _make_failed(client)
    resp = client.post(f"/v1/tasks/{tid}/retry")
    assert resp.status_code == 202, resp.text
    data = resp.json()["data"]
    assert data["status"] == "pending"
    assert data["attempt_no"] == 2
    # 消费者自动重新处理并再次失败（ASR 仍 100% 失败）→ attempt 2
    body = _wait_status(client, tid, "failed")
    assert body["attempt_no"] == 2


def test_retry_clears_previous_error_and_results(client):
    tid = _make_failed(client)
    resp = client.post(f"/v1/tasks/{tid}/retry")
    assert resp.status_code == 202
    # 重试后立刻查询：error_code 已清空、状态 pending、attempt=2
    body = client.get(f"/v1/tasks/{tid}").json()["data"]
    assert body["status"] == "pending"
    assert body["attempt_no"] == 2
    assert body["error_code"] is None
    assert body["error_message"] is None
    assert body["finished_at"] is None


# ---------- 非法/冲突 ----------

def test_retry_not_found_404(client):
    resp = client.post(
        "/v1/tasks/00000000000000000000000000000000/retry"
    )
    assert resp.status_code == 404


def test_retry_bad_id_400(client):
    resp = client.post("/v1/tasks/not-a-uuid/retry")
    assert resp.status_code == 400


def test_retry_non_failed_status_409(client):
    tid = _make_failed(client)
    for status in ("pending", "done"):
        _set_task_status(tid, status)
        resp = client.post(f"/v1/tasks/{tid}/retry")
        assert resp.status_code == 409, status
        assert resp.json()["error"]["code"] == "CONFLICT"


def test_concurrent_retries_only_one_succeeds(client):
    tid = _make_failed(client)
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(
            pool.map(lambda _: client.post(f"/v1/tasks/{tid}/retry"), range(20))
        )
    codes = [r.status_code for r in results]
    assert codes.count(202) == 1
    assert codes.count(409) == 19


# ---------- 旧轮次写回失效 ----------

def test_old_attempt_writeback_is_rejected(client):
    """旧轮次（attempt=1）的结果写回不得覆盖新一轮（attempt=2）。"""
    tid = _make_failed(client)              # failed, attempt=1
    resp = client.post(f"/v1/tasks/{tid}/retry")
    assert resp.status_code == 202          # 新一轮 pending, attempt=2
    settings = get_settings()

    from app.db.repository import fail_task
    from app.db.constants import now_utc_ms

    async def _old_writeback():
        async with db_ctx(settings.database_path) as conn:
            return await fail_task(
                conn, task_id=tid, attempt_no=1,
                error_code="ASR_FAILED", error_message="旧轮次迟到失败",
                now=now_utc_ms(),
            )

    assert asyncio.run(_old_writeback()) is False  # 被 attempt_no 条件拒绝
    # 任务仍处于 pending(attempt=2)，未被旧轮次覆盖
    body = client.get(f"/v1/tasks/{tid}").json()["data"]
    assert body["status"] == "pending"
    assert body["attempt_no"] == 2
