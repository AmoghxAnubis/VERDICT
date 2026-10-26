import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

Dataset = Annotated[str, Field(pattern=r"^(live|replay:[a-z0-9][a-z0-9_-]{0,63})$")]
Asset = Literal["ETH-USD"]
Source = Literal["coinbase", "kraken", "binance_us", "chainlink", "pyth", "uniswap_v3"]
EXCHANGE_SOURCES: tuple[Source, ...] = ("coinbase", "kraken", "binance_us")
SOURCES: tuple[Source, ...] = (*EXCHANGE_SOURCES, "chainlink", "pyth", "uniswap_v3")
ALL_SOURCES = SOURCES
PositiveDecimal = Annotated[Decimal, Field(gt=0, max_digits=38, decimal_places=18)]
Address = Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{40}$")]
Hash32 = Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{64}$")]


class ChainBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    chain_id: Literal[1] = 1
    number: int = Field(ge=0, strict=True)
    hash: Hash32
    timestamp: AwareDatetime
    tag: Literal["latest", "safe", "finalized"]


class QuoteConversion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    pair: Literal["USDC-USD"] = "USDC-USD"
    feed_address: Address
    round_id: str
    value: PositiveDecimal
    updated_at: AwareDatetime
    maximum_age_seconds: int = Field(ge=1, le=172800)


class SourceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol: Literal["chainlink", "pyth", "uniswap_v3"]
    instrument: str
    verification: Literal["rpc_checked", "hermes_unverified", "synthetic_fixture"]
    dependencies: tuple[str, ...]
    block: ChainBlock | None = None
    contract: Address | None = None
    quote_conversion: QuoteConversion | None = None
    native_wrapping_ratio: Literal["1:1 WETH redemption"] | None = None
    pool_liquidity_raw: str | None = None
    pyth_confidence_usd: Decimal | None = Field(default=None, ge=0)
    pyth_feed_id: str | None = None


class ObservationData(BaseModel):
    """One observation. Decimal values serialize as strings, never binary floats."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    dataset: Dataset = "live"
    asset: Asset = "ETH-USD"
    source: Source = "coinbase"
    source_type: Literal["exchange", "oracle", "dex"] = "exchange"
    provider_event_id: str = Field(min_length=1, max_length=128)
    event_sequence: int = Field(
        default=0, ge=0, le=9223372036854775807, strict=True, exclude_if=lambda value: value == 0
    )
    value: PositiveDecimal
    timestamp: AwareDatetime
    received_at: AwareDatetime
    latency_ms: float = Field(ge=0, allow_inf_nan=False)
    raw_payload: dict
    provenance: SourceProvenance | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def source_metadata_matches(self):
        if (
            self.provenance
            and self.provenance.verification == "synthetic_fixture"
            and (
                not self.dataset.startswith("replay:")
                or self.raw_payload.get("synthetic") is not True
            )
        ):
            raise ValueError(
                "synthetic provenance requires an explicitly marked replay observation"
            )
        expected = {"chainlink": "oracle", "pyth": "oracle", "uniswap_v3": "dex"}
        if self.source in expected:
            if self.source_type != expected[self.source]:
                raise ValueError("source type does not match provider")
            if self.provenance is None or self.provenance.protocol != self.source:
                raise ValueError("oracle and DEX observations require matching provenance")
        elif self.source_type != "exchange" or self.provenance is not None:
            raise ValueError("exchange observations cannot claim oracle provenance")
        return self

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


class Observation(ObservationData):
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
