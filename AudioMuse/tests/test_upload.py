"""P0-03 上传接口测试（独立临时 data 目录与数据库，不碰真实 data/）。"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.connection import db as db_ctx
from app.db.connection import run_migrations
from app.main import create_app

MAX_BYTES = 50 * 1024 * 1024


@pytest.fixture
def client(tmp_path, monkeypatch):
    """指向临时 data 目录：独立 DB + 独立录音目录，测试间互不影响。"""
    monkeypatch.setenv("AUDIOMUSE_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    settings = get_settings()
    asyncio.run(run_migrations(settings.database_path))
    return TestClient(create_app())


def _fetch_rows(settings, sql, params=()):
    async def _do():
        async with db_ctx(settings.database_path) as conn:
            cur = await conn.execute(sql, params)
            return [dict(row) for row in await cur.fetchall()]

    return asyncio.run(_do())


def _upload(client, filename="a.wav", content=b"fake-audio-data"):
    return client.post("/v1/recordings", files={"file": (filename, content)})


def test_upload_accepts_all_supported_extensions(client):
    """wav/mp3/m4a/aac 四种扩展名均返回 202 与 pending 任务。"""
    for ext in ("wav", "mp3", "m4a", "aac"):
        resp = _upload(client, filename=f"rec.{ext}")
        assert resp.status_code == 202, resp.text
        data = resp.json()["data"]
        assert data["status"] == "pending"
        assert uuid.UUID(data["recording_id"])  # 非法 UUID 会抛 ValueError
        assert uuid.UUID(data["task_id"])


def test_upload_uppercase_extension_accepted(client):
    resp = _upload(client, filename="REC.WAV")
    assert resp.status_code == 202


def test_upload_missing_file_returns_400(client):
    resp = client.post("/v1/recordings")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BAD_REQUEST"


def test_upload_zero_byte_file_returns_400(client):
    resp = _upload(client, content=b"")
    assert resp.status_code == 400


def test_upload_unsupported_extension_returns_415(client):
    for name in ("a.txt", "b.exe", "noext"):
        resp = _upload(client, filename=name)
        assert resp.status_code == 415, name


def test_upload_exactly_at_max_size_ok(client):
    """恰好 50 MiB 允许。"""
    resp = _upload(client, content=b"x" * MAX_BYTES)
    assert resp.status_code == 202


def test_upload_over_max_size_returns_413(client):
    """超 1 字节即拒绝，并清理半成品文件。"""
    resp = _upload(client, content=b"x" * (MAX_BYTES + 1))
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_upload_persists_record_task_and_file(client, tmp_path):
    """成功后：DB 有录音行 + pending 任务行，磁盘存在文件且内容一致。"""
    settings = get_settings()
    resp = _upload(client, filename="meeting.wav", content=b"abc")
    assert resp.status_code == 202
    rid = resp.json()["data"]["recording_id"]

    recordings = _fetch_rows(
        settings,
        "SELECT id, original_filename, extension, size_bytes, storage_path, lifecycle"
        " FROM recordings WHERE id=?",
        (rid,),
    )
    assert len(recordings) == 1
    row = recordings[0]
    assert row["original_filename"] == "meeting.wav"
    assert row["extension"] == "wav"
    assert row["size_bytes"] == 3
    assert row["lifecycle"] == "active"

    tasks = _fetch_rows(
        settings,
        "SELECT recording_id, status, attempt_no FROM tasks WHERE recording_id=?",
        (rid,),
    )
    assert len(tasks) == 1
    assert tasks[0]["status"] == "pending"
    assert tasks[0]["attempt_no"] == 1

    file_path = tmp_path / row["storage_path"]
    assert file_path.is_file()
    assert file_path.read_bytes() == b"abc"


def test_duplicate_uploads_create_two_recordings(client):
    """同一文件重复上传生成两条独立录音（不做去重，属加分项）。"""
    r1 = _upload(client, filename="same.wav", content=b"data").json()["data"]
    r2 = _upload(client, filename="same.wav", content=b"data").json()["data"]
    assert r1["recording_id"] != r2["recording_id"]
    settings = get_settings()
    rows = _fetch_rows(settings, "SELECT id FROM recordings")
    assert len(rows) == 2


async def _boom(*args, **kwargs):
    raise RuntimeError("db down")


def test_upload_db_failure_returns_clear_error_and_no_orphan(
    client, monkeypatch, tmp_path
):
    """DB 写入失败：返回 500 + 明确文案（不裸报内部错误），且不残留孤儿文件。"""
    import app.api.recordings as recordings_api

    monkeypatch.setattr(recordings_api, "create_recording_and_task", _boom)
    resp = _upload(client, filename="a.wav", content=b"abc")
    assert resp.status_code == 500
    err = resp.json()["error"]
    assert err["code"] == "UPLOAD_FAILED"
    assert "请稍后重试" in err["message"]

    recordings_dir = get_settings().data_dir / "recordings"
    leftovers = (
        [p.name for p in recordings_dir.iterdir()] if recordings_dir.exists() else []
    )
    assert leftovers == []  # 正式文件与临时文件均已被清理
