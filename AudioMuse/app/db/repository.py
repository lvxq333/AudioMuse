"""封装录音和处理任务的数据库读写及状态条件更新。"""

from __future__ import annotations

import json
from typing import Optional

import aiosqlite

from app.db.constants import ErrorCode, Lifecycle, TaskStatus, now_utc_ms

_INSERT_RECORDING = """
    INSERT INTO recordings
        (id, original_filename, storage_path, extension, size_bytes,
         lifecycle, idempotency_key, file_sha256, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
     WHERE t.status = ? AND r.lifecycle = ?
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
    idempotency_key: Optional[str] = None,
    file_sha256: Optional[str] = None,
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
                size_bytes, Lifecycle.ACTIVE.value, idempotency_key,
                file_sha256, now, now,
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


async def get_recording_by_idempotency_key(
    conn: aiosqlite.Connection, idempotency_key: str
) -> Optional[dict]:
    """查询幂等键对应的录音和任务；不存在返回 None。"""
    cursor = await conn.execute(
        "SELECT r.id AS recording_id, r.lifecycle, r.file_sha256,"
        " t.id AS task_id, t.status"
        " FROM recordings r JOIN tasks t ON t.recording_id=r.id"
        " WHERE r.idempotency_key=?",
        (idempotency_key,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def claim_next_task(
    conn: aiosqlite.Connection,
    *,
    now: Optional[int] = None,
) -> Optional[dict]:
    """原子领取一个 pending 任务，将其置为 transcribing。

    保证同一任务只会被一个消费者领取：BEGIN IMMEDIATE 让 SQLite
    写事务串行化，领取期间其他消费者的写锁请求会等待；本事务提交后
    它们再 SELECT 时该任务已是 transcribing，因此不会被重复领取。

    转写和摘要执行期间不持有任何数据库事务或锁，
    因此多个消费者可并行处理各自领取的不同录音。

    无 pending 任务时返回 None；返回的 dict 含任务与录音的定位字段。
    """
    now = now or now_utc_ms()
    await conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = await conn.execute(
            _CLAIM_SELECT,
            (TaskStatus.PENDING.value, Lifecycle.ACTIVE.value),
        )
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


async def mark_summarizing(
    conn: aiosqlite.Connection,
    *,
    task_id: str,
    attempt_no: int,
    transcript: str,
    now: Optional[int] = None,
) -> bool:
    """转写成功：保存 transcript 并把状态置为 summarizing。

    带 attempt_no + 期望状态条件，防止旧轮次/已删除任务被覆盖写回；
    参数非法（transcript 为空）抛 ValueError（调用方 bug），写回不生效
    返回 False（过期/竞争，正常业务情形）。
    """
    if not transcript or not transcript.strip():
        raise ValueError("transcript 不能为空")
    now = now or now_utc_ms()
    await conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = await conn.execute(
            "UPDATE tasks SET status=?, transcript=?, updated_at=?"
            " WHERE id=? AND attempt_no=? AND status=?",
            (
                TaskStatus.SUMMARIZING.value, transcript, now,
                task_id, attempt_no, TaskStatus.TRANSCRIBING.value,
            ),
        )
        ok = cursor.rowcount == 1
        await conn.commit()
        return bool(ok)
    except Exception:
        await conn.rollback()
        raise


async def finish_task_done(
    conn: aiosqlite.Connection,
    *,
    task_id: str,
    attempt_no: int,
    summary_json: str,
    now: Optional[int] = None,
) -> bool:
    """摘要完成：写入 summary_json 并置为 done（结果与状态同一事务）。

    兜底校验：summary_json 必须是可解析的 JSON 对象（业务字段结构由
    LLM 适配层校验，本层只保证“能落库、是对象”）。
    """
    try:
        parsed = json.loads(summary_json)
    except (TypeError, ValueError) as exc:
        raise ValueError("summary_json 必须是合法 JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("summary_json 必须是 JSON 对象")
    now = now or now_utc_ms()
    await conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = await conn.execute(
            "UPDATE tasks SET status=?, summary_json=?, finished_at=?, updated_at=?"
            " WHERE id=? AND attempt_no=? AND status=?",
            (
                TaskStatus.DONE.value, summary_json, now, now,
                task_id, attempt_no, TaskStatus.SUMMARIZING.value,
            ),
        )
        ok = cursor.rowcount == 1
        await conn.commit()
        return bool(ok)
    except Exception:
        await conn.rollback()
        raise


async def fail_task(
    conn: aiosqlite.Connection,
    *,
    task_id: str,
    attempt_no: int,
    error_code: str,
    error_message: str,
    now: Optional[int] = None,
) -> bool:
    """把正在处理（transcribing/summarizing）的任务置为 failed。

    error_code 必须来自 ErrorCode 白名单，error_message 非空；
    写回不生效（任务已不在执行中状态）返回 False。
    """
    if error_code not in {e.value for e in ErrorCode}:
        raise ValueError(f"非法 error_code: {error_code}")
    if not error_message:
        raise ValueError("error_message 不能为空")
    now = now or now_utc_ms()
    await conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = await conn.execute(
            "UPDATE tasks SET status=?, error_code=?, error_message=?, "
            "finished_at=?, updated_at=?"
            " WHERE id=? AND attempt_no=? AND status IN (?, ?)",
            (
                TaskStatus.FAILED.value, error_code, error_message, now, now,
                task_id, attempt_no,
                TaskStatus.TRANSCRIBING.value, TaskStatus.SUMMARIZING.value,
            ),
        )
        ok = cursor.rowcount == 1
        await conn.commit()
        return bool(ok)
    except Exception:
        await conn.rollback()
        raise


# 查询

_QUERY_TASK = """
    SELECT id, recording_id, status, attempt_no,
           error_code, error_message,
           created_at, updated_at, started_at, finished_at
      FROM tasks
     WHERE id = ?
"""

_LIST_SQL = """
    SELECT r.id AS recording_id, r.original_filename, r.size_bytes,
           r.extension, r.created_at,
           t.id AS task_id, t.status AS task_status,
           t.attempt_no, t.error_code
      FROM recordings r
      JOIN tasks t ON t.recording_id = r.id
     WHERE r.lifecycle = ?
     ORDER BY r.created_at DESC, r.id DESC
     LIMIT ? OFFSET ?
"""

_COUNT_SQL = """
    SELECT COUNT(*) AS n FROM recordings WHERE lifecycle = ?
"""

_DETAIL_SQL = """
    SELECT r.id AS recording_id, r.original_filename, r.extension,
           r.size_bytes, r.storage_path, r.created_at,
           t.id AS task_id, t.status, t.attempt_no,
           t.transcript, t.summary_json,
           t.error_code, t.error_message,
           t.created_at AS task_created_at, t.updated_at AS task_updated_at,
           t.started_at, t.finished_at
      FROM recordings r
      JOIN tasks t ON t.recording_id = r.id
     WHERE r.id = ?
"""


async def get_task_by_id(
    conn: aiosqlite.Connection, task_id: str
) -> Optional[dict]:
    """按 task_id 查询任务；不存在返回 None。"""
    cursor = await conn.execute(_QUERY_TASK, (task_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_recordings(
    conn: aiosqlite.Connection,
    *,
    page: int,
    page_size: int,
) -> dict:
    """分页倒序列出 active 录音（含最新任务状态），返回 {items, total}。"""
    lifecycle = Lifecycle.ACTIVE.value
    cursor = await conn.execute(_COUNT_SQL, (lifecycle,))
    row = await cursor.fetchone()
    total = int(row["n"])

    offset = (page - 1) * page_size
    cursor = await conn.execute(_LIST_SQL, (lifecycle, page_size, offset))
    items = [dict(r) for r in await cursor.fetchall()]
    return {"items": items, "total": total}


async def get_recording_with_task(
    conn: aiosqlite.Connection, recording_id: str
) -> Optional[dict]:
    """查询录音及其任务（含 transcript/summary_json）；不存在返回 None。"""
    cursor = await conn.execute(_DETAIL_SQL, (recording_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


# 手动重试

async def retry_reset_task(
    conn: aiosqlite.Connection,
    *,
    task_id: str,
    now: Optional[int] = None,
) -> bool:
    """失败任务重试：原子地把 failed 任务重置为 pending（attempt_no + 1）。

    - 仅当当前状态为 failed 且关联录音为 active 时生效（条件 UPDATE，防并发重复重试：
      同一时刻只有一个请求能把 failed → pending，其余影响 0 行）；
    - 清理上一轮的 error_code/error_message、transcript、summary_json、
      started_at/finished_at，让新一轮从干净状态开始；
    - 返回是否生效（行数 == 1）。
    """
    now = now or now_utc_ms()
    await conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = await conn.execute(
            "UPDATE tasks"
            " SET status=?, attempt_no=attempt_no + 1,"
            "     error_code=NULL, error_message=NULL,"
            "     transcript=NULL, summary_json=NULL,"
            "     started_at=NULL, finished_at=NULL, updated_at=?"
            " WHERE id=? AND status=?"
            " AND EXISTS (SELECT 1 FROM recordings r"
            " WHERE r.id=tasks.recording_id AND r.lifecycle=?)",
            (
                TaskStatus.PENDING.value, now,
                task_id, TaskStatus.FAILED.value, Lifecycle.ACTIVE.value,
            ),
        )
        ok = cursor.rowcount == 1
        await conn.commit()
        return bool(ok)
    except Exception:
        await conn.rollback()
        raise


# 录音删除

async def mark_recording_deleting(
    conn: aiosqlite.Connection,
    *,
    recording_id: str,
    now: Optional[int] = None,
) -> bool:
    """原子地把 active 录音置为 deleting。

    防止并发双删与删除期间后台写回/新领取（claim 已按 lifecycle 过滤）。
    """
    now = now or now_utc_ms()
    await conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = await conn.execute(
            "UPDATE recordings SET lifecycle=?, updated_at=?"
            " WHERE id=? AND lifecycle=?",
            (Lifecycle.DELETING.value, now, recording_id, Lifecycle.ACTIVE.value),
        )
        ok = cursor.rowcount == 1
        await conn.commit()
        return bool(ok)
    except Exception:
        await conn.rollback()
        raise


async def delete_recording_row(
    conn: aiosqlite.Connection,
    *,
    recording_id: str,
) -> bool:
    """删除录音行（tasks 经外键 ON DELETE CASCADE 一并删除）。

    仅允许删除处于 deleting 的录音（防止误删仍在活跃服务的行）。
    """
    await conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = await conn.execute(
            "DELETE FROM recordings WHERE id=? AND lifecycle=?",
            (recording_id, Lifecycle.DELETING.value),
        )
        ok = cursor.rowcount == 1
        await conn.commit()
        return bool(ok)
    except Exception:
        await conn.rollback()
        raise
