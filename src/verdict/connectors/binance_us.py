import time
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from pydantic import BaseModel, Field

from verdict.connectors.common import check_clock, epoch_seconds
from verdict.models import Dataset, Observation

BASE_URL = "https://api.binance.us"


class TradeIdentity(BaseModel):
    id: int = Field(ge=0, strict=True)
    time: int = Field(ge=0, strict=True)


def normalize_trades(
    payload: dict, *, received_at: datetime, latency_ms: float, dataset: Dataset = "live"
) -> Observation:
    # The adapter, not provider-supplied data, pins the requested instrument.
    if payload.get("symbol") != "ETHUSD":
        raise ValueError("expected Binance.US ETHUSD; stablecoin pairs are not USD")
    trades = payload.get("trades")
    if not isinstance(trades, list) or not trades or any(not isinstance(t, dict) for t in trades):
        raise ValueError("Binance.US returned no valid trades")
    parsed = [(TradeIdentity.model_validate(trade), trade) for trade in trades]
    identity, trade = max(parsed, key=lambda item: (item[0].time, item[0].id))
    return Observation(
        dataset=dataset,
        source="binance_us",
        provider_event_id=str(identity.id),
        value=trade.get("price"),
        timestamp=epoch_seconds(Decimal(identity.time) / 1000),
        received_at=received_at,
        latency_ms=latency_ms,
        raw_payload=payload,
    )


class BinanceUSConnector:
    def __init__(self, client: httpx.AsyncClient, max_future_skew_seconds: float = 5):
        self.client = client
        self.max_future_skew_seconds = max_future_skew_seconds

    async def fetch(self) -> Observation:
        started = time.perf_counter()
        response = await self.client.get(
            f"{BASE_URL}/api/v3/trades", params={"symbol": "ETHUSD", "limit": 1}
        )
        received_at = datetime.now(UTC)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        observation = normalize_trades(
            {"symbol": "ETHUSD", "trades": response.json()},
            received_at=received_at,
            latency_ms=elapsed_ms,
        )
        return check_clock(observation, self.max_future_skew_seconds)
