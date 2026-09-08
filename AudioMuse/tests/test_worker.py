"""P0-04 worker 测试：原子领取、并发峰值、遗留清理、跨进程锁、优雅退出。

使用独立临时 data 目录与数据库，不碰真实 data/。
"""

import asyncio
import subprocess
import sys
from pathlib import Path

from app.db.connection import db as db_ctx
from app.db.connection import run_migrations
from app.db.constants import ErrorCode, TaskStatus, now_utc_ms
from app.db.repository import (
    claim_next_task,
    create_recording_and_task,
    mark_interrupted_tasks,
)
from app.worker.consumer import consume_loop, run_consumers
from app.worker.lock import DataDirLock


# ---------- helpers ----------

def _db_path(tmp_path) -> Path:
    return tmp_path / "worker.db"


async def _seed_pending(db_path: Path, count: int) -> None:
    """建 count 个录音 + pending 任务（r0..rN / t0..tN）。"""
    await run_migrations(db_path)
    now = now_utc_ms()
    async with db_ctx(db_path) as conn:
        for i in range(count):
            await create_recording_and_task(
                conn,
                recording_id=f"r{i}", task_id=f"t{i}",
                original_filename=f"{i}.wav",
                storage_relpath=f"recordings/r{i}.wav",
                extension="wav", size_bytes=10, now=now,
            )


async def _finish(db_path, task_id, attempt_no):
    """测试替身：把任务写回 done（模拟 processor 成功收尾）。"""
    now = now_utc_ms()
    async with db_ctx(db_path) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute(
                "UPDATE tasks SET status=?, finished_at=?, updated_at=?"
                " WHERE id=? AND attempt_no=?",
                (TaskStatus.DONE.value, now, now, task_id, attempt_no),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def _wait_tasks_status(db_path, status, expected, timeout=5.0):
    """轮询等待 DB 中处于某状态的任务数达到 expected。"""
    async def _count():
        async with db_ctx(db_path) as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE status=?", (status,)
            )
            row = await cur.fetchone()
            return int(row["n"])

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if await _count() >= expected:
            return
        if loop.time() > deadline:
            raise AssertionError(f"等待超时：status={status} 未达到 {expected}")
        await asyncio.sleep(0.02)


# ---------- 1. 原子领取唯一性 ----------

async def test_claim_is_atomic_and_exclusive(tmp_path):
    db_path = _db_path(tmp_path)
    await _seed_pending(db_path, 1)

    # 两个连接并发领取同一个任务：必须恰有一个成功
    async def _try_claim():
        async with db_ctx(db_path) as conn:
            return await claim_next_task(conn)

    results = await asyncio.gather(_try_claim(), _try_claim())
    got = [r for r in results if r is not None]
    assert len(got) == 1
    assert got[0]["id"] == "t0"
    assert got[0]["status"] == TaskStatus.TRANSCRIBING.value

    # 第三个连接再领：已无 pending
    async with db_ctx(db_path) as conn:
        assert await claim_next_task(conn) is None


# ---------- 2. 并发峰值 ≤3 且积压全部消费 ----------

async def test_three_consumers_process_all_with_peak_at_most_3(tmp_path):
    db_path = _db_path(tmp_path)
    await _seed_pending(db_path, 10)

    active = 0
    peak = 0

    async def processor(task):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)  # 模拟处理耗时
        await _finish(db_path, task["id"], task["attempt_no"])
        active -= 1

    stop = asyncio.Event()
    runner = asyncio.create_task(
        run_consumers(
            3, db_path=db_path, processor=processor, stop=stop,
            poll_interval=0.01,
        )
    )
    try:
        await _wait_tasks_status(db_path, TaskStatus.DONE.value, 10)
    finally:
        stop.set()
        await runner

    assert peak <= 3
    async with db_ctx(db_path) as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status='pending'"
        )
        row = await cur.fetchone()
        assert int(row["n"]) == 0


# ---------- 3. 启动遗留清理 ----------

async def test_mark_interrupted_only_hits_running_states(tmp_path):
    db_path = _db_path(tmp_path)
    await _seed_pending(db_path, 0)
    # 补建 4 个任务并把状态改为：transcribing/summarizing/pending/done
    now = now_utc_ms()
    statuses = [
        TaskStatus.TRANSCRIBING,
        TaskStatus.SUMMARIZING,
        TaskStatus.PENDING,
        TaskStatus.DONE,
    ]
    async with db_ctx(db_path) as conn:
        for i, status in enumerate(statuses):
            await create_recording_and_task(
                conn,
                recording_id=f"r{i}", task_id=f"t{i}",
                original_filename=f"{i}.wav",
                storage_relpath=f"recordings/r{i}.wav",
                extension="wav", size_bytes=10, now=now,
            )
            # UPDATE 会开启 sqlite 隐式事务：立即 commit，避免与下一轮
            # create_recording_and_task 的 BEGIN IMMEDIATE 冲突
            await conn.execute(
                "UPDATE tasks SET status=?, started_at=?, updated_at=?"
                " WHERE id=?",
                (status.value, now, now, f"t{i}"),
            )
            await conn.commit()
        affected = await mark_interrupted_tasks(conn)

    assert affected == 2  # 只处理 transcribing + summarizing
    async with db_ctx(db_path) as conn:
        cur = await conn.execute("SELECT status, error_code FROM tasks ORDER BY id")
        rows = [dict(r) for r in await cur.fetchall()]
    assert rows[0] == {"status": TaskStatus.FAILED.value,
                       "error_code": ErrorCode.WORKER_INTERRUPTED.value}
    assert rows[1] == {"status": TaskStatus.FAILED.value,
                       "error_code": ErrorCode.WORKER_INTERRUPTED.value}
    assert rows[2] == {"status": TaskStatus.PENDING.value, "error_code": None}
    assert rows[3] == {"status": TaskStatus.DONE.value, "error_code": None}


# ---------- 4. 跨进程锁互斥（真实子进程） ----------

def test_data_dir_lock_is_exclusive_across_processes(tmp_path):
    data_dir = tmp_path / "data"
    lock_code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from app.worker.lock import DataDirLock, DataDirLockError\n"
        "try:\n"
        "    DataDirLock(Path(sys.argv[1])).acquire(); print('ACQUIRED')\n"
        "except DataDirLockError:\n"
        "    print('LOCKED'); sys.exit(3)\n"
    )

    holder = DataDirLock(data_dir)
    holder.acquire()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", lock_code, str(data_dir)],
            capture_output=True, text=True, timeout=10,
        )
        assert "LOCKED" in proc.stdout, proc.stdout
        assert proc.returncode == 3
    finally:
        holder.release()

    # 释放后子进程可以拿到
    proc = subprocess.run(
        [sys.executable, "-c", lock_code, str(data_dir)],
        capture_output=True, text=True, timeout=10,
    )
    assert "ACQUIRED" in proc.stdout, proc.stdout


# ---------- 5. 优雅退出 ----------

async def test_consumer_exits_promptly_on_stop(tmp_path):
    db_path = _db_path(tmp_path)
    await run_migrations(db_path)

    stop = asyncio.Event()

    async def noop_processor(task):
        pass

    async def _run():
        await consume_loop(
            db_path=db_path, processor=noop_processor,
            stop=stop, poll_interval=0.01,
        )

    t = asyncio.create_task(_run())
    await asyncio.sleep(0.05)  # 让消费者进入空转
    stop.set()
    await asyncio.wait_for(t, timeout=1.0)  # 1s 内退出
