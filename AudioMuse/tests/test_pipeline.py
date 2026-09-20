"""验证摘要结构、处理流水线错误分类及任务取消行为。

全部使用确定性替身：LLM 走 mock 分支或 httpx.MockTransport（不出网），
ASR 走毫秒级 + 注入假 sleep；不依赖真实网络与随机源。
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.db.connection import db as db_ctx
from app.db.connection import run_migrations
from app.db.constants import ErrorCode, TaskStatus, now_utc_ms
from app.db.repository import claim_next_task, create_recording_and_task
from app.services.llm import LlmClient, LlmInvalidOutput, parse_and_validate
from app.services.pipeline import build_processor
from app.services.registry import TaskRegistry
from app.worker.consumer import consume_loop

# 测试辅助函数

_FAST_ASR = {"min_seconds": 0.001, "max_seconds": 0.002,
             "failure_threshold": 0.0}


async def _seed(db_path: Path, task_id="t1", recording_id="r1"):
    await run_migrations(db_path)
    now = now_utc_ms()
    async with db_ctx(db_path) as conn:
        await create_recording_and_task(
            conn, recording_id=recording_id, task_id=task_id,
            original_filename="a.wav",
            storage_relpath=f"recordings/{recording_id}.wav",
            extension="wav", size_bytes=10, now=now,
        )
    return task_id


async def _claim_first(db_path):
    """领取第一个 pending 任务（模拟消费者 claim，返回 transcribing 任务）。"""
    async with db_ctx(db_path) as conn:
        return await claim_next_task(conn)


async def _get_task(db_path, task_id):
    async with db_ctx(db_path) as conn:
        cur = await conn.execute(
            "SELECT status, error_code, transcript, summary_json"
            " FROM tasks WHERE id=?",
            (task_id,),
        )
        return dict(await cur.fetchone())


def _fake_sleep(duration):
    """假 sleep：不真实等待（配合毫秒级 ASR 参数）。"""
    return asyncio.sleep(0)


def _mock_client():
    """无 Key 的 LlmClient：走本地 mock 摘要分支。"""
    return LlmClient(api_key="", base_url="", model="", timeout_seconds=1)


# LLM 输出结构校验

def test_parse_and_validate_ok():
    result = parse_and_validate(
        '{"summary": "一句话", "key_points": ["a"], "todos": []}'
    )
    assert result.summary == "一句话"
    assert result.key_points == ["a"]
    assert result.todos == []


def test_parse_and_validate_rejects_non_string_summary():
    with pytest.raises(LlmInvalidOutput):
        parse_and_validate('{"summary": 123, "key_points": []}')


def test_parse_and_validate_rejects_non_json():
    with pytest.raises(LlmInvalidOutput):
        parse_and_validate("这不是 JSON")


def test_parse_and_validate_tolerates_code_fence():
    r = parse_and_validate('```json\n{"summary": "s", "key_points": []}\n```')
    assert r.summary == "s"


# 处理成功

async def test_pipeline_success_to_done(tmp_path):
    db_path = tmp_path / "p.db"
    await _seed(db_path)
    task = await _claim_first(db_path)
    assert task is not None
    processor = build_processor(
        db_path=db_path, llm_client=_mock_client(),
        asr_params=dict(_FAST_ASR, sleep=_fake_sleep),
    )
    await processor(task)

    row = await _get_task(db_path, task["id"])
    assert row["status"] == TaskStatus.DONE.value
    assert row["transcript"]
    assert row["summary_json"]


# ASR 失败

async def test_pipeline_asr_failure_marks_failed_and_skips_llm(tmp_path):
    db_path = tmp_path / "p.db"
    await _seed(db_path)
    task = await _claim_first(db_path)
    assert task is not None

    class _CountingLlm(LlmClient):
        calls = 0

        async def summarize(self, transcript, *, task_id):
            type(self).calls += 1
            raise AssertionError("ASR 失败时不应调用 LLM")

    llm = _CountingLlm(api_key="", base_url="", model="", timeout_seconds=1)
    processor = build_processor(
        db_path=db_path, llm_client=llm,
        asr_params=dict(_FAST_ASR, failure_threshold=1.0, sleep=_fake_sleep),
        retry_sleep=_fake_sleep,
    )
    await processor(task)

    row = await _get_task(db_path, task["id"])
    assert row["status"] == TaskStatus.FAILED.value
    assert row["error_code"] == "ASR_FAILED"
    assert _CountingLlm.calls == 0
    assert row["transcript"] is None


# LLM 超时与非法输出

async def _run_with_llm_output(tmp_path, handler):
    db_path = tmp_path / "p.db"
    await _seed(db_path)
    task = await _claim_first(db_path)
    assert task is not None
    llm = LlmClient(
        api_key="k", base_url="https://llm.test/v1", model="m",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler=handler),
    )
    processor = build_processor(
        db_path=db_path, llm_client=llm,
        asr_params=dict(_FAST_ASR, sleep=_fake_sleep),
        retry_sleep=_fake_sleep,
    )
    await processor(task)
    return await _get_task(db_path, task["id"])


async def test_pipeline_llm_invalid_output(tmp_path):
    async def handler(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "非 JSON"}}]}
        )

    row = await _run_with_llm_output(tmp_path, handler)
    assert row["status"] == TaskStatus.FAILED.value
    assert row["error_code"] == "LLM_INVALID_OUTPUT"


async def test_pipeline_llm_timeout(tmp_path):
    # MockTransport 不应用 httpx 的超时，直接抛 ReadTimeout 模拟超时
    async def handler(request):
        raise httpx.ReadTimeout("模拟 LLM 超时")

    row = await _run_with_llm_output(tmp_path, handler)
    assert row["status"] == TaskStatus.FAILED.value
    assert row["error_code"] == "LLM_TIMEOUT"


@pytest.mark.parametrize("body", [
    "<html>upstream error</html>",
    "null", "[]",
    "{}",
    '{"choices": null}',
    '{"choices": []}',
    '{"choices": {}}',
    '{"choices": [null]}',
    '{"choices": [{}]}',
    '{"choices": [{"message": null}]}',
    '{"choices": [{"message": []}]}',
    '{"choices": [{"message": {}}]}',
] + [
    json.dumps({"choices": [{"message": {"content": value}}]})
    for value in (None, 123, [], {}, "", "   ")
])
async def test_pipeline_malformed_llm_response_finishes_failed(tmp_path, body):
    row = await _run_with_llm_output(
        tmp_path, lambda request: httpx.Response(200, text=body)
    )
    assert row["status"] == "failed"
    assert row["error_code"] == "LLM_INVALID_OUTPUT"
    assert row["transcript"]
    assert row["summary_json"] is None


@pytest.mark.parametrize("failure", ["http", "network", "unexpected"])
async def test_pipeline_llm_failures_keep_error_classification(tmp_path, failure):
    def handler(request):
        if failure == "http":
            return httpx.Response(503, text="upstream unavailable")
        if failure == "network":
            raise httpx.ConnectError("connection failed")
        raise RuntimeError("unexpected SDK error")

    row = await _run_with_llm_output(tmp_path, handler)
    assert row["status"] == "failed"
    assert row["error_code"] == "LLM_FAILED"


async def test_pipeline_llm_cancellation_propagates(tmp_path):
    def handler(request):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _run_with_llm_output(tmp_path, handler)
    row = await _get_task(tmp_path / "p.db", "t1")
    # 取消交由消费者处理，不能被 LLM 的普通异常兜底改成失败。
    assert row["status"] == "summarizing"
    assert row["error_code"] is None


@pytest.mark.parametrize("failure", ["invalid_output", "unexpected"])
async def test_llm_failure_consumer_continues_and_api_retry_succeeds(
    tmp_path, monkeypatch, failure
):
    from app.config import get_settings
    from app.main import create_app

    calls = 0
    expected = {"summary": "已完成", "key_points": ["要点"], "todos": []}

    def handler(request):
        nonlocal calls
        calls += 1
        failure_calls = 4 if failure == "invalid_output" else 1
        if calls <= failure_calls:
            if failure == "unexpected":
                raise RuntimeError("private SDK error")
            return httpx.Response(200, json={"choices": None})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(expected)}}]
        })

    llm = LlmClient(
        api_key="test", base_url="https://llm.test/v1", model="m",
        timeout_seconds=1, transport=httpx.MockTransport(handler),
    )
    monkeypatch.setenv("AUDIOMUSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUDIOMUSE_LLM_API_KEY", "")
    monkeypatch.setenv("AUDIOMUSE_ASR_API_KEY", "")
    monkeypatch.setenv("AUDIOMUSE_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("AUDIOMUSE_ASR_MIN_SECONDS", "0")
    monkeypatch.setenv("AUDIOMUSE_ASR_MAX_SECONDS", "0")
    monkeypatch.setenv("AUDIOMUSE_ASR_FAILURE_THRESHOLD", "0")
    monkeypatch.setenv("AUDIOMUSE_AUTO_RETRY_MAX_RETRIES", "3")
    monkeypatch.setenv("AUDIOMUSE_AUTO_RETRY_BASE_DELAY_SECONDS", "0")
    monkeypatch.setattr("app.main.build_llm_client", lambda settings: llm)
    get_settings.cache_clear()
    try:
        app = create_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                async def upload():
                    response = await client.post(
                        "/v1/recordings", files={"file": ("a.wav", b"audio")}
                    )
                    assert response.status_code == 202
                    return response.json()["data"]

                async def wait_status(task_id, status):
                    async def poll():
                        while True:
                            response = await client.get(f"/v1/tasks/{task_id}")
                            assert response.status_code == 200
                            row = response.json()["data"]
                            if row["status"] == status:
                                return row
                            await asyncio.sleep(0.01)
                    return await asyncio.wait_for(poll(), timeout=3)

                first = await upload()
                failed = await wait_status(first["task_id"], "failed")
                assert failed["error_code"] == (
                    "LLM_INVALID_OUTPUT" if failure == "invalid_output"
                    else "LLM_FAILED"
                )
                assert "private SDK error" not in failed["error_message"]
                # 仅一个消费者：后一段能完成，证明前一段失败没有杀死它。
                second = await upload()
                await wait_status(second["task_id"], "done")

                retry = await client.post(f"/v1/tasks/{first['task_id']}/retry")
                assert retry.status_code == 202
                assert retry.json()["data"]["attempt_no"] == 2
                done = await wait_status(first["task_id"], "done")
                assert done["error_code"] is None
                detail = await client.get(f"/v1/recordings/{first['recording_id']}")
                assert detail.status_code == 200
                assert detail.json()["data"]["transcript"]
                assert detail.json()["data"]["summary"] == expected
                assert calls == (6 if failure == "invalid_output" else 3)
    finally:
        get_settings.cache_clear()


# 单任务取消后消费者继续运行

async def test_cancel_one_task_consumer_keeps_working(tmp_path):
    db_path = tmp_path / "p.db"
    await run_migrations(db_path)
    now = now_utc_ms()
    async with db_ctx(db_path) as conn:
        for i in range(2):
            await create_recording_and_task(
                conn, recording_id=f"r{i}", task_id=f"t{i}",
                original_filename=f"{i}.wav",
                storage_relpath=f"recordings/r{i}.wav",
                extension="wav", size_bytes=10, now=now,
            )

    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_processor(task):
        # 第一个任务阻塞等待（模拟正在转写/摘要）；第二个任务直接返回
        if task["id"] == "t0":
            started.set()
            await release.wait()  # 会被外部 cancel 打断

    registry = TaskRegistry()
    stop = asyncio.Event()
    runner = asyncio.create_task(
        consume_loop(
            db_path=db_path, processor=blocking_processor,
            registry=registry, stop=stop, poll_interval=0.01,
        )
    )
    try:
        await started.wait()  # t0 已进入处理（阻塞中）
        assert registry.cancel("t0") is True
        await asyncio.sleep(0.1)  # 让取消传播并完成标记写回

        row = await _get_task(db_path, "t0")
        assert row["status"] == TaskStatus.FAILED.value
        assert row["error_code"] == ErrorCode.PROCESSING_CANCELLED.value
        assert registry.active_count() == 0
        assert not runner.done()  # 消费者未被杀死，继续服务其他录音
    finally:
        stop.set()
        await asyncio.wait_for(runner, timeout=1.0)
