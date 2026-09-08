"""录音与任务的数据访问：短事务。

repository 为录音/任务数据的读写落点（数据访问层）；
P0-03 提供上传创建；P0-04 追加原子领取与启动清理；
后续阶段在此补充状态流转写回、删除等操作。
"""

from __future__ import annotations

from typing import Optional

import aiosqlite

from app.db.constants import ErrorCode, Lifecycle, TaskStatus, now_utc_ms

_INSERT_RECORDING = """
    INSERT INTO recordings
        (id, original_filename, storage_path, extension, size_bytes,
         lifecycle, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_TASK = """
    INSERT INTO tasks
        (id, recording_id, status, attempt_no, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?)
"""

_CLAIM_SELECT = """
    SELECT t.id, t.recording_id, t.attempt_no, t.status,
           r.original_filename, r.storage_path, r.extension
      FROM tasks t
      JOIN recordings r ON r.id = t.recording_id
     WHERE t.status = ?
     ORDER BY t.created_at ASC, t.id ASC
     LIMIT 1
"""

_CLAIM_UPDATE = """
    UPDATE tasks
       SET status = ?, started_at = ?, updated_at = ?
     WHERE id = ? AND status = ? AND attempt_no = ?
"""

_MARK_INTERRUPTED = """
    UPDATE tasks
       SET status = ?, error_code = ?, error_message = ?,
           finished_at = ?, updated_at = ?
     WHERE status IN (?, ?)
"""


async def create_recording_and_task(
    conn: aiosqlite.Connection,
    *,
    recording_id: str,
    task_id: str,
    original_filename: str,
    storage_relpath: str,
    extension: str,
    size_bytes: int,
    now: int | None = None,
) -> None:
    """在同一事务内创建录音记录与其 pending 任务；失败整体回滚。"""
    now = now or now_utc_ms()
    # aiosqlite 的 `async with conn` 不是事务语义（会重复启动后台线程），
    # 事务需显式 BEGIN / COMMIT / ROLLBACK。
    await conn.execute("BEGIN IMMEDIATE")
    try:
        await conn.execute(
            _INSERT_RECORDING,
            (
                recording_id, original_filename, storage_relpath, extension,
                size_bytes, Lifecycle.ACTIVE.value, now, now,
            ),
        )
        await conn.execute(
            _INSERT_TASK,
            (task_id, recording_id, TaskStatus.PENDING.value, 1, now, now),
        )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def claim_next_task(
    conn: aiosqlite.Connection,
    *,
    now: Optional[int] = None,
) -> Optional[dict]:
    """原子领取一个 pending 任务，将其置为 transcribing。

    保证同一任务只会被一个消费者领取：BEGIN IMMEDIATE 让 SQLite
    写事务串行化，领取期间其他消费者的写锁请求会等待；本事务提交后
    它们再 SELECT 时该任务已是 transcribing，因此不会被重复领取。

    处理阶段（P0-05 的转写/摘要）不持有任何数据库事务/锁，
    因此多个消费者可并行处理各自领取的不同录音。

    无 pending 任务时返回 None；返回的 dict 含任务与录音的定位字段。
    """
    now = now or now_utc_ms()
    await conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = await conn.execute(_CLAIM_SELECT, (TaskStatus.PENDING.value,))
        row = await cursor.fetchone()
        if row is None:
            await conn.rollback()
            return None
        task = dict(row)
        await conn.execute(
            _CLAIM_UPDATE,
            (
                TaskStatus.TRANSCRIBING.value, now, now,
                task["id"], TaskStatus.PENDING.value, task["attempt_no"],
            ),
        )
        await conn.commit()
        task["status"] = TaskStatus.TRANSCRIBING.value
        task["started_at"] = now
        return task
    except Exception:
        await conn.rollback()
        raise


async def mark_interrupted_tasks(
    conn: aiosqlite.Connection,
    *,
    now: Optional[int] = None,
) -> int:
    """启动清理：把遗留的 transcribing/summarizing 任务标记为 failed。

    返回受影响行数。仅适用于单进程服务（不能用于多实例，否则可能
    误判其他实例正在执行的任务）。
    """
    now = now or now_utc_ms()
    await conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = await conn.execute(
            _MARK_INTERRUPTED,
            (
                TaskStatus.FAILED.value,
                ErrorCode.WORKER_INTERRUPTED.value,
                "服务重启，处理被中断，请手动重试",
                now, now,
                TaskStatus.TRANSCRIBING.value,
                TaskStatus.SUMMARIZING.value,
            ),
        )
        affected = cursor.rowcount
        await conn.commit()
        return int(affected)
    except Exception:
        await conn.rollback()
        raise
