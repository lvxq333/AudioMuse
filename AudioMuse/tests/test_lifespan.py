"""服务生命周期（lifespan）测试：真实启动路径自动迁移并自动处理任务。

测试强制 LLM 走本地 mock（不连真实 API）；ASR 调至毫秒级且 0 失败率，
保证端到端用例快且确定。真实 LLM 验证请用 scripts/smoke_llm.py。
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.connection import db as db_ctx
from app.db.constants import TaskStatus
from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """临时 data 目录 + 毫秒级 ASR、0 失败、LLM mock。"""
    monkeypatch.setenv("AUDIOMUSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUDIOMUSE_LLM_API_KEY", "")  # 强制 mock，不连真实 LLM
    monkeypatch.setenv("AUDIOMUSE_ASR_MIN_SECONDS", "0.001")
    monkeypatch.setenv("AUDIOMUSE_ASR_MAX_SECONDS", "0.002")
    monkeypatch.setenv("AUDIOMUSE_ASR_FAILURE_THRESHOLD", "0")
    get_settings.cache_clear()
    return get_settings(), tmp_path


def test_lifespan_migrates_then_upload_succeeds(client):
    """空库 + with TestClient 触发 lifespan → 自动建表 → 上传成功。"""
    settings, _ = client
    with TestClient(create_app()) as c:  # 触发 lifespan：迁移 + 消费者
        resp = c.post("/v1/recordings", files={"file": ("a.wav", b"abc")})
        assert resp.status_code == 202, resp.text
        rid = resp.json()["data"]["recording_id"]
        assert uuid.UUID(rid)


def test_lifespan_auto_processes_upload_to_done(client):
    """上传后不手动干预：消费者自动把任务推进到 done 并落结果。"""
    settings, _ = client
    with TestClient(create_app()) as c:
        resp = c.post("/v1/recordings", files={"file": ("a.wav", b"abc")})
        tid = resp.json()["data"]["task_id"]

        # 轮询等待任务被消费者处理完成（ASR 毫秒级 + mock LLM）
        async def _wait_done():
            for _ in range(200):
                async with db_ctx(settings.database_path) as conn:
                    cur = await conn.execute(
                        "SELECT status, transcript, summary_json"
                        " FROM tasks WHERE id=?",
                        (tid,),
                    )
                    row = await cur.fetchone()
                if row and row["status"] == TaskStatus.DONE.value:
                    return dict(row)
                await asyncio.sleep(0.02)
            raise AssertionError("任务未在限时内被处理为 done")

        row = asyncio.run(_wait_done())
        assert row["transcript"]
        assert row["summary_json"]  # mock LLM 输出结构合法且已落库
