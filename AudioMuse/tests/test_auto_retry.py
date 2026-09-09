"""ASR/LLM 阶段内自动重试：局部计数、指数退避与 attempt_no 语义。"""

import asyncio
from pathlib import Path

import pytest

import app.services.pipeline as pipeline_module
from app.db.connection import db as db_ctx
from app.db.connection import run_migrations
from app.db.repository import (
    claim_next_task,
    create_recording_and_task,
    get_task_by_id,
    retry_reset_task,
)
from app.services.asr import AsrFailure
from app.services.llm import LlmInvalidOutput, LlmResult, LlmTimeout
from app.services.pipeline import build_processor


async def _seed_and_claim(db_path: Path) -> dict:
    await run_migrations(db_path)
    async with db_ctx(db_path) as conn:
        await create_recording_and_task(
            conn,
            recording_id="r1",
            task_id="t1",
            original_filename="a.wav",
            storage_relpath="recordings/r1.wav",
            extension="wav",
            size_bytes=10,
        )
        task = await claim_next_task(conn)
    assert task is not None
    return task


async def _task_row(db_path: Path) -> dict:
    async with db_ctx(db_path) as conn:
        row = await get_task_by_id(conn, "t1")
    assert row is not None
    return row


class _SuccessfulLlm:
    def __init__(self) -> None:
        self.calls = 0

    async def summarize(self, transcript, *, task_id):
        self.calls += 1
        return LlmResult(summary="完成", key_points=["要点"], todos=[])


async def test_asr_retries_twice_then_succeeds_without_changing_attempt(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry.db"
    task = await _seed_and_claim(db_path)
    calls = 0
    delays = []

    async def flaky_asr(**kwargs):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise AsrFailure("暂时失败")
        return "转写成功"

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(pipeline_module, "transcribe", flaky_asr)
    llm = _SuccessfulLlm()
    processor = build_processor(
        db_path=db_path, llm_client=llm,
        retry_sleep=fake_sleep,
    )
    await processor(task)

    row = await _task_row(db_path)
    assert (calls, delays, llm.calls) == (3, [1.0, 2.0], 1)
    assert row["status"] == "done"
    assert row["attempt_no"] == 1


async def test_asr_retry_exhaustion_fails_and_skips_llm(tmp_path, monkeypatch):
    db_path = tmp_path / "retry.db"
    task = await _seed_and_claim(db_path)
    calls = 0
    delays = []

    async def failing_asr(**kwargs):
        nonlocal calls
        calls += 1
        raise AsrFailure(f"第 {calls} 次失败")

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(pipeline_module, "transcribe", failing_asr)
    llm = _SuccessfulLlm()
    await build_processor(
        db_path=db_path, llm_client=llm, retry_sleep=fake_sleep
    )(task)

    row = await _task_row(db_path)
    assert (calls, delays, llm.calls) == (4, [1.0, 2.0, 4.0], 0)
    assert row["status"] == "failed"
    assert row["error_code"] == "ASR_FAILED"
    assert row["attempt_no"] == 1


@pytest.mark.parametrize("error", [LlmTimeout("超时"), LlmInvalidOutput("非法输出")])
async def test_llm_retries_twice_then_succeeds_and_keeps_transcript(
    tmp_path, monkeypatch, error
):
    db_path = tmp_path / "retry.db"
    task = await _seed_and_claim(db_path)
    asr_calls = 0
    delays = []

    async def successful_asr(**kwargs):
        nonlocal asr_calls
        asr_calls += 1
        return "已保存的转写"

    class FlakyLlm:
        def __init__(self):
            self.calls = 0

        async def summarize(self, transcript, *, task_id):
            self.calls += 1
            if self.calls <= 2:
                raise error
            return LlmResult(summary="完成", key_points=[], todos=[])

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(pipeline_module, "transcribe", successful_asr)
    llm = FlakyLlm()
    await build_processor(
        db_path=db_path, llm_client=llm, retry_sleep=fake_sleep
    )(task)

    row = await _task_row(db_path)
    assert (asr_calls, llm.calls, delays) == (1, 3, [1.0, 2.0])
    assert row["status"] == "done"
    assert row["attempt_no"] == 1
    async with db_ctx(db_path) as conn:
        cursor = await conn.execute("SELECT transcript FROM tasks WHERE id='t1'")
        assert (await cursor.fetchone())["transcript"] == "已保存的转写"


async def test_llm_retry_exhaustion_preserves_last_error_and_transcript(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry.db"
    task = await _seed_and_claim(db_path)
    delays = []

    async def successful_asr(**kwargs):
        return "已保存的转写"

    class FailingLlm:
        def __init__(self):
            self.calls = 0

        async def summarize(self, transcript, *, task_id):
            self.calls += 1
            raise LlmTimeout(f"第 {self.calls} 次超时")

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(pipeline_module, "transcribe", successful_asr)
    llm = FailingLlm()
    await build_processor(
        db_path=db_path, llm_client=llm, retry_sleep=fake_sleep
    )(task)

    row = await _task_row(db_path)
    assert (llm.calls, delays) == (4, [1.0, 2.0, 4.0])
    assert row["status"] == "failed"
    assert row["error_code"] == "LLM_TIMEOUT"
    assert row["attempt_no"] == 1
    async with db_ctx(db_path) as conn:
        cursor = await conn.execute(
            "SELECT transcript, error_message FROM tasks WHERE id='t1'"
        )
        stored = await cursor.fetchone()
        assert stored["transcript"] == "已保存的转写"
        assert stored["error_message"] == "第 4 次超时"


@pytest.mark.parametrize("stage", ["asr", "llm"])
async def test_unexpected_errors_are_not_retried(tmp_path, monkeypatch, stage):
    db_path = tmp_path / "retry.db"
    task = await _seed_and_claim(db_path)
    delays = []
    asr_calls = 0

    async def asr(**kwargs):
        nonlocal asr_calls
        asr_calls += 1
        if stage == "asr":
            raise RuntimeError("代码异常")
        return "转写"

    class Llm:
        def __init__(self):
            self.calls = 0

        async def summarize(self, transcript, *, task_id):
            self.calls += 1
            raise RuntimeError("SDK 异常")

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(pipeline_module, "transcribe", asr)
    llm = Llm()
    await build_processor(
        db_path=db_path, llm_client=llm, retry_sleep=fake_sleep
    )(task)

    row = await _task_row(db_path)
    assert delays == []
    assert asr_calls == 1
    assert llm.calls == (0 if stage == "asr" else 1)
    assert row["status"] == "failed"
    assert row["error_code"] == ("ASR_FAILED" if stage == "asr" else "LLM_FAILED")
    assert row["attempt_no"] == 1


async def test_cancellation_during_backoff_stops_further_retries(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry.db"
    task = await _seed_and_claim(db_path)
    entered_backoff = asyncio.Event()
    never_release = asyncio.Event()
    calls = 0

    async def failing_asr(**kwargs):
        nonlocal calls
        calls += 1
        raise AsrFailure("失败")

    async def blocking_sleep(delay):
        entered_backoff.set()
        await never_release.wait()

    monkeypatch.setattr(pipeline_module, "transcribe", failing_asr)
    running = asyncio.create_task(build_processor(
        db_path=db_path, llm_client=_SuccessfulLlm(), retry_sleep=blocking_sleep
    )(task))
    await entered_backoff.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert calls == 1
    row = await _task_row(db_path)
    assert row["status"] == "transcribing"
    assert row["attempt_no"] == 1


async def test_manual_retry_starts_new_attempt_and_resets_local_retry_count(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "retry.db"
    first_task = await _seed_and_claim(db_path)
    delays = []

    async def successful_asr(**kwargs):
        return "转写"

    class Llm:
        def __init__(self):
            self.calls = 0

        async def summarize(self, transcript, *, task_id):
            self.calls += 1
            if self.calls <= 5:
                raise LlmTimeout("超时")
            return LlmResult(summary="第二轮完成", key_points=[], todos=[])

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(pipeline_module, "transcribe", successful_asr)
    llm = Llm()
    processor = build_processor(
        db_path=db_path, llm_client=llm, retry_sleep=fake_sleep
    )
    await processor(first_task)
    first_row = await _task_row(db_path)
    assert first_row["status"] == "failed"
    assert first_row["attempt_no"] == 1

    async with db_ctx(db_path) as conn:
        assert await retry_reset_task(conn, task_id="t1") is True
        second_task = await claim_next_task(conn)
    assert second_task is not None
    assert second_task["attempt_no"] == 2
    await processor(second_task)

    second_row = await _task_row(db_path)
    assert second_row["status"] == "done"
    assert second_row["attempt_no"] == 2
    # 第一轮退避 1/2/4，第二轮从 1 秒重新开始，证明局部计数已重置。
    assert delays == [1.0, 2.0, 4.0, 1.0]
    assert llm.calls == 6
