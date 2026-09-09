"""P0-02 持久化自检：迁移、幂等、外键、CHECK 约束、事务回滚。"""

import pytest
import aiosqlite

from app.db.connection import MIGRATIONS_DIR, db, run_migrations
from app.db.constants import now_utc_ms

_INSERT_RECORDING = (
    "INSERT INTO recordings (id, original_filename, storage_path, extension,"
    " size_bytes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_TASK = (
    "INSERT INTO tasks (id, recording_id, status, created_at, updated_at)"
    " VALUES (?, ?, ?, ?, ?)"
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


async def _count(conn, table: str) -> int:
    cursor = await conn.execute(f"SELECT COUNT(*) AS n FROM {table}")
    row = await cursor.fetchone()
    return int(row["n"])


async def test_empty_db_migrates(db_path):
    """空库迁移后三张表（recordings/tasks/schema_migrations）存在。"""
    await run_migrations(db_path)
    async with db(db_path) as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name IN ('recordings','tasks','schema_migrations')"
        )
        names = {row["name"] for row in await cursor.fetchall()}
    assert names == {"recordings", "tasks", "schema_migrations"}


async def test_migrate_twice_is_idempotent(db_path):
    """重复启动（重复迁移）不抛错：记录已应用的迁移并跳过。"""
    await run_migrations(db_path)


async def test_idempotency_migration_adds_columns_and_unique_index(db_path):
    await run_migrations(db_path)
    async with db(db_path) as conn:
        cursor = await conn.execute("PRAGMA table_info(recordings)")
        columns = {row["name"] for row in await cursor.fetchall()}
        assert {"idempotency_key", "file_sha256"} <= columns

        cursor = await conn.execute("PRAGMA index_list(recordings)")
        indexes = {row["name"] for row in await cursor.fetchall()}
        assert "idx_recordings_idempotency_key" in indexes


async def test_idempotency_migration_upgrades_existing_database(db_path):
    """已有 001 数据升级后保留原行，并将新增幂等字段置空。"""
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            "CREATE TABLE schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL);"
            + (MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8")
        )
        now = now_utc_ms()
        await conn.execute(
            "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
            ("001_init.sql", now),
        )
        await conn.execute(
            _INSERT_RECORDING,
            ("legacy", "old.wav", "recordings/old.wav", "wav", 10, now, now),
        )
        await conn.commit()

    await run_migrations(db_path)
    async with db(db_path) as conn:
        cursor = await conn.execute(
            "SELECT id, idempotency_key, file_sha256 FROM recordings WHERE id='legacy'"
        )
        row = dict(await cursor.fetchone())
        assert row == {
            "id": "legacy", "idempotency_key": None, "file_sha256": None
        }
        cursor = await conn.execute(
            "SELECT name FROM schema_migrations ORDER BY name"
        )
        assert [r["name"] for r in await cursor.fetchall()] == [
            "001_init.sql", "002_upload_idempotency.sql"
        ]
    await run_migrations(db_path)


async def test_foreign_key_enforced(db_path):
    """外键约束：tasks.recording_id 引用不存在的录音时写入失败。"""
    await run_migrations(db_path)
    now = now_utc_ms()
    async with db(db_path) as conn:
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                _INSERT_TASK,
                ("t1", "no-such-recording", "pending", now, now),
            )
            await conn.commit()


async def test_status_and_lifecycle_check_enforced(db_path):
    """CHECK 约束：非法任务状态与非法录音生命周期均被拒绝。"""
    await run_migrations(db_path)
    now = now_utc_ms()
    async with db(db_path) as conn:
        await conn.execute(
            _INSERT_RECORDING,
            ("r1", "a.wav", "recordings/a.wav", "wav", 1024, now, now),
        )
        await conn.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                _INSERT_TASK,
                ("t1", "r1", "running", now, now),
            )
            await conn.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO recordings (id, original_filename, storage_path, extension,"
                " size_bytes, lifecycle, created_at, updated_at)"
                " VALUES ('r2', 'b.mp3', 'recordings/b.mp3', 'mp3', 2048, 'gone', ?, ?)",
                (now, now),
            )
            await conn.commit()


async def test_transaction_rollback(db_path):
    """事务回滚：事务中途异常经 ROLLBACK 后数据不落库。"""
    await run_migrations(db_path)
    now = now_utc_ms()
    async with db(db_path) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute(
                _INSERT_RECORDING,
                ("r1", "a.wav", "recordings/a.wav", "wav", 1024, now, now),
            )
            raise RuntimeError("boom")  # 模拟事务中途失败
        except RuntimeError:
            await conn.rollback()
    async with db(db_path) as conn:
        assert await _count(conn, "recordings") == 0


async def test_transaction_commit_persists(db_path):
    """对照：事务正常 COMMIT 后数据落库（防止回滚用例误绿）。"""
    await run_migrations(db_path)
    now = now_utc_ms()
    async with db(db_path) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute(
            _INSERT_RECORDING,
            ("r1", "a.wav", "recordings/a.wav", "wav", 1024, now, now),
        )
        await conn.commit()
    async with db(db_path) as conn:
        assert await _count(conn, "recordings") == 1
