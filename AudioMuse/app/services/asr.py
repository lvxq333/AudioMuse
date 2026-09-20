"""ASR 适配层：支持真实 OpenAI 兼容转写接口，并保留本地 Mock 兜底。

配置 ``AUDIOMUSE_ASR_API_KEY`` 后，请求 ``/audio/transcriptions``；未配置时
沿用可控耗时和失败率的 Mock，方便本地开发与测试。真实模式只读取服务端已落盘的
录音文件，密钥不会发送给浏览器，也不会写入日志。
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import random
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

FAILURE_THRESHOLD = 0.2
MIN_DURATION_SECONDS = 5
MAX_DURATION_SECONDS = 15

_TRANSCRIPT_TEMPLATES = (
    "这是第 {seq} 段录音的转写结果：会议讨论了项目排期与资源分配。",
    "转写 {seq}：主讲人汇报了本周进展，并提出了三个待确认事项。",
    "第 {seq} 段内容：QA 环节记录了关于接口联调的若干问题。",
)


class AsrFailure(Exception):
    """可重试的 ASR 调用失败。"""


async def _mock_transcribe(
    *,
    recording_id: str,
    task_id: str,
    attempt_no: int,
    min_seconds: float,
    max_seconds: float,
    failure_threshold: float,
    rng: Optional[random.Random],
    sleep: Optional[object],
) -> str:
    rng = rng or random.Random()
    if rng.random() < failure_threshold:
        logger.info("ASR Mock 失败 task_id=%s attempt_no=%d", task_id, attempt_no)
        raise AsrFailure("模拟 ASR 转写失败")

    duration = rng.uniform(min_seconds, max_seconds)
    logger.info("ASR Mock 开始 task_id=%s 预计 %.1fs", task_id, duration)
    if sleep is None:
        await asyncio.sleep(duration)
    else:
        await sleep(duration)
    return rng.choice(_TRANSCRIPT_TEMPLATES).format(seq=recording_id[:8])


async def _real_transcribe(
    *,
    audio_path: Path,
    original_filename: str,
    task_id: str,
    api_key: str,
    base_url: str,
    model: str,
    timeout_seconds: float,
    language: str,
    prompt: str,
    transport: Optional[httpx.AsyncBaseTransport],
) -> str:
    if not audio_path.is_file():
        raise AsrFailure("待转写音频文件不存在")

    url = f"{base_url.rstrip('/')}/audio/transcriptions"
    content_type = mimetypes.guess_type(original_filename)[0] or "application/octet-stream"
    data = {"model": model, "response_format": "json"}
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt

    logger.info("真实 ASR 调用开始 task_id=%s model=%s", task_id, model)
    try:
        with audio_path.open("rb") as audio_file:
            files = {"file": (original_filename, audio_file, content_type)}
            async with httpx.AsyncClient(
                timeout=timeout_seconds, transport=transport
            ) as client:
                response = await client.post(
                    url,
                    data=data,
                    files=files,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise AsrFailure("ASR 调用超时") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        logger.warning("ASR HTTP 错误 task_id=%s status=%s", task_id, status)
        raise AsrFailure(f"ASR HTTP 错误: {status}") from exc
    except httpx.HTTPError as exc:
        raise AsrFailure("ASR 网络响应异常") from exc
    except OSError as exc:
        raise AsrFailure("无法读取待转写音频") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise AsrFailure("ASR 响应不是合法 JSON") from exc
    transcript = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(transcript, str) or not transcript.strip():
        raise AsrFailure("ASR 响应缺少有效 text 字段")
    logger.info("真实 ASR 调用完成 task_id=%s", task_id)
    return transcript.strip()


async def transcribe(
    *,
    recording_id: str,
    task_id: str,
    attempt_no: int,
    audio_path: Optional[Path] = None,
    original_filename: str = "audio.wav",
    api_key: str = "",
    base_url: str = "https://api.openai.com/v1",
    model: str = "whisper-1",
    timeout_seconds: float = 120.0,
    language: str = "zh",
    prompt: str = "",
    min_seconds: float = MIN_DURATION_SECONDS,
    max_seconds: float = MAX_DURATION_SECONDS,
    failure_threshold: float = FAILURE_THRESHOLD,
    rng: Optional[random.Random] = None,
    sleep: Optional[object] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> str:
    """转写单个音频。配置 API Key 时走真实接口，否则使用本地 Mock。"""
    if not api_key:
        return await _mock_transcribe(
            recording_id=recording_id,
            task_id=task_id,
            attempt_no=attempt_no,
            min_seconds=min_seconds,
            max_seconds=max_seconds,
            failure_threshold=failure_threshold,
            rng=rng,
            sleep=sleep,
        )
    if audio_path is None:
        raise AsrFailure("真实 ASR 未收到音频文件路径")
    return await _real_transcribe(
        audio_path=Path(audio_path),
        original_filename=original_filename,
        task_id=task_id,
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout_seconds=timeout_seconds,
        language=language,
        prompt=prompt,
        transport=transport,
    )
