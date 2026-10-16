import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from verdict.config import Settings
from verdict.connectors.registry import CONNECTORS
from verdict.ingestion import ingest_all, retry_delay
from verdict.models import SOURCES


def test_health_counts_duplicates_failures_and_recovery(store, market_observation):
    observation = market_observation(dataset="live")
    store.record_attempt("coinbase", error_type="ConnectError")
    assert store.record_attempt("coinbase", observation=observation)
    assert not store.record_attempt("coinbase", observation=observation)
    health = store.health()["coinbase"]
    assert (health["attempts"], health["received"], health["inserted"], health["errors"]) == (
        3,
        2,
        1,
        1,
    )
    assert health["consecutive_failures"] == 0
    assert health["last_error_type"] is None
    assert health["last_failure"] is not None
    assert health["last_success"].tzinfo is not None


def test_replay_cannot_change_live_health(store, market_observation):
    with pytest.raises(ValueError, match="live observations"):
        store.record_attempt("coinbase", observation=market_observation())
    assert store.health() == {}


def test_concurrent_providers_preserve_success_when_one_fails(
    store, market_observation, monkeypatch
):
    for source in SOURCES:
        fetch = (
            AsyncMock(side_effect=httpx.ConnectError("offline"))
            if source == "kraken"
            else (AsyncMock(return_value=market_observation(source, dataset="live")))
        )
        monkeypatch.setattr(CONNECTORS[source], "fetch", fetch)
    results = asyncio.run(ingest_all(store, Settings(_env_file=None), once=True))
    assert [result["status"] for result in results] == ["ok", "error", "ok"]
    assert {row.source for row in store.list()} == {"coinbase", "binance_us"}
    assert store.health()["kraken"]["consecutive_failures"] == 1


def test_retry_after_is_respected_and_bounded():
    response = httpx.Response(
        429, headers={"Retry-After": "120"}, request=httpx.Request("GET", "https://example.com")
    )
    error = httpx.HTTPStatusError("limited", request=response.request, response=response)
    assert retry_delay(error, 1, 2) == 120
    response.headers["Retry-After"] = "99999"
    assert retry_delay(error, 1, 2) == 300
    response.headers["Retry-After"] = "invalid"
    assert retry_delay(error, 1, 2) == 2
