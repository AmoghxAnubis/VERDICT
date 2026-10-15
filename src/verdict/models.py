import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

Dataset = Annotated[str, Field(pattern=r"^(live|replay:[a-z0-9][a-z0-9_-]{0,63})$")]
Asset = Literal["ETH-USD"]
PositiveDecimal = Annotated[Decimal, Field(gt=0, max_digits=38, decimal_places=18)]


class Observation(BaseModel):
    """One observation. Decimal values serialize as strings, never binary floats."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    dataset: Dataset = "live"
    asset: Asset = "ETH-USD"
    source: Literal["coinbase"] = "coinbase"
    source_type: Literal["exchange"] = "exchange"
    provider_event_id: str = Field(min_length=1, max_length=128)
    value: PositiveDecimal
    timestamp: AwareDatetime
    received_at: AwareDatetime
    latency_ms: float = Field(ge=0, allow_inf_nan=False)
    raw_payload: dict

    @field_validator("value", mode="before")
    @classmethod
    def reject_float_price(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError("prices must use decimal strings, Decimal, or integers")
        return value

    @field_validator("timestamp", "received_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @property
    def observation_id(self) -> str:
        identity = [
            self.schema_version,
            self.dataset,
            self.source,
            self.asset,
            self.provider_event_id,
        ]
        return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()

    def clock_skew_seconds(self) -> float:
        return (self.timestamp - self.received_at).total_seconds()
