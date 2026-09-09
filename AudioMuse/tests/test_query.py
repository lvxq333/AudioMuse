"""P0-06 查询接口测试（独立临时 data 目录与数据库）。"""

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """临时 data 目录 + 毫秒级 ASR、0 失败、LLM mock（自动处理到 done）。"""
    monkeypatch.setenv("AUDIOMUSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUDIOMUSE_LLM_API_KEY", "")
    monkeypatch.setenv("AUDIOMUSE_ASR_MIN_SECONDS", "0.001")
    monkeypatch.setenv("AUDIOMUSE_ASR_MAX_SECONDS", "0.002")
    monkeypatch.setenv("AUDIOMUSE_ASR_FAILURE_THRESHOLD", "0")
    get_settings.cache_clear()
    with TestClient(create_app()) as c:  # lifespan：迁移 + 消费者自动处理
        yield c


def _upload(client, filename="a.wav", content=b"data"):
    return client.post("/v1/recordings", files={"file": (filename, content)})


def _wait_status(client, task_id, status, tries=100):
    """轮询任务直至指定状态。"""
    for _ in range(tries):
        resp = client.get(f"/v1/tasks/{task_id}")
        assert resp.status_code == 200
        body = resp.json()["data"]
        if body["status"] == status:
            return body
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} 未达到状态 {status}")


# ---------- GET /v1/tasks/{task_id} ----------

def test_task_query_reaches_done_with_stage(client):
    r = _upload(client)
    tid = r.json()["data"]["task_id"]
    body = _wait_status(client, tid, "done")
    assert body["status"] == "done"
    assert body["attempt_no"] == 1
    assert body["recording_id"]
    assert body["finished_at"] is not None


def test_task_query_not_found_404(client):
    resp = client.get("/v1/tasks/00000000000000000000000000000000")
    assert resp.status_code == 404


def test_task_query_bad_id_400(client):
    resp = client.get("/v1/tasks/not-a-uuid")
    assert resp.status_code == 400


# ---------- GET /v1/recordings（列表） ----------

def test_list_pagination_and_desc_order(client):
    ids = [
        _upload(client, filename=f"{i}.wav").json()["data"]["recording_id"]
        for i in range(5)
    ]
    resp = client.get("/v1/recordings?page=1&page_size=2")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert data["total"] == 5
    assert [it["recording_id"] for it in data["items"]] == ids[-1:-3:-1]


def test_list_defaults_and_empty(client):
    resp = client.get("/v1/recordings")
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 0
    assert resp.json()["data"]["items"] == []


def test_list_bad_pagination_422(client):
    resp = client.get("/v1/recordings?page=0")
    assert resp.status_code == 422


# ---------- GET /v1/recordings/{id}（详情） ----------

def test_detail_includes_transcript_and_summary_when_done(client):
    r = _upload(client, filename="meeting.wav", content=b"hello world")
    rid = r.json()["data"]["recording_id"]
    tid = r.json()["data"]["task_id"]
    _wait_status(client, tid, "done")

    resp = client.get(f"/v1/recordings/{rid}")
    assert resp.status_code == 200
    d = resp.json()["data"]
    assert d["recording_id"] == rid
    assert d["status"] == "done"
    assert d["transcript"]
    assert isinstance(d["summary"], dict)
    assert {"summary", "key_points", "todos"} <= set(d["summary"])


def test_detail_not_found_404(client):
    resp = client.get("/v1/recordings/00000000000000000000000000000000")
    assert resp.status_code == 404


def test_detail_bad_id_400(client):
    resp = client.get("/v1/recordings/xyz")
    assert resp.status_code == 400
