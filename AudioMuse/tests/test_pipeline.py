"""P0-05 pipeline/registry/consumer 取消语义测试。

全部使用确定性替身：LLM 走 mock 分支或 httpx.MockTransport（不出网），
ASR 走毫秒级 + 注入假 sleep；不依赖真实网络与随机源。
"""

import asyncio
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

# ---------- helpers ----------

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


# ---------- 1. LLM 结构校验（第一层） ----------

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


# ---------- 2. pipeline：成功路径 ----------

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


# ---------- 3. pipeline：ASR 失败 → ASR_FAILED，且不调用 LLM ----------

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
    )
    await processor(task)

    row = await _get_task(db_path, task["id"])
    assert row["status"] == TaskStatus.FAILED.value
    assert row["error_code"] == "ASR_FAILED"
    assert _CountingLlm.calls == 0
    assert row["transcript"] is None


# ---------- 4. pipeline：LLM 超时/非法输出 → 对应错误码 ----------

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


# ---------- 5. 单任务取消：消费者继续 ----------

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
