"""处理中任务注册表：按任务粒度取消单个处理，消费者不受影响。

对应需求：停止某一个正在处理的任务时，只是停止对该录音文件的处理，
空闲消费者继续处理其他录音；该录音之后可选择继续处理（retry）
或删除（DELETE）。

- start(task_id, coro)：把单个任务的处理协程包装成 asyncio.Task 并登记，
  返回该 Task 供消费者 await；任务结束后自动移除登记（保留取消标记，
  由 ack_cancel 显式消费，避免与 await 侧读取产生竞态）；
- cancel(task_id)：请求取消指定任务的处理（不取消消费者协程本身）；
- was_cancel_requested / ack_cancel：消费者据此区分
  「单任务被停」（写回可重试状态、继续循环）与「消费者整体被停」（退出）；
- cancel_all()：取消全部进行中任务（用于关闭流程）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Dict, Set

logger = logging.getLogger(__name__)


class TaskRegistry:
    """task_id → asyncio.Task 的登记表（单进程、单事件循环内使用）。"""

    def __init__(self) -> None:
        self._tasks: Dict[str, asyncio.Task] = {}
        self._cancel_requested: Set[str] = set()

    def start(self, task_id: str, coro: Awaitable[None]) -> asyncio.Task:
        """登记并启动一个任务的处理协程，返回其 Task 句柄。"""
        if task_id in self._tasks:
            raise RuntimeError(f"任务已在处理中，不能重复启动: {task_id}")
        task = asyncio.create_task(coro, name=f"task-{task_id}")
        self._tasks[task_id] = task

        def _done(_: asyncio.Task) -> None:
            # 只移除登记；取消标记保留到 ack_cancel 显式消费，
            # 避免 done_callback 与 await 侧判读的竞态
            self._tasks.pop(task_id, None)

        task.add_done_callback(_done)
        return task

    def cancel(self, task_id: str) -> bool:
        """请求取消某任务的处理；命中并取消返回 True，否则 False。"""
        task = self._tasks.get(task_id)
        if task is None or task.done():
            return False
        self._cancel_requested.add(task_id)
        task.cancel()
        return True

    def was_cancel_requested(self, task_id: str) -> bool:
        """该任务是否已被外部请求取消（消费者据此区分取消来源）。"""
        return task_id in self._cancel_requested

    def ack_cancel(self, task_id: str) -> None:
        """消费者确认已处理某任务的取消（写回后可重试状态后调用）。"""
        self._cancel_requested.discard(task_id)

    def cancel_all(self) -> int:
        """取消全部进行中任务，返回被取消的数量。"""
        count = 0
        for task_id, task in list(self._tasks.items()):
            if not task.done():
                self._cancel_requested.add(task_id)
                task.cancel()
                count += 1
        return count

    def active_ids(self) -> list:
        return list(self._tasks)

    def active_count(self) -> int:
        return len(self._tasks)
