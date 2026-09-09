"""录音文件本地存储。

- 扩展名与大小校验按实际读取字节计数，不信任 Content-Length；
- 存储文件名由服务端 UUID 生成（{recording_id}.{ext}），
  原始文件名仅作元数据，不进入存储路径；
- 先写临时文件，全部成功后再原子改名（os.replace）到正式位置；
- 流式写入时同步计算 SHA-256，供上传幂等判断使用；
- 任何异常（含超限、零字节）都会清理临时文件，且清理失败不掩盖原始异常。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile

from app.core.errors import (
    BadRequestError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
)

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = frozenset({"wav", "mp3", "m4a", "aac"})
_CHUNK_SIZE = 1024 * 1024


@dataclass
class StoredUpload:
    """一次成功落盘的结果。"""

    recording_id: str       # 服务端生成的录音 ID（UUID hex）
    original_filename: str  # 原始文件名，仅作元数据
    storage_relpath: str    # 相对 data_dir 的存储路径，如 recordings/{id}.wav
    extension: str          # 小写扩展名，wav/mp3/m4a/aac 之一
    size_bytes: int         # 实际写入字节数
    file_sha256: str        # 文件内容 SHA-256，用于上传幂等冲突校验


def _parse_extension(filename: str) -> str:
    """从原始文件名提取并校验小写扩展名。"""
    if not filename:
        raise BadRequestError("缺少文件字段 file")
    # 只取文件名部分，避免路径干扰扩展名判断
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    dot = name.rfind(".")
    if dot <= 0:
        raise UnsupportedMediaTypeError("文件名缺少扩展名，仅支持 wav/mp3/m4a/aac")
    ext = name[dot + 1 :].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UnsupportedMediaTypeError(
            f"不支持的文件类型 .{ext}，仅支持 wav/mp3/m4a/aac"
        )
    return ext


async def persist_upload(
    file: UploadFile, recordings_dir: Path, *, max_bytes: int
) -> StoredUpload:
    """流式校验并保存上传文件，返回 StoredUpload；失败时抛出对应 AppError。"""
    original_filename = file.filename or ""
    extension = _parse_extension(original_filename)

    recording_id = uuid.uuid4().hex
    recordings_dir.mkdir(parents=True, exist_ok=True)
    final_path = recordings_dir / f"{recording_id}.{extension}"
    tmp_path = recordings_dir / f".{recording_id}.{uuid.uuid4().hex[:8]}.tmp"

    size_bytes = 0
    digest = hashlib.sha256()
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = await file.read(_CHUNK_SIZE)
                if not chunk:
                    break
                size_bytes += len(chunk)
                if size_bytes > max_bytes:
                    raise PayloadTooLargeError(
                        f"文件大小超过限制（最大 {max_bytes} 字节）"
                    )
                digest.update(chunk)
                out.write(chunk)
        if size_bytes == 0:
            raise BadRequestError("文件内容为空")
        os.replace(tmp_path, final_path)  # 同目录内原子改名
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)  # 清理半成品，不掩盖原始异常
        except OSError:
            logger.warning("清理临时文件失败: %s", tmp_path, exc_info=True)
        raise

    logger.info(
        "录音文件已保存 recording_id=%s size=%d ext=%s",
        recording_id, size_bytes, extension,
    )
    return StoredUpload(
        recording_id=recording_id,
        original_filename=original_filename,
        storage_relpath=f"recordings/{recording_id}.{extension}",
        extension=extension,
        size_bytes=size_bytes,
        file_sha256=digest.hexdigest(),
    )


async def remove_file_with_retry(
    path: Path, *, attempts: int = 3, base_delay: float = 0.1
) -> bool:
    """删除文件的本地重试框架（短退避，不在此抛错）。

    - 文件本就不存在（missing_ok）视为成功，兼容"已被手动删除"场景；
    - 重试仍失败时返回 False 并记录告警，由调用方决定兜底/人工处理；
    - 更完整的残留文件治理方案记录在 docs/TODO.md。
    """
    for i in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            if i < attempts - 1:
                await asyncio.sleep(base_delay * (i + 1))
    logger.warning("文件删除失败(已重试 %d 次): %s", attempts, path)
    return False
