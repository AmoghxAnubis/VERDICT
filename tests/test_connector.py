import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from verdict.connectors.coinbase import CoinbaseConnector, normalize_ticker


def test_public_endpoint_and_exact_normalization(observation):
    def handler(request):
        assert str(request.url) == "https://api.exchange.coinbase.com/products/ETH-USD/ticker"
        return httpx.Response(200, json=observation.raw_payload)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await CoinbaseConnector(client).fetch()
            assert result.value == observation.value
            assert result.dataset == "live"
            assert result.timestamp == observation.timestamp
            assert result.latency_ms >= 0

    asyncio.run(run())


@pytest.mark.parametrize("status", [429, 500, 503])
def test_http_errors_are_not_converted_to_observations(status):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(status))
        ) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await CoinbaseConnector(client).fetch()

    asyncio.run(run())


def test_transport_timeout_is_propagated():
    def timeout(request):
        raise httpx.ReadTimeout("simulated timeout", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            with pytest.raises(httpx.ReadTimeout):
                await CoinbaseConnector(client).fetch()

    asyncio.run(run())


def test_future_timestamp_is_rejected(observation):
    payload = {
        **observation.raw_payload,
        "time": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        ) as client:
            with pytest.raises(ValueError, match="clock skew"):
                await CoinbaseConnector(client).fetch()

    asyncio.run(run())


@pytest.mark.parametrize("changes", [{"price": 3843.01}, {"trade_id": True}, {"price": "NaN"}])
def test_malformed_provider_prices_and_ids_are_rejected(observation, changes):
    with pytest.raises(ValueError):
        normalize_ticker(
            {**observation.raw_payload, **changes},
            received_at=observation.received_at,
            latency_ms=1,
        )
