import time
from datetime import UTC, datetime

import httpx

from verdict.connectors.common import check_clock, epoch_seconds
from verdict.models import Dataset, Observation

BASE_URL = "https://api.kraken.com"


def normalize_trades(
    payload: dict, *, received_at: datetime, latency_ms: float, dataset: Dataset = "live"
) -> Observation:
    if payload.get("error") != []:
        raise ValueError("Kraken returned a provider error")
    result = payload.get("result")
    if not isinstance(result, dict) or set(result) != {"XETHZUSD", "last"}:
        raise ValueError("expected Kraken ETH/USD trades with an explicit USD market")
    trades = result["XETHZUSD"]
    if not isinstance(trades, list) or not trades:
        raise ValueError("Kraken returned no trades")
    for trade in trades:
        if not isinstance(trade, list) or len(trade) != 7:
            raise ValueError("invalid Kraken trade shape")
        if type(trade[6]) is not int or trade[6] < 0:
            raise ValueError("invalid Kraken trade ID")
    trade = max(trades, key=lambda row: (epoch_seconds(row[2]), row[6]))
    return Observation(
        dataset=dataset,
        source="kraken",
        provider_event_id=str(trade[6]),
        value=trade[0],
        timestamp=epoch_seconds(trade[2]),
        received_at=received_at,
        latency_ms=latency_ms,
        raw_payload=payload,
    )


class KrakenConnector:
    def __init__(self, client: httpx.AsyncClient, max_future_skew_seconds: float = 5):
        self.client = client
        self.max_future_skew_seconds = max_future_skew_seconds

    async def fetch(self) -> Observation:
        started = time.perf_counter()
        response = await self.client.get(
            f"{BASE_URL}/0/public/Trades", params={"pair": "ETHUSD", "count": 1}
        )
        received_at = datetime.now(UTC)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        # Preserve Kraken's fractional epoch timestamp instead of rounding via float.
        observation = normalize_trades(
            response.json(parse_float=str), received_at=received_at, latency_ms=elapsed_ms
        )
        return check_clock(observation, self.max_future_skew_seconds)
