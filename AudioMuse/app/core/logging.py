"""日志配置：保证关键路径（任务生命周期）可通过日志还原。"""

from __future__ import annotations

import logging

_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def setup_logging(level: str = "INFO") -> None:
    """初始化根日志格式与级别。任务阶段日志建议携带 ``task_id``、``attempt_no`` 字段。"""
    logging.basicConfig(level=level.upper(), format=_FORMAT)
