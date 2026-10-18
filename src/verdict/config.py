from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VERDICT_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///data/verdict.db"
    poll_interval_seconds: float = Field(default=2, ge=1, le=300)
    request_timeout_seconds: float = Field(default=10, gt=0, le=60)
    freshness_seconds: int = Field(default=30, ge=1, le=300)
    max_future_skew_seconds: int = Field(default=5, ge=0, le=60)
    coinbase_stream_enabled: bool = False
    coinbase_websocket_url: str = "wss://ws-feed.exchange.coinbase.com"
    coinbase_stream_timeout_seconds: float = Field(default=10, ge=2, le=60)
    coinbase_recovery_page_size: int = Field(default=1000, ge=1, le=1000)
    coinbase_recovery_max_pages: int = Field(default=20, ge=1, le=100)
    ethereum_rpc_url: str = "https://ethereum-rpc.publicnode.com"
    ethereum_block_tag: Literal["latest", "safe", "finalized"] = "latest"
    onchain_poll_interval_seconds: float = Field(default=15, ge=12, le=300)
    usdc_conversion_max_age_seconds: int = Field(default=86400, ge=1, le=172800)
    pyth_hermes_url: str = "https://hermes.pyth.network"
    pyth_api_key: SecretStr = SecretStr("")
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
