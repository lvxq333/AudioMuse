"""Mock ASR（转写）服务：模拟真实 ASR 的行为与失败率。

- 随机耗时 5~15 秒（随机源可注入，测试用固定值消除随机）；
- 约 20% 概率失败（阈值 0.2，随机源可注入）；
- asyncio.sleep 天然可取消：外部停止该任务时立即中止，不留后台等待；
- 产出"带录音片段标识的固定文本"，模拟不同录音得到不同识别结果。
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

logger = logging.getLogger(__name__)

FAILURE_THRESHOLD = 0.2      # 约 20% 失败
MIN_DURATION_SECONDS = 5     # 模拟转写耗时下限
MAX_DURATION_SECONDS = 15    # 模拟转写耗时上限

# 简单"识别文本"素材：用录音 ID 前缀做片段标识，不同录音文本不同
_TRANSCRIPT_TEMPLATES = (
    "这是第 {seq} 段录音的转写结果：会议讨论了项目排期与资源分配。",
    "转写 {seq}：主讲人汇报了本周进展，并提出了三个待确认事项。",
    "第 {seq} 段内容：QA 环节记录了关于接口联调的若干问题。",
)


class AsrFailure(Exception):
    """模拟 ASR 转写失败（约 20% 概率触发）。"""


async def transcribe(
    *,
    recording_id: str,
    task_id: str,
    attempt_no: int,
    min_seconds: int = MIN_DURATION_SECONDS,
    max_seconds: int = MAX_DURATION_SECONDS,
    failure_threshold: float = FAILURE_THRESHOLD,
    rng: Optional[random.Random] = None,
    sleep: Optional[object] = None,
) -> str:
    """模拟一次 ASR 转写，返回 transcript 文本；失败抛 AsrFailure。

    rng / sleep 可注入以便确定性测试：
      - rng：控制耗时与成败的随机源；
      - sleep：可替换为不真实等待的替身（默认依赖 asyncio.sleep 的
        可取消性做取消测试）。
    """
    rng = rng or random.Random()
    # 失败判定（先于耗时，避免"失败任务还白等 5~15 秒"）
    if rng.random() < failure_threshold:
        logger.info("ASR 模拟失败 task_id=%s attempt_no=%d", task_id, attempt_no)
        raise AsrFailure("模拟 ASR 转写失败")

    duration = rng.uniform(min_seconds, max_seconds)
    logger.info(
        "ASR 转写开始 task_id=%s recording_id=%s 预计 %.1fs",
        task_id, recording_id, duration,
    )
    if sleep is None:
        await asyncio.sleep(duration)
    else:
        await sleep(duration)

    template = rng.choice(_TRANSCRIPT_TEMPLATES)
    seq = recording_id[:8]  # 取 UUID 前 8 位做片段标识，保证同一录音文本稳定
    logger.info("ASR 转写完成 task_id=%s", task_id)
    return template.format(seq=seq)
