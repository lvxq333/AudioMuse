"""录音接口。P0-03：POST /v1/recordings 上传。"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Optional

import aiosqlite
from fastapi import APIRouter, File, Header, Query, Request, UploadFile

from app.config import get_settings
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.core.responses import ok
from app.db.connection import db as db_ctx
from app.db.constants import TaskStatus
from app.db.repository import (
    create_recording_and_task,
    delete_recording_row,
    get_recording_by_idempotency_key,
    get_recording_with_task,
    list_recordings as repo_list_recordings,
    mark_recording_deleting,
)
from app.services.storage import persist_upload, remove_file_with_retry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recordings", tags=["recordings"])

_MAX_IDEMPOTENCY_KEY_LENGTH = 128


def _validate_recording_id(recording_id: str) -> None:
    """格式校验：非法 UUID 一律 400。"""
    try:
        uuid.UUID(recording_id)
    except (ValueError, AttributeError, TypeError):
        raise BadRequestError("recording_id 格式不合法（应为 UUID）") from None


def _normalize_idempotency_key(value: Optional[str]) -> Optional[str]:
    """校验可选幂等键；键作为不透明字符串保存，仅去除首尾空白。"""
    if value is None:
        return None
    key = value.strip()
    if not key:
        raise BadRequestError("Idempotency-Key 不能为空")
    if len(key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise BadRequestError(
            f"Idempotency-Key 长度不能超过 {_MAX_IDEMPOTENCY_KEY_LENGTH} 个字符"
        )
    return key


async def _cleanup_new_upload(settings, stored) -> bool:
    """清理未能创建数据库记录的新文件；失败时记录可定位信息。"""
    target = Path(settings.data_dir) / stored.storage_relpath
    cleaned = await remove_file_with_retry(target)
    if not cleaned:
        logger.error(
            "孤儿文件清理失败，请稍后重试或人工处理 "
            "recording_id=%s storage_relpath=%s",
            stored.recording_id, stored.storage_relpath,
        )
    return cleaned


@router.post("")
async def upload_recording(
    request: Request,
    file: Optional[UploadFile] = File(default=None),
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key"
    ),
):
    """上传录音：落盘并创建 pending 任务后立即返回 202，不等待处理。"""
    settings = get_settings()
    if file is None:
        raise BadRequestError("缺少文件字段 file")
    idempotency_key = _normalize_idempotency_key(idempotency_key)

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
                idempotency_key=idempotency_key,
                file_sha256=stored.file_sha256,
            )
    except aiosqlite.IntegrityError as exc:
        # 同一幂等键的并发请求由唯一索引裁决；输家清理自己的文件后复用赢家。
        existing = None
        if idempotency_key is not None:
            try:
                async with db_ctx(settings.database_path) as conn:
                    existing = await get_recording_by_idempotency_key(
                        conn, idempotency_key
                    )
            except Exception as lookup_exc:
                await _cleanup_new_upload(settings, stored)
                logger.error(
                    "幂等冲突后查询原记录失败 recording_id=%s",
                    stored.recording_id,
                    exc_info=lookup_exc,
                )
                raise AppError(
                    "录音上传失败，请稍后重试", code="UPLOAD_FAILED"
                ) from lookup_exc
        if not await _cleanup_new_upload(settings, stored):
            raise AppError(
                "上传清理失败，请稍后重试", code="UPLOAD_CLEANUP_FAILED"
            ) from exc
        if existing is None:
            logger.error(
                "上传完整性约束冲突但未找到幂等记录 recording_id=%s",
                stored.recording_id,
                exc_info=exc,
            )
            raise AppError("录音上传失败，请稍后重试", code="UPLOAD_FAILED") from exc
        if existing["lifecycle"] != "active":
            raise ConflictError("该 Idempotency-Key 对应的录音正在删除")
        if existing["file_sha256"] != stored.file_sha256:
            raise ConflictError("Idempotency-Key 已用于不同的文件")
        logger.info(
            "命中上传幂等记录 recording_id=%s task_id=%s",
            existing["recording_id"], existing["task_id"],
        )
        return ok(
            data={
                "recording_id": existing["recording_id"],
                "task_id": existing["task_id"],
                "status": existing["status"],
            },
            status_code=200,
            request_id=getattr(request.state, "request_id", None),
        )
    except Exception as exc:
        # 文件已落盘但 DB 写入失败：先重试清理孤儿文件（相对路径入日志供人工兜底），
        # 再抛出语义明确的业务错误——避免用户看到裸 500 误以为上传成功
        await _cleanup_new_upload(settings, stored)
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


@router.get("")
async def list_recordings(
    request: Request,
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(
        default=20, ge=1, le=100, description="每页条数 1~100"
    ),
):
    """录音列表：分页、按创建时间倒序，含每条的最新任务状态。"""
    settings = get_settings()
    async with db_ctx(settings.database_path) as conn:
        result = await repo_list_recordings(conn, page=page, page_size=page_size)

    return ok(
        data={
            "items": result["items"],
            "page": page,
            "page_size": page_size,
            "total": result["total"],
        },
        request_id=getattr(request.state, "request_id", None),
    )


@router.get("/{recording_id}")
async def get_recording(request: Request, recording_id: str):
    """录音详情；处理完成（done）时包含 transcript 与结构化摘要。"""
    settings = get_settings()
    _validate_recording_id(recording_id)

    async with db_ctx(settings.database_path) as conn:
        row = await get_recording_with_task(conn, recording_id)
    if row is None:
        raise NotFoundError("录音不存在")

    summary = None
    if row.get("summary_json"):
        try:
            summary = json.loads(row["summary_json"])
        except (TypeError, ValueError):
            logger.warning("录音摘要数据损坏 recording_id=%s", recording_id)
            summary = None

    return ok(
        data={
            "recording_id": row["recording_id"],
            "original_filename": row["original_filename"],
            "extension": row["extension"],
            "size_bytes": row["size_bytes"],
            "created_at": row["created_at"],
            "task_id": row["task_id"],
            "status": row["status"],
            "attempt_no": row["attempt_no"],
            "transcript": row.get("transcript"),
            "summary": summary,
            "error_code": row.get("error_code"),
            "error_message": row.get("error_message"),
            "finished_at": row.get("finished_at"),
        },
        request_id=getattr(request.state, "request_id", None),
    )


@router.delete("/{recording_id}", status_code=204)
async def delete_recording(request: Request, recording_id: str):
    """删除录音：置 deleting → 取消进行中处理 → 删文件 → 删数据库行。

    - 404：录音不存在（或行已被删）；
    - 5xx(RECORDING_DELETE_FAILED)：文件清理失败——保留 deleting 状态，
      再次 DELETE 会“续扫”继续清理（已支持 deleting 行重试）；
    - 204：删除完成（无响应体）。
    """
    settings = get_settings()
    _validate_recording_id(recording_id)

    async with db_ctx(settings.database_path) as conn:
        row = await get_recording_with_task(conn, recording_id)
    if row is None:
        raise NotFoundError("录音不存在")

    # 1) 置 deleting（首次或续扫皆可：已是 deleting 也继续走清理）
    async with db_ctx(settings.database_path) as conn:
        await mark_recording_deleting(conn, recording_id=recording_id)

    # 2) 若该任务正在处理：取消处理器（消费者停手并继续处理其他录音）
    registry: "TaskRegistry" = request.app.state.registry
    registry.cancel(row["task_id"])

    # 3) 删除磁盘文件（不存在视为成功；失败不删行，保留 deleting 供续扫）
    storage_path = Path(settings.data_dir) / row["storage_path"]
    if not await remove_file_with_retry(storage_path):
        logger.error(
            "删除录音文件失败（保留 deleting 供重试） recording_id=%s",
            recording_id,
        )
        raise AppError(
            "录音删除失败（文件清理失败），请稍后重试",
            code="RECORDING_DELETE_FAILED",
        )

    # 4) 删除数据库行（tasks 经外键级联删除）
    async with db_ctx(settings.database_path) as conn:
        deleted = await delete_recording_row(conn, recording_id=recording_id)
    if not deleted:
        raise NotFoundError("录音不存在")

    logger.info("录音已删除 recording_id=%s", recording_id)
    return None  # 204 无响应体
