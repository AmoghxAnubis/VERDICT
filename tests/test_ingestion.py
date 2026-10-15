import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from verdict.config import Settings
from verdict.connectors.coinbase import CoinbaseConnector
from verdict.ingestion import ingest_coinbase


def test_one_shot_is_idempotent(store, observation, monkeypatch):
    live = observation.model_copy(update={"dataset": "live"})
    monkeypatch.setattr(CoinbaseConnector, "fetch", AsyncMock(return_value=live))
    for _ in range(2):
        asyncio.run(ingest_coinbase(store, Settings(_env_file=None), once=True))
    assert store.list() == [live]


def test_failed_one_shot_does_not_fabricate_data(store, monkeypatch):
    monkeypatch.setattr(
        CoinbaseConnector, "fetch", AsyncMock(side_effect=httpx.ConnectError("offline"))
    )
    with pytest.raises(httpx.ConnectError):
        asyncio.run(ingest_coinbase(store, Settings(_env_file=None), once=True))
    assert store.list() == []


def test_continuous_worker_retries_and_recovers(store, observation, monkeypatch):
    live = observation.model_copy(update={"dataset": "live"})
    monkeypatch.setattr(
        CoinbaseConnector,
        "fetch",
        AsyncMock(side_effect=[httpx.ConnectError("offline"), live]),
    )
    delays = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ingest_coinbase(store, Settings(_env_file=None), sleep=sleep))
    assert delays == [2, 2]
    assert store.list() == [live]
