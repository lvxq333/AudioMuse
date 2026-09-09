"""处理流水线（processor）：把一次任务从 transcribing 推进到终态。

流程（每步独立短事务写回，携带 attempt_no + 期望状态防旧写回）：
  claim 后（已 transcribing）
    → ASR Mock 转写（约 20% 失败）
    → 保存 transcript 并置 summarizing
    → LLM 摘要（真实或 mock）
    → 保存 summary_json 并置 done
  ASR/LLM 明确业务失败 → 当前阶段内最多自动重试 3 次（1/2/4 秒退避）；
  重试耗尽 → failed + 对应 error_code（ASR_FAILED / LLM_*）；
  自动重试采用局部计数，不改变表示业务处理轮次的 attempt_no；
  写回不生效（任务已被删除/停止/进入新一轮）→ 返回 False 即停止后续步骤；
  被外部取消（CancelledError）→ 直接向上传播，由 registry 的取消流程
  接手（本文件不吞取消，except Exception 不含 CancelledError）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional

from app.db.connection import db
from app.db.repository import fail_task, finish_task_done, mark_summarizing
from app.services.asr import AsrFailure, transcribe
from app.services.llm import LlmClient, LlmError

logger = logging.getLogger(__name__)

# 与 app/worker/consumer.Processor 保持一致：入参为 claim 返回的任务 dict
Processor = Callable[[dict], Awaitable[None]]
RetrySleep = Callable[[float], Awaitable[None]]


async def _run_with_auto_retry(
    operation: Callable[[], Awaitable],
    *,
    stage: str,
    task_id: str,
    attempt_no: int,
    retryable_exceptions: tuple,
    max_retries: int,
    base_delay: float,
    sleep: RetrySleep,
):
    """执行一个处理阶段，失败时在当前业务轮次内做指数退避重试。"""
    retry_count = 0
    while True:
        try:
            return await operation()
        except retryable_exceptions as exc:
            if retry_count >= max_retries:
                logger.warning(
                    "自动重试耗尽 task_id=%s attempt_no=%d stage=%s "
                    "retry_count=%d code=%s",
                    task_id, attempt_no, stage, retry_count,
                    getattr(exc, "code", type(exc).__name__),
                )
                raise
            delay = base_delay * (2 ** retry_count)
            retry_count += 1
            logger.warning(
                "处理失败，准备自动重试 task_id=%s attempt_no=%d stage=%s "
                "retry_count=%d delay=%s code=%s",
                task_id, attempt_no, stage, retry_count, delay,
                getattr(exc, "code", type(exc).__name__),
            )
            # CancelledError 不会在这里被捕获；删除/关闭可立即中断退避。
            await sleep(delay)


async def _fail(db_path: Path, task: dict, error_code: str, message: str) -> None:
    async with db(db_path) as conn:
        await fail_task(
            conn,
            task_id=task["id"], attempt_no=task["attempt_no"],
            error_code=error_code, error_message=message,
        )


async def _to_summarizing(db_path: Path, task: dict, transcript: str) -> bool:
    async with db(db_path) as conn:
        return await mark_summarizing(
            conn,
            task_id=task["id"], attempt_no=task["attempt_no"],
            transcript=transcript,
        )


async def _done(db_path: Path, task: dict, summary_json: str) -> bool:
    async with db(db_path) as conn:
        return await finish_task_done(
            conn,
            task_id=task["id"], attempt_no=task["attempt_no"],
            summary_json=summary_json,
        )


def build_processor(
    *,
    db_path: Path,
    llm_client: LlmClient,
    asr_params: Optional[dict] = None,
    retry_max_retries: int = 3,
    retry_base_delay: float = 1.0,
    retry_sleep: RetrySleep = asyncio.sleep,
) -> Processor:
    """构造可注入消费者的 processor（闭包携带依赖）。

    asr_params 直接透传给 app.services.asr.transcribe 的关键字参数；
    retry_sleep 可由测试替换，从而验证退避序列而不真实等待。
    """
    if retry_max_retries < 0:
        raise ValueError("retry_max_retries 不能小于 0")
    if retry_base_delay < 0:
        raise ValueError("retry_base_delay 不能小于 0")
    asr_kwargs: dict = dict(asr_params or {})

    async def processor(task: dict) -> None:
        task_id = task["id"]
        attempt_no = task["attempt_no"]
        recording_id = task["recording_id"]
        logger.info("开始处理任务 task_id=%s attempt_no=%d", task_id, attempt_no)

        # 1) 转写（Mock ASR）
        try:
            transcript = await _run_with_auto_retry(
                lambda: transcribe(
                    recording_id=recording_id, task_id=task_id,
                    attempt_no=attempt_no, **asr_kwargs,
                ),
                stage="transcribing", task_id=task_id, attempt_no=attempt_no,
                retryable_exceptions=(AsrFailure,),
                max_retries=retry_max_retries,
                base_delay=retry_base_delay,
                sleep=retry_sleep,
            )
        except AsrFailure as exc:
            logger.warning("ASR 失败 task_id=%s", task_id)
            await _fail(db_path, task, "ASR_FAILED", str(exc))
            return
        except Exception as exc:  # 参数/实现 bug：归类为失败而非悬挂
            logger.exception("ASR 异常 task_id=%s", task_id)
            await _fail(db_path, task, "ASR_FAILED", f"ASR 异常: {exc}")
            return

        # 2) 保存 transcript 并进入 summarizing；写回失效（已删/新轮）则停止
        if not await _to_summarizing(db_path, task, transcript):
            logger.info("任务已被删除或进入新轮，放弃写回 task_id=%s", task_id)
            return

        # 3) LLM 摘要（超时/结构校验已在 LlmClient 内分类为 LlmError）
        try:
            result = await _run_with_auto_retry(
                lambda: llm_client.summarize(transcript, task_id=task_id),
                stage="summarizing", task_id=task_id, attempt_no=attempt_no,
                retryable_exceptions=(LlmError,),
                max_retries=retry_max_retries,
                base_delay=retry_base_delay,
                sleep=retry_sleep,
            )
        except LlmError as exc:
            logger.warning("LLM 失败 task_id=%s code=%s", task_id, exc.code)
            await _fail(db_path, task, exc.code, exc.message)
            return
        except Exception:
            logger.exception("LLM 未预期异常 task_id=%s", task_id)
            await _fail(db_path, task, "LLM_FAILED", "摘要处理发生异常，请重试")
            return

        # 4) 落 done（与 summary_json 同一事务）
        if not await _done(db_path, task, result.to_json()):
            logger.info("任务已完成或删除，放弃写回 task_id=%s", task_id)
            return
        logger.info("任务完成 task_id=%s attempt_no=%d", task_id, attempt_no)

    return processor
