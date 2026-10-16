import time
from datetime import UTC, datetime
from typing import Literal

import httpx
from pydantic import AwareDatetime, BaseModel, Field

from verdict.connectors.common import check_clock
from verdict.models import Dataset, Observation, PositiveDecimal

BASE_URL = "https://api.exchange.coinbase.com"


class CoinbaseTicker(BaseModel):
    trade_id: int = Field(ge=0, strict=True)
    price: PositiveDecimal
    time: AwareDatetime


def normalize_ticker(
    payload: dict,
    *,
    received_at: datetime,
    latency_ms: float,
    dataset: Dataset = "live",
    asset: Literal["ETH-USD"] = "ETH-USD",
) -> Observation:
    # Validate through Observation before price conversion to reject binary floats.
    if isinstance(payload.get("price"), (float, bool)):
        raise ValueError("Coinbase price must be a decimal string")
    ticker = CoinbaseTicker.model_validate(payload)
    return Observation(
        dataset=dataset,
        asset=asset,
        provider_event_id=str(ticker.trade_id),
        value=ticker.price,
        timestamp=ticker.time,
        received_at=received_at,
        latency_ms=latency_ms,
        raw_payload=payload,
    )


class CoinbaseConnector:
    def __init__(self, client: httpx.AsyncClient, max_future_skew_seconds: float = 5):
        self.client = client
        self.max_future_skew_seconds = max_future_skew_seconds

    async def fetch(self) -> Observation:
        started = time.perf_counter()
        response = await self.client.get(f"{BASE_URL}/products/ETH-USD/ticker")
        received_at = datetime.now(UTC)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        observation = normalize_ticker(
            response.json(), received_at=received_at, latency_ms=elapsed_ms
        )
        return check_clock(observation, self.max_future_skew_seconds)
