"""LLM 摘要适配层：调用真实 LLM（OpenAI 兼容接口）并做结构校验。

职责边界（对应"两层校验"中的第一层——业务结构校验）：
- 把 transcript 交给 LLM，要求返回 JSON 对象 {summary, key_points, todos}；
- 对返回内容做结构校验（summary 非空字符串；key_points/todos 为
  字符串数组，缺失按空数组处理）——不合格抛 LlmInvalidOutput，
  由调用方把任务置为 failed + LLM_INVALID_OUTPUT；
- 超时抛 LlmTimeout（LLM_TIMEOUT），网络/HTTP 异常抛 LlmError（LLM_FAILED）；
- 无 API Key 时自动降级为本地 mock（README 需说明此项会降低得分）；
- httpx 异步调用支持 asyncio 取消（外部停止任务时立即中止请求）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)


# 结构化结果与异常

@dataclass
class LlmResult:
    """LLM 输出通过结构校验后的结果。"""

    summary: str
    key_points: List[str] = field(default_factory=list)
    todos: List[str] = field(default_factory=list)

    def to_json(self) -> str:
        """序列化为落库用的 JSON 字符串。"""
        return json.dumps(
            {"summary": self.summary, "key_points": self.key_points,
             "todos": self.todos},
            ensure_ascii=False,
        )


class LlmError(Exception):
    """LLM 调用失败基类。"""

    code: str = "LLM_FAILED"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class LlmTimeout(LlmError):
    code = "LLM_TIMEOUT"


class LlmInvalidOutput(LlmError):
    code = "LLM_INVALID_OUTPUT"


# 业务结构校验

_SYSTEM_PROMPT = (
    "你是会议纪要助手。请根据用户提供的录音转写文本，输出严格的 JSON 对象，"
    '格式为 {"summary": "一句话摘要", "key_points": ["要点1", ...], '
    '"todos": ["待办1", ...]}，不要输出任何 JSON 以外的内容。'
)


def _extract_json_object(content: str) -> dict:
    """从模型回复中解析 JSON 对象（容忍 ```json 代码围栏等包裹）。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[len("json"):].lstrip()
        text = text.strip()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        # 兜底：截取首尾花括号之间的内容再试一次
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            data = json.loads(text[start : end + 1])
        else:
            raise
    if not isinstance(data, dict):
        raise ValueError("LLM 输出不是 JSON 对象")
    return data


def _validate_structure(data: dict) -> LlmResult:
    """业务结构校验：summary 非空字符串；key_points/todos 为字符串数组。"""
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise LlmInvalidOutput("summary 缺失或不是非空字符串")

    key_points = data.get("key_points", [])
    todos = data.get("todos", [])
    for name, value in (("key_points", key_points), ("todos", todos)):
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise LlmInvalidOutput(f"{name} 必须是字符串数组")
    return LlmResult(
        summary=summary.strip(), key_points=key_points, todos=todos
    )


def parse_and_validate(content: str) -> LlmResult:
    """解析并校验 LLM 回复；不合格抛 LlmInvalidOutput。"""
    if not isinstance(content, str) or not content.strip():
        raise LlmInvalidOutput("LLM content 必须是非空字符串")
    try:
        data = _extract_json_object(content)
    except (TypeError, ValueError) as exc:
        raise LlmInvalidOutput("LLM 返回内容无法解析为 JSON 对象") from exc
    return _validate_structure(data)


# LLM 客户端

class LlmClient:
    """对 OpenAI 兼容 /chat/completions 的轻量客户端；无 Key 时降级 mock。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._transport = transport  # 测试注入 httpx.MockTransport

    async def summarize(self, transcript: str, *, task_id: str) -> LlmResult:
        if not self._api_key:
            logger.warning(
                "未配置 LLM API Key，摘要使用本地 mock（README 已注明降分影响）"
            )
            return self._mock_result(transcript)

        url = f"{self._base_url}/chat/completions"
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        logger.info("LLM 调用开始 task_id=%s model=%s", task_id, self._model)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
        except httpx.TimeoutException as exc:
            logger.warning("LLM 超时 task_id=%s", task_id)
            raise LlmTimeout("LLM 调用超时") from exc
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "LLM HTTP 错误 task_id=%s status=%s",
                task_id, exc.response.status_code,
            )
            raise LlmError(f"LLM HTTP 错误: {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise LlmError("LLM 响应异常") from exc

        try:
            data = resp.json()
        except ValueError as exc:
            raise LlmInvalidOutput("LLM 响应体不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise LlmInvalidOutput("LLM 响应体必须是 JSON 对象")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LlmInvalidOutput("LLM choices 必须是非空数组")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise LlmInvalidOutput("LLM choices 首项必须是对象")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise LlmInvalidOutput("LLM message 必须是对象")
        content = message.get("content")
        result = parse_and_validate(content)
        logger.info("LLM 调用完成 task_id=%s", task_id)
        return result

    @staticmethod
    def _mock_result(transcript: str) -> LlmResult:
        """本地 mock 摘要（无 Key 兜底）：结构合法、内容取自 transcript。"""
        head = transcript.strip()[:50]
        return LlmResult(
            summary=f"（Mock 摘要）本段录音要点：{head}",
            key_points=[f"涉及内容：{head}"],
            todos=["（Mock）待整理后续行动项"],
        )


def build_llm_client(settings) -> LlmClient:
    """按应用配置构造 LLM 客户端。"""
    return LlmClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url or "https://api.openai.com/v1",
        model=settings.llm_model or "gpt-4o-mini",
        timeout_seconds=settings.llm_timeout_seconds,
    )
