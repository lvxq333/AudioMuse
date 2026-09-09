"""从数据库领取待处理任务，并管理消费者执行、取消和退出。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

from app.db.connection import db
from app.db.constants import ErrorCode
from app.db.repository import claim_next_task, fail_task
from app.services.registry import TaskRegistry

logger = logging.getLogger(__name__)

# processor 契约：入参为 claim 返回的任务 dict（含 recording 定位字段），
# 由实现方管理任务状态写回（须携带 attempt_no 防止旧轮次覆盖新轮次）。
Processor = Callable[[dict], Awaitable[None]]

_POLL_INTERVAL = 0.2  # 无任务时的轮询间隔（秒）


async def _mark_stopped(db_path: Path, task: dict) -> None:
    """将被外部停止的单个任务置为可重试或删除的失败状态。"""
    async with db(db_path) as conn:
        await fail_task(
            conn,
            task_id=task["id"], attempt_no=task["attempt_no"],
            error_code=ErrorCode.PROCESSING_CANCELLED.value,
            error_message="任务处理已被停止，可重试或删除",
        )


async def consume_loop(
    *,
    db_path: Path,
    processor: Processor,
    registry: TaskRegistry,
    stop: asyncio.Event,
    poll_interval: float = _POLL_INTERVAL,
) -> None:
    """持续领取并处理任务，直到收到消费者停止信号。"""
    logger.info("消费者启动")
    try:
        while not stop.is_set():
            # 流程 1：原子领取一个 pending 任务。
            task = None
            async with db(db_path) as conn:
                task = await claim_next_task(conn)

            if task is None:
                # 流程 2：无任务时异步退避，避免持续轮询数据库。
                await asyncio.sleep(poll_interval)
                continue

            # 流程 3：注册并执行任务，使删除接口可以按任务取消。
            try:
                proc_task = registry.start(task["id"], processor(task))
                try:
                    await proc_task
                except asyncio.CancelledError:
                    if registry.was_cancel_requested(task["id"]):
                        # 流程 4：单任务取消时写入失败状态，消费者继续处理下一项。
                        logger.info("任务处理被外部停止 task_id=%s", task["id"])
                        await _mark_stopped(db_path, task)
                        registry.ack_cancel(task["id"])
                        continue
                    raise  # 取消来源是消费者本身时退出循环。
            except Exception:
                # processor 不应抛未分类异常；此处兜底防单个任务异常杀死消费者
                logger.exception(
                    "处理任务异常 task_id=%s attempt_no=%s",
                    task.get("id"), task.get("attempt_no"),
                )
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("消费者退出")


async def run_consumers(
    count: int,
    *,
    db_path: Path,
    processor: Processor,
    registry: TaskRegistry,
    stop: asyncio.Event,
    poll_interval: float = _POLL_INTERVAL,
) -> None:
    """并发运行 count 个消费者，直到全部结束（配合 stop 优雅退出）。"""
    consumers = [
        asyncio.create_task(
            consume_loop(
                db_path=db_path,
                processor=processor,
                registry=registry,
                stop=stop,
                poll_interval=poll_interval,
            ),
            name=f"consumer-{i}",
        )
        for i in range(count)
    ]
    await asyncio.gather(*consumers, return_exceptions=True)
