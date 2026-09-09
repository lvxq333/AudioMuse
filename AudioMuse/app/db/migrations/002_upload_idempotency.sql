-- 002_upload_idempotency.sql：上传幂等键与文件内容指纹

ALTER TABLE recordings
    ADD COLUMN idempotency_key TEXT;

ALTER TABLE recordings
    ADD COLUMN file_sha256 TEXT
    CHECK (file_sha256 IS NULL OR length(file_sha256) = 64);

CREATE UNIQUE INDEX idx_recordings_idempotency_key
    ON recordings (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
