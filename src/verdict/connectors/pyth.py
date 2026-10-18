import time
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from verdict.connectors.common import check_clock, epoch_seconds
from verdict.models import Dataset, Observation, SourceProvenance

ETH_USD_ID = "ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace"
HERMES_URL = "https://hermes.pyth.network"


class Price(BaseModel):
    price: str = Field(pattern=r"^[0-9]{1,19}$")
    conf: str = Field(pattern=r"^[0-9]{1,20}$")
    expo: int = Field(ge=-18, le=0, strict=True)
    publish_time: int = Field(gt=0, strict=True)


class Metadata(BaseModel):
    slot: int = Field(ge=0, le=9223372036854775807, strict=True)


class ParsedPrice(BaseModel):
    id: Literal[ETH_USD_ID]
    price: Price
    metadata: Metadata


class Binary(BaseModel):
    encoding: Literal["hex"]
    data: list[str] = Field(min_length=1, max_length=16)


class HermesResponse(BaseModel):
    binary: Binary
    parsed: list[ParsedPrice] = Field(min_length=1, max_length=1)


def normalize_price(
    payload: dict, *, received_at: datetime, latency_ms: float, dataset: Dataset = "live"
) -> Observation:
    response = HermesResponse.model_validate(payload)
    for message in response.binary.data:
        if (
            not message
            or len(message) % 2
            or len(message) > 1_000_000
            or any(character not in "0123456789abcdefABCDEF" for character in message)
        ):
            raise ValueError("invalid Pyth update message")
        try:
            bytes.fromhex(message)
        except ValueError as error:
            raise ValueError("invalid Pyth update message") from error
    parsed = response.parsed[0]
    price = parsed.price
    if not 0 < int(price.price) <= 9223372036854775807 or int(price.conf) > 18446744073709551615:
        raise ValueError("Pyth price or confidence is outside its wire integer range")
    with localcontext() as context:
        context.prec = 80
        value = Decimal(price.price).scaleb(price.expo)
        confidence = Decimal(price.conf).scaleb(price.expo)
    return Observation(
        dataset=dataset,
        source="pyth",
        source_type="oracle",
        provider_event_id=f"{price.publish_time}:{parsed.metadata.slot}",
        event_sequence=parsed.metadata.slot,
        value=value,
        timestamp=epoch_seconds(price.publish_time),
        received_at=received_at,
        latency_ms=latency_ms,
        raw_payload=payload,
        provenance=SourceProvenance(
            protocol="pyth",
            instrument="ETH/USD",
            verification="hermes_unverified",
            dependencies=("pyth_network", "hermes_service"),
            pyth_feed_id=ETH_USD_ID,
            pyth_confidence_usd=confidence,
        ),
    )


class PythConnector:
    def __init__(
        self,
        client: httpx.AsyncClient,
        max_future_skew_seconds: float = 5,
        *,
        hermes_url: str = HERMES_URL,
        api_key: str = "",
    ):
        self.client = client
        self.max_future_skew_seconds = max_future_skew_seconds
        self.hermes_url = hermes_url.rstrip("/")
        self.api_key = api_key

    async def fetch(self) -> Observation:
        started = time.perf_counter()
        response = await self.client.get(
            f"{self.hermes_url}/v2/updates/price/latest",
            headers={
                "Authorization": f"Bearer {self.api_key}",
            }
            if self.api_key
            else {},
            params={
                "ids[]": ETH_USD_ID,
                "encoding": "hex",
                "parsed": "true",
            },
        )
        received_at = datetime.now(UTC)
        response.raise_for_status()
        observation = normalize_price(
            response.json(),
            received_at=received_at,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        return check_clock(observation, self.max_future_skew_seconds)
