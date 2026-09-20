"""应用配置。

所有可配置项均可通过环境变量覆盖，前缀为 ``AUDIOMUSE_``，
也可通过项目根目录的 ``.env`` 文件提供（见 ``.env.example``）。
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：app/config.py -> 项目根
ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AUDIOMUSE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AudioMuse 录音转写服务"
    debug: bool = False
    log_level: str = "INFO"

    # 录音文件与 SQLite 数据库的统一数据根目录
    data_dir: Path = ROOT_DIR / "data"

    # 上传限制：单文件最大字节数（默认 50 MiB = 50 * 1024 * 1024）
    max_upload_bytes: int = 50 * 1024 * 1024

    # 全局最多同时处理的任务数。
    max_concurrency: int = 3

    # LLM 配置；密钥只从环境读取，不写日志。
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    # LLM 摘要请求超时（秒）
    llm_timeout_seconds: float = 30.0

    # ASR 配置；有 Key 时调用 OpenAI 兼容 /audio/transcriptions，无 Key 用 Mock。
    asr_api_key: str = ""
    asr_base_url: str = "https://api.openai.com/v1"
    asr_model: str = "whisper-1"
    asr_timeout_seconds: float = Field(default=120.0, gt=0)
    asr_language: str = "zh"
    asr_prompt: str = ""

    # Mock ASR 参数；测试和演示可通过环境变量缩短耗时或调整失败率。
    asr_min_seconds: float = 5.0
    asr_max_seconds: float = 15.0
    asr_failure_threshold: float = 0.2

    # 阶段内自动重试：首次执行失败后最多再试 3 次，退避 1/2/4 秒
    auto_retry_max_retries: int = Field(default=3, ge=0, le=3)
    auto_retry_base_delay_seconds: float = Field(default=1.0, ge=0)

    @property
    def recordings_dir(self) -> Path:
        """录音文件存储目录。"""
        return self.data_dir / "recordings"

    @property
    def database_path(self) -> Path:
        """SQLite 数据库文件路径。"""
        return self.data_dir / "audiomuse.db"


@lru_cache
def get_settings() -> Settings:
    """返回进程级缓存的配置实例。"""
    return Settings()
