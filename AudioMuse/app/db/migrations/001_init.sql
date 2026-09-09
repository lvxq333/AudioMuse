-- 001_init.sql：录音与处理任务的初始表结构
-- 约定：所有 DDL 幂等（IF NOT EXISTS），重复执行无副作用；
--       时间字段统一存 epoch 毫秒（Unix 毫秒时间戳 INTEGER，应用层 now_utc_ms() 生成）

CREATE TABLE IF NOT EXISTS recordings (
    id                TEXT PRIMARY KEY,
    original_filename TEXT NOT NULL,
    storage_path      TEXT NOT NULL UNIQUE,   -- 服务端生成，客户端不可指定
    extension         TEXT NOT NULL CHECK (extension IN ('wav', 'mp3', 'm4a', 'aac')),
    size_bytes        INTEGER NOT NULL CHECK (size_bytes > 0),
    lifecycle         TEXT NOT NULL DEFAULT 'active'
                      CHECK (lifecycle IN ('active', 'deleting')),
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recordings_created_at
    ON recordings (created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    recording_id  TEXT NOT NULL UNIQUE
                  REFERENCES recordings(id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'transcribing', 'summarizing', 'done', 'failed')),
    attempt_no    INTEGER NOT NULL DEFAULT 1 CHECK (attempt_no >= 1),
    transcript    TEXT,
    summary_json  TEXT,
    error_code    TEXT,
    error_message TEXT,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    started_at    INTEGER,
    finished_at   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_created
    ON tasks (status, created_at);
