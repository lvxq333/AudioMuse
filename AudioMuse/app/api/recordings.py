"""录音接口。P0-03：POST /v1/recordings 上传。"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Request, UploadFile

from app.config import get_settings
from app.core.errors import AppError, BadRequestError
from app.core.responses import ok
from app.db.connection import db as db_ctx
from app.db.constants import TaskStatus
from app.db.repository import create_recording_and_task
from app.services.storage import persist_upload, remove_file_with_retry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recordings", tags=["recordings"])


@router.post("")
async def upload_recording(
    request: Request,
    file: Optional[UploadFile] = File(default=None),
):
    """上传录音：落盘并创建 pending 任务后立即返回 202，不等待处理。"""
    settings = get_settings()
    if file is None:
        raise BadRequestError("缺少文件字段 file")

    stored = await persist_upload(
        file, settings.recordings_dir, max_bytes=settings.max_upload_bytes
    )
    task_id = uuid.uuid4().hex
    try:
        async with db_ctx(settings.database_path) as conn:
            await create_recording_and_task(
                conn,
                recording_id=stored.recording_id,
                task_id=task_id,
                original_filename=stored.original_filename,
                storage_relpath=stored.storage_relpath,
                extension=stored.extension,
                size_bytes=stored.size_bytes,
            )
    except Exception as exc:
        # 文件已落盘但 DB 写入失败：先重试清理孤儿文件（相对路径入日志供人工兜底），
        # 再抛出语义明确的业务错误——避免用户看到裸 500 误以为上传成功
        target = Path(settings.data_dir) / stored.storage_relpath
        if not await remove_file_with_retry(target):
            logger.error(
                "孤儿文件清理失败，请稍后重试或人工处理 "
                "recording_id=%s storage_relpath=%s",
                stored.recording_id, stored.storage_relpath,
            )
        logger.error(
            "上传落库失败 recording_id=%s size=%d",
            stored.recording_id, stored.size_bytes,
            exc_info=exc,
        )
        raise AppError("录音上传失败，请稍后重试", code="UPLOAD_FAILED") from exc

    logger.info(
        "上传完成 recording_id=%s task_id=%s size=%d",
        stored.recording_id, task_id, stored.size_bytes,
    )
    return ok(
        data={
            "recording_id": stored.recording_id,
            "task_id": task_id,
            "status": TaskStatus.PENDING.value,
        },
        status_code=202,
        request_id=getattr(request.state, "request_id", None),
    )
