"""验证录音删除、关联数据清理及删除失败续扫行为。

通过真实链路（lifespan + 消费者，ASR 100% 失败制造稳定 failed 态）
构造录音再删除，验证：删除后 DB 行/任务/文件均消失、列表排除、
文件删除失败不误报成功、再次 DELETE 可续扫清理。
"""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.connection import db as db_ctx
from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """ASR 100% 失败 + LLM mock：任务会自动流转 failed。"""
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


def _upload(client, filename="a.wav", content=b"data"):
    return client.post("/v1/recordings", files={"file": (filename, content)})


def _wait_status(client, task_id, status, tries=100):
    for _ in range(tries):
        resp = client.get(f"/v1/tasks/{task_id}")
        assert resp.status_code == 200
        body = resp.json()["data"]
        if body["status"] == status:
            return body
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} 未达到状态 {status}")


def _db_counts(recording_id):
    """返回 (recordings 行数, tasks 行数, 磁盘文件是否存在)。"""
    settings = get_settings()

    async def _do():
        async with db_ctx(settings.database_path) as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) AS n FROM recordings WHERE id=?", (recording_id,)
            )
            rc = int((await cur.fetchone())["n"])
            cur = await conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE recording_id=?", (recording_id,)
            )
            tc = int((await cur.fetchone())["n"])
            cur = await conn.execute(
                "SELECT storage_path FROM recordings WHERE id=?", (recording_id,)
            )
            row = await cur.fetchone()
            storage = row["storage_path"] if row else None
            exists = bool(storage) and (settings.data_dir / storage).exists()
        return rc, tc, exists

    return asyncio.run(_do())


def _make_failed(client):
    """上传并等待任务变 failed，返回 (rid, tid)。"""
    data = _upload(client).json()["data"]
    tid = data["task_id"]
    _wait_status(client, tid, "failed")
    rid = client.get(f"/v1/tasks/{tid}").json()["data"]["recording_id"]
    return rid, tid


# 成功删除

def test_delete_failed_recording_removes_all(client):
    rid, tid = _make_failed(client)
    resp = client.delete(f"/v1/recordings/{rid}")
    assert resp.status_code == 204
    rc, tc, exists = _db_counts(rid)
    assert (rc, tc, exists) == (0, 0, False)
    # 删除后再查详情：404
    assert client.get(f"/v1/recordings/{rid}").status_code == 404


def test_delete_just_uploaded_recording(client):
    """刚上传（可能 pending 或已被消费者领为 transcribing）即可删除。"""
    data = _upload(client).json()["data"]
    rid, tid = data["recording_id"], data["task_id"]
    resp = client.delete(f"/v1/recordings/{rid}")
    assert resp.status_code == 204, resp.text
    rc, tc, exists = _db_counts(rid)
    assert (rc, tc, exists) == (0, 0, False)


def test_delete_not_found_404(client):
    resp = client.delete("/v1/recordings/00000000000000000000000000000000")
    assert resp.status_code == 404


def test_delete_bad_id_400(client):
    resp = client.delete("/v1/recordings/xyz")
    assert resp.status_code == 400


def test_list_excludes_deleted_recording(client):
    rid, tid = _make_failed(client)
    client.delete(f"/v1/recordings/{rid}")
    items = client.get("/v1/recordings").json()["data"]["items"]
    assert all(it["recording_id"] != rid for it in items)


# 文件删除失败后续扫

def test_file_delete_failure_keeps_row_then_retry_succeeds(client, monkeypatch):
    """第一次文件删除失败 → 500 不删行；再次 DELETE 续扫成功 → 204。"""
    import app.api.recordings as recordings_api

    rid, tid = _make_failed(client)
    real = recordings_api.remove_file_with_retry
    calls = {"n": 0}

    async def flaky(path, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return False  # 模拟第一次文件删除失败
        return await real(path, **kwargs)

    monkeypatch.setattr(recordings_api, "remove_file_with_retry", flaky)
    resp = client.delete(f"/v1/recordings/{rid}")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "RECORDING_DELETE_FAILED"
    rc, tc, exists = _db_counts(rid)
    assert rc == 1   # DB 行保留（deleting），不误报成功
    assert exists    # 文件也还在

    # 恢复真实删除 → 再次 DELETE 续扫成功
    monkeypatch.setattr(recordings_api, "remove_file_with_retry", real)
    resp = client.delete(f"/v1/recordings/{rid}")
    assert resp.status_code == 204
    rc, tc, exists = _db_counts(rid)
    assert (rc, tc, exists) == (0, 0, False)
