"""上传幂等：键复用、内容冲突、并发唯一性及文件清理。"""

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.connection import db as db_ctx
from app.db.connection import run_migrations
from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIOMUSE_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    asyncio.run(run_migrations(get_settings().database_path))
    return TestClient(create_app())


def _upload(client, *, key=None, content=b"same-audio", filename="a.wav"):
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post(
        "/v1/recordings",
        files={"file": (filename, content)},
        headers=headers,
    )


def _rows(sql, params=()):
    async def query():
        async with db_ctx(get_settings().database_path) as conn:
            cursor = await conn.execute(sql, params)
            return [dict(row) for row in await cursor.fetchall()]

    return asyncio.run(query())


def test_same_key_and_content_reuses_existing_recording(client):
    first = _upload(client, key="upload-001")
    replay = _upload(client, key="upload-001")

    assert first.status_code == 202
    assert replay.status_code == 200
    assert replay.json()["data"] == first.json()["data"]
    rows = _rows(
        "SELECT id, idempotency_key, file_sha256 FROM recordings"
    )
    assert rows == [{
        "id": first.json()["data"]["recording_id"],
        "idempotency_key": "upload-001",
        "file_sha256": hashlib.sha256(b"same-audio").hexdigest(),
    }]
    assert len(_rows("SELECT id FROM tasks")) == 1
    assert len(list(get_settings().recordings_dir.iterdir())) == 1


def test_same_key_with_different_content_returns_409(client):
    first = _upload(client, key="upload-002", content=b"first")
    conflict = _upload(client, key="upload-002", content=b"second")

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CONFLICT"
    assert "不同的文件" in conflict.json()["error"]["message"]
    assert len(_rows("SELECT id FROM recordings")) == 1
    assert len(_rows("SELECT id FROM tasks")) == 1
    files = list(get_settings().recordings_dir.iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == b"first"


def test_without_key_still_creates_independent_recordings(client):
    first = _upload(client)
    second = _upload(client)

    assert first.status_code == second.status_code == 202
    assert first.json()["data"]["recording_id"] != second.json()["data"]["recording_id"]
    assert len(_rows("SELECT id FROM recordings")) == 2
    assert len(list(get_settings().recordings_dir.iterdir())) == 2


def test_concurrent_same_key_creates_exactly_one_recording(client):
    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(
            lambda _: _upload(client, key="concurrent-key"), range(20)
        ))

    codes = [response.status_code for response in responses]
    assert codes.count(202) == 1
    assert codes.count(200) == 19
    ids = {
        (response.json()["data"]["recording_id"],
         response.json()["data"]["task_id"])
        for response in responses
    }
    assert len(ids) == 1
    assert len(_rows("SELECT id FROM recordings")) == 1
    assert len(_rows("SELECT id FROM tasks")) == 1
    assert len(list(get_settings().recordings_dir.iterdir())) == 1


@pytest.mark.parametrize("key", ["   ", "x" * 129])
def test_invalid_idempotency_key_returns_400_without_writing_file(client, key):
    response = _upload(client, key=key)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"
    assert _rows("SELECT id FROM recordings") == []
    assert not get_settings().recordings_dir.exists()


def test_same_key_is_normalized_by_trimming_outer_whitespace(client):
    first = _upload(client, key="  normalized-key  ")
    replay = _upload(client, key="normalized-key")
    assert first.status_code == 202
    assert replay.status_code == 200
    assert replay.json()["data"] == first.json()["data"]


def test_key_for_deleting_recording_returns_409_and_cleans_new_file(client):
    first = _upload(client, key="deleting-key")
    recording_id = first.json()["data"]["recording_id"]

    async def mark_deleting():
        async with db_ctx(get_settings().database_path) as conn:
            await conn.execute(
                "UPDATE recordings SET lifecycle='deleting' WHERE id=?",
                (recording_id,),
            )
            await conn.commit()

    asyncio.run(mark_deleting())
    replay = _upload(client, key="deleting-key")
    assert replay.status_code == 409
    assert "正在删除" in replay.json()["error"]["message"]
    assert len(_rows("SELECT id FROM recordings")) == 1
    assert len(list(get_settings().recordings_dir.iterdir())) == 1
