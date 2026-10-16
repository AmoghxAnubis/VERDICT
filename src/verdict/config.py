from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VERDICT_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///data/verdict.db"
    poll_interval_seconds: float = Field(default=2, ge=1, le=300)
    request_timeout_seconds: float = Field(default=10, gt=0, le=60)
    freshness_seconds: int = Field(default=30, ge=1, le=300)
    max_future_skew_seconds: int = Field(default=5, ge=0, le=60)
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
