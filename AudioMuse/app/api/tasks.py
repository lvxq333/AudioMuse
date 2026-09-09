"""提供任务状态查询和失败任务手动重试接口。"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.config import get_settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.responses import ok
from app.db.connection import db as db_ctx
from app.db.constants import TaskStatus
from app.db.repository import get_task_by_id, retry_reset_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _validate_id(task_id: str) -> None:
    """格式校验：非法 UUID 一律 400（与题目"ID 格式合法且存在"一致）。"""
    try:
        uuid.UUID(task_id)
    except (ValueError, AttributeError, TypeError):
        raise BadRequestError("task_id 格式不合法（应为 UUID）") from None


class TaskOut(BaseModel):
    """任务状态响应。"""

    task_id: str
    recording_id: str
    status: str          # pending/transcribing/summarizing/done/failed
    attempt_no: int
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: int
    updated_at: int
    started_at: Optional[int] = None
    finished_at: Optional[int] = None


@router.get("/{task_id}")
async def get_task(request: Request, task_id: str):
    """查询任务状态（processing 中直接体现 transcribing/summarizing 阶段）。"""
    settings = get_settings()
    _validate_id(task_id)

    async with db_ctx(settings.database_path) as conn:
        row = await get_task_by_id(conn, task_id)
    if row is None:
        raise NotFoundError("任务不存在")

    task_out = TaskOut(
        task_id=row["id"],
        recording_id=row["recording_id"],
        status=row["status"],
        attempt_no=row["attempt_no"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )
    return ok(
        data=task_out.model_dump(),
        request_id=getattr(request.state, "request_id", None),
    )


@router.post("/{task_id}/retry")
async def retry_task(request: Request, task_id: str):
    """为失败任务开启新的处理轮次并重新加入待处理队列。

    - 404：任务不存在；
    - 409：状态不允许重试、录音正在删除，或并发重试已被其他请求抢先；
    - 202：重试已受理，返回 task_id / status=pending / attempt_no（已 +1）。
    """
    settings = get_settings()
    _validate_id(task_id)

    # 流程 1：确认任务存在且当前状态允许重试。
    async with db_ctx(settings.database_path) as conn:
        row = await get_task_by_id(conn, task_id)
        if row is None:
            raise NotFoundError("任务不存在")
        if row["status"] != TaskStatus.FAILED.value:
            raise ConflictError(
                f"当前任务状态为 {row['status']}，仅 failed 状态可重试"
            )
        resetted = await retry_reset_task(conn, task_id=task_id)
    if not resetted:
        # 流程 2：条件更新失败时返回状态冲突，避免重复重试或删除竞争。
        raise ConflictError("任务状态已变化或录音正在删除，无法重试")

    # 流程 3：返回新业务轮次，任务由消费者异步领取。
    return ok(
        data={
            "task_id": task_id,
            "status": TaskStatus.PENDING.value,
            "attempt_no": row["attempt_no"] + 1,  # 新一轮轮次号
        },
        status_code=202,
        request_id=getattr(request.state, "request_id", None),
    )
