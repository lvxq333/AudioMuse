"""应用配置。

所有可配置项均可通过环境变量覆盖，前缀为 ``AUDIOMUSE_``，
也可通过项目根目录的 ``.env`` 文件提供（见 ``.env.example``）。
"""

from functools import lru_cache
from pathlib import Path

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

    # 异步流水线：全局最多同时处理的任务数（P0 起生效）
    max_concurrency: int = 3

    # --- LLM（真实摘要阶段使用，P0 起实现；密钥只从环境读取，不写日志） ---
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    # LLM 摘要请求超时（秒）
    llm_timeout_seconds: float = 30.0

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
