"""后台异步消费者。

- 每个消费者是一个 asyncio Task（协程），共享同一事件循环；
- 循环：原子领取一个 pending 任务 → 交给 processor 处理 → 继续；
- processor 负责该任务领取后的全部状态流转写回（P0-05 提供真实
  转写+摘要实现），本文件不关心处理细节；
- 领取与写回都只在短暂瞬间持有数据库事务/写锁，处理阶段不持锁，
  因此多个消费者可并行处理各自领取的不同录音；
- 队列模型（P0-04 决定）：数据库中的 pending 行即持久化队列，
  消费者按 poll_interval 轮询原子领取——服务重启后 pending 自动
  被继续消费，无内存队列的恢复/一致性窗口；
- 优雅退出：stop 事件置位后，处理完当前任务即退出，不再领取新任务。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional

from app.db.connection import db
from app.db.repository import claim_next_task

logger = logging.getLogger(__name__)

# processor 契约：入参为 claim 返回的任务 dict（含 recording 定位字段），
# 由实现方自行管理该任务的后续状态写回（须携带 attempt_no 防旧写回覆盖）。
Processor = Callable[[dict], Awaitable[None]]

_POLL_INTERVAL = 0.2  # 无任务时的轮询间隔（秒）


async def consume_loop(
    *,
    db_path: Path,
    processor: Processor,
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
                # 无待处理任务：短暂退避，避免空转刷库；stop 置位后最迟
                # poll_interval 内退出
                await asyncio.sleep(poll_interval)
                continue

            try:
                await processor(task)
            except Exception:
                # processor 应自行把失败任务写回 failed；此处仅兜底防单个
                # 任务异常杀死消费者（任务将保持 transcribing，由后续
                # 阶段的失败写回/启动清理兜底）
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
    stop: asyncio.Event,
    poll_interval: float = _POLL_INTERVAL,
) -> None:
    """并发运行 count 个消费者，直到全部结束（配合 stop 优雅退出）。

    调用方负责：置位 stop 后 await 本函数，等待进行中任务处理完成。
    """
    consumers = [
        asyncio.create_task(
            consume_loop(
                db_path=db_path,
                processor=processor,
                stop=stop,
                poll_interval=poll_interval,
            ),
            name=f"consumer-{i}",
        )
        for i in range(count)
    ]
    await asyncio.gather(*consumers, return_exceptions=True)
