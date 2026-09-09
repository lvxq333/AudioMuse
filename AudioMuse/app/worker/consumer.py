"""后台异步消费者。

- 每个消费者是一个 asyncio Task（协程），共享同一事件循环；
- 循环：原子领取一个 pending 任务 → 交给 processor 处理 → 继续；
- 每个任务的处理被包装成独立子任务并登记到 TaskRegistry：
  - 外部停止单个任务（registry.cancel）只取消该子任务——消费者捕获
    CancelledError 后把该录音标记为可重试失败（PROCESSING_CANCELLED）
    并继续循环，符合“停一个录音、消费者空闲继续处理其他录音”的契约；
  - 消费者整体被停（关闭流程）时不存在取消请求标记，CancelledError
    继续上抛导致本协程退出；
- 队列模型（P0-04 决定）：数据库中的 pending 行即持久化队列，
  消费者按 poll_interval 轮询原子领取——服务重启后 pending 自动
  被继续消费，无内存队列的恢复/一致性窗口。
"""

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
# 由实现方自行管理该任务的后续状态写回（须携带 attempt_no 防旧写回覆盖）。
Processor = Callable[[dict], Awaitable[None]]

_POLL_INTERVAL = 0.2  # 无任务时的轮询间隔（秒）


async def _mark_stopped(db_path: Path, task: dict) -> None:
    """单任务被外部停止：置为 failed(PROCESSING_CANCELLED)，可后续 retry/删除。"""
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
    """单个消费者的主循环：领取 → 处理 → 领取……直到 stop 置位。"""
    logger.info("消费者启动")
    try:
        while not stop.is_set():
            task = None
            async with db(db_path) as conn:
                task = await claim_next_task(conn)

            if task is None:
                # 无待处理任务：短暂退避，避免空转刷库
                await asyncio.sleep(poll_interval)
                continue

            try:
                proc_task = registry.start(task["id"], processor(task))
                try:
                    await proc_task
                except asyncio.CancelledError:
                    if registry.was_cancel_requested(task["id"]):
                        # 该录音的处理被单独停止：标记可重试失败，消费者继续
                        logger.info("任务处理被外部停止 task_id=%s", task["id"])
                        await _mark_stopped(db_path, task)
                        registry.ack_cancel(task["id"])
                        continue
                    raise  # 消费者整体被停
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
