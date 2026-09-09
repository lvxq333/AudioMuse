"""真实 LLM 冒烟脚本：验证 API Key 可用性与输出结构。

用法（项目根目录执行）：:

    .venv/bin/python scripts/smoke_llm.py

读取项目根 .env（或环境变量）中的 AUDIOMUSE_LLM_* 配置，对一段示例
transcript 调用 LlmClient.summarize 并打印结构化结果。
不写数据库、不改任何文件；仅用于人工验证真实 LLM 链路。

退出码：0=成功；1=LLM 调用失败；2=未配置 API Key。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# 保证从任意工作目录运行时都能 import app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.services.llm import LlmError, build_llm_client  # noqa: E402

SAMPLE_TRANSCRIPT = (
    "会议讨论了 Q3 项目排期：张三负责接口联调，预计下周五完成；"
    "李四跟进测试环境搭建；需要确认服务器预算后启动开发。"
)


async def main() -> int:
    settings = get_settings()
    if not settings.llm_api_key:
        print("未配置 AUDIOMUSE_LLM_API_KEY（.env 或环境变量），无法冒烟。",
              file=sys.stderr)
        return 2

    print(
        f"调用 LLM: base_url={settings.llm_base_url or '(默认 OpenAI)'} "
        f"model={settings.llm_model or '(默认)'} "
        f"timeout={settings.llm_timeout_seconds}s"
    )
    client = build_llm_client(settings)
    try:
        result = await client.summarize(SAMPLE_TRANSCRIPT, task_id="smoke")
    except LlmError as exc:
        print(f"LLM 调用失败 code={exc.code}: {exc.message}", file=sys.stderr)
        return 1

    print("=== LLM 结构化结果 ===")
    print(
        json.dumps(
            {
                "summary": result.summary,
                "key_points": result.key_points,
                "todos": result.todos,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
