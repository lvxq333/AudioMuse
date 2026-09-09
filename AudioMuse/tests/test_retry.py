"""验证失败任务手动重试、并发冲突和旧轮次写回保护。

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
    monkeypatch.setenv("AUDIOMUSE_AUTO_RETRY_MAX_RETRIES", "3")
    monkeypatch.setenv("AUDIOMUSE_AUTO_RETRY_BASE_DELAY_SECONDS", "0")
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


# 重试成功

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


# 非法请求与状态冲突

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


# 删除中的录音不可重试

def test_retry_rejects_failed_delete_then_delete_can_finish(client, monkeypatch):
    """删除失败不能重新排队；保留全部任务字段，仍可再次删除完成清理。"""
    import app.api.recordings as recordings_api

    tid = _make_failed(client)
    settings = get_settings()

    async def snapshot():
        async with db_ctx(settings.database_path) as conn:
            cur = await conn.execute("SELECT * FROM tasks WHERE id=?", (tid,))
            task = dict(await cur.fetchone())
            cur = await conn.execute(
                "SELECT * FROM recordings WHERE id=?", (task["recording_id"],)
            )
            return task, dict(await cur.fetchone())

    before, recording = asyncio.run(snapshot())
    rid = recording["id"]
    file_path = settings.data_dir / recording["storage_path"]
    assert file_path.exists()
    real_remove = recordings_api.remove_file_with_retry

    async def cannot_remove(path, **kwargs):
        return False

    monkeypatch.setattr(recordings_api, "remove_file_with_retry", cannot_remove)
    deletion = client.delete(f"/v1/recordings/{rid}")
    assert deletion.status_code == 500
    assert deletion.json()["error"]["code"] == "RECORDING_DELETE_FAILED"
    assert asyncio.run(snapshot())[1]["lifecycle"] == "deleting"

    retry = client.post(f"/v1/tasks/{tid}/retry")
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "CONFLICT"
    assert "删除" in retry.json()["error"]["message"]
    after, recording = asyncio.run(snapshot())
    assert after == before  # 轮次、错误、结果和时间戳均不应被清理或更新
    assert recording["lifecycle"] == "deleting"
    assert file_path.exists()

    monkeypatch.setattr(recordings_api, "remove_file_with_retry", real_remove)
    assert client.delete(f"/v1/recordings/{rid}").status_code == 204
    assert not file_path.exists()
    assert client.get(f"/v1/recordings/{rid}").status_code == 404
    assert client.get(f"/v1/tasks/{tid}").status_code == 404


def test_retry_rechecks_lifecycle_after_api_lookup(client, monkeypatch):
    """在接口读取 failed 后才开始删除，数据库条件更新仍须拒绝重试。"""
    import app.api.tasks as tasks_api
    from app.db.repository import mark_recording_deleting

    tid = _make_failed(client)
    original_lookup = tasks_api.get_task_by_id

    async def lookup_then_delete(conn, task_id):
        row = await original_lookup(conn, task_id)
        await mark_recording_deleting(conn, recording_id=row["recording_id"])
        return row

    with monkeypatch.context() as patch:
        patch.setattr(tasks_api, "get_task_by_id", lookup_then_delete)
        response = client.post(f"/v1/tasks/{tid}/retry")
    assert response.status_code == 409
    task = client.get(f"/v1/tasks/{tid}").json()["data"]
    assert task["status"] == "failed"
    assert task["attempt_no"] == 1
    assert task["error_code"] == "ASR_FAILED"


# 旧轮次写回失效

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
