from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or .env."""

    llm_base_url: str = Field(default="http://llm:8000/v1")
    llm_model: str = Field(default="qwen3")
    llm_api_key: str = Field(default="dummy")
    ixsmi_path: str = Field(default="/usr/local/corex-4.4.0/bin/ixsmi")
    ixsmi_timeout_seconds: float = Field(default=5.0, gt=0)
    gpu_sampling_interval_seconds: float = Field(default=0.2, gt=0)
    gpu_sampling_window_seconds: float = Field(default=5.0, gt=0)
    agent_port: int = Field(default=8001, ge=1, le=65535)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
