import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from verdict.connectors.binance_us import BinanceUSConnector
from verdict.connectors.binance_us import normalize_trades as normalize_binance
from verdict.connectors.kraken import KrakenConnector
from verdict.connectors.kraken import normalize_trades as normalize_kraken


def test_kraken_trade_identity_and_subsecond_precision():
    payload = {
        "error": [],
        "result": {
            "XETHZUSD": [["3843.010000000000000001", "1", "1792108800.1234567", "b", "m", "", 42]],
            "last": "cursor",
        },
    }
    result = normalize_kraken(payload, received_at=datetime(2026, 10, 16, tzinfo=UTC), latency_ms=1)
    assert result.source == "kraken"
    assert result.provider_event_id == "42"
    assert result.timestamp.microsecond == 123456
    assert result.value == Decimal("3843.010000000000000001")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"error": ["EGeneral:failure"]},
        {"error": [], "result": {"XETHZUSDT": [], "last": "cursor"}},
        {"error": [], "result": {"XETHZUSD": [], "last": "cursor"}},
        {"error": [], "result": {"XETHZUSD": [["1", "1", "1", "b", "m", "", True]], "last": "c"}},
    ],
)
def test_kraken_rejects_error_empty_wrong_market_and_invalid_id(payload):
    with pytest.raises(ValueError):
        normalize_kraken(payload, received_at=datetime.now(UTC), latency_ms=1)


def test_binance_us_uses_milliseconds_and_explicit_usd():
    result = normalize_binance(
        {"symbol": "ETHUSD", "trades": [{"id": 42, "price": "3843.01", "time": 1792108800123}]},
        received_at=datetime.now(UTC),
        latency_ms=1,
    )
    assert result.source == "binance_us"
    assert result.timestamp.microsecond == 123000
    assert result.provider_event_id == "42"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"symbol": "ETHUSDT", "trades": []},
        {"symbol": "ETHUSD", "trades": []},
        {"symbol": "ETHUSD", "trades": [{"id": 1, "price": 3843.1, "time": 1792108800000}]},
        {"symbol": "ETHUSD", "trades": [{"id": True, "price": "1", "time": 1792108800000}]},
    ],
)
def test_binance_us_rejects_wrong_pair_empty_and_malformed_trades(payload):
    with pytest.raises(ValueError):
        normalize_binance(payload, received_at=datetime.now(UTC), latency_ms=1)


@pytest.mark.parametrize("connector", [KrakenConnector, BinanceUSConnector])
def test_new_connectors_request_pinned_markets(connector):
    now = datetime.now(UTC)

    def handler(request):
        if connector is KrakenConnector:
            assert request.url.params["pair"] == "ETHUSD"
            assert request.url.path == "/0/public/Trades"
            payload = {
                "error": [],
                "result": {
                    "XETHZUSD": [["100", "1", str(now.timestamp()), "b", "m", "", 1]],
                    "last": "c",
                },
            }
            return httpx.Response(200, content=json.dumps(payload))
        assert request.url.params["symbol"] == "ETHUSD"
        assert request.url.host == "api.binance.us"
        return httpx.Response(
            200, json=[{"id": 1, "price": "100", "time": int(now.timestamp() * 1000)}]
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert (await connector(client).fetch()).value == 100

    asyncio.run(run())
