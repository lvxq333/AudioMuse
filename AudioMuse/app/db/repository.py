"""录音与任务的数据访问：短事务。

repository 为录音/任务数据的读写落点（数据访问层）；
P0-03 先提供上传创建（录音 + 任务同一事务），
后续阶段在此补充原子领取、状态流转、删除等读写操作。
"""

from __future__ import annotations

import aiosqlite

from app.db.constants import Lifecycle, TaskStatus, now_utc_ms

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
