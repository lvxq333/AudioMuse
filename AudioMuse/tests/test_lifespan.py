"""服务生命周期（lifespan）测试：真实启动路径自动迁移后上传可用。

覆盖安全审查发现的 HIGH 项：此前 run_migrations 仅被测试直接调用，
生产启动从不建表；本用例通过 ``with TestClient(...)`` 触发 lifespan startup，
在完全空库前提下验证「启动自动建表 → 上传成功 → 数据落库」真实链路。
"""

import asyncio
import uuid

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.connection import db as db_ctx
from app.main import create_app


def test_lifespan_migrates_then_upload_succeeds(tmp_path, monkeypatch):
    """空库 + with TestClient 触发 lifespan → 自动建表 → 上传成功。"""
    monkeypatch.setenv("AUDIOMUSE_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()

    with TestClient(create_app()) as client:  # with 触发 lifespan startup
        resp = client.post("/v1/recordings", files={"file": ("a.wav", b"abc")})
        assert resp.status_code == 202, resp.text
        rid = resp.json()["data"]["recording_id"]
        assert uuid.UUID(rid)  # 上传成功且返回合法 recording_id

        # 数据确实落库（未经测试手工迁移，证明是 lifespan 建的表）
        async def _count():
            async with db_ctx(get_settings().database_path) as conn:
                cur = await conn.execute("SELECT COUNT(*) AS n FROM recordings")
                row = await cur.fetchone()
                return int(row["n"])

        assert asyncio.run(_count()) == 1
