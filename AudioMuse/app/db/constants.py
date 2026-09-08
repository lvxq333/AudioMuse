"""领域常量：任务/录音状态、错误码与统一时间函数。

与 001_init.sql 中的 CHECK 约束保持一致；应用层一律引用本模块常量，
避免散落魔法字符串。时间统一为 epoch 毫秒 INTEGER（UTC）。
"""

from __future__ import annotations

import datetime
from enum import Enum


def now_utc_ms() -> int:
    """当前 UTC 时间的 Unix 毫秒时间戳。"""
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)


class TaskStatus(str, Enum):
    """处理任务状态机：pending → transcribing → summarizing → done；任一环节失败进入 failed。"""

    PENDING = "pending"
    TRANSCRIBING = "transcribing"
    SUMMARIZING = "summarizing"
    DONE = "done"
    FAILED = "failed"


class Lifecycle(str, Enum):
    """录音删除协调状态，与处理状态分离。"""

    ACTIVE = "active"
    DELETING = "deleting"


class ErrorCode(str, Enum):
    """任务失败错误码（写入 tasks.error_code）。"""

    ASR_FAILED = "ASR_FAILED"
    ASR_TIMEOUT = "ASR_TIMEOUT"
    LLM_FAILED = "LLM_FAILED"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_INVALID_OUTPUT = "LLM_INVALID_OUTPUT"
    PROCESSING_CANCELLED = "PROCESSING_CANCELLED"
    WORKER_INTERRUPTED = "WORKER_INTERRUPTED"
