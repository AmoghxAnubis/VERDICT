import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from verdict.api import create_app
from verdict.config import Settings
from verdict.connectors.coinbase import normalize_ticker
from verdict.ingestion import ingest_all, ingest_source, retry_delay
from verdict.storage import CheckpointConflict, ObservationConflict, ObservationStore, build_engine
from verdict.streaming import (
    RecoveryGap,
    StreamProtocolError,
    coinbase_stream_session,
    ingest_coinbase_stream,
    recover_coinbase,
    validate_subscription,
)

BASE = datetime(2026, 10, 16, tzinfo=UTC)
ACK = {
    "type": "subscriptions",
    "channels": [
        {"name": "matches", "product_ids": ["ETH-USD"]},
        {"name": "heartbeat", "product_ids": ["ETH-USD"]},
    ],
}


def trade(trade_id, price="100", *, stream=False):
    payload = {
        "trade_id": trade_id,
        "price": price,
        "time": (BASE + timedelta(seconds=trade_id)).isoformat(),
    }
    if stream:
        payload.update(type="match", product_id="ETH-USD")
    return payload


def observation(trade_id, price="100"):
    return normalize_ticker(
        trade(trade_id, price), received_at=BASE + timedelta(hours=1), latency_ms=1
    )


def bootstrap(store, trade_id=100):
    store.set_coinbase_stream_status("recovering")
    store.record_coinbase_stream_batch([observation(trade_id)], expected_trade_id=None)


def recovery_client(pages):
    requests = []

    def handler(request):
        assert request.url.host == "api.exchange.coinbase.com"
        assert request.url.path == "/products/ETH-USD/trades"
        requests.append(dict(request.url.params))
        result = pages.pop(0)
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(
            200,
            json=[trade(event) for event in result[0]],
            headers={"CB-AFTER": result[1]} if result[1] else {},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), requests


class Socket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def recv(self):
        if not self.messages:
            raise asyncio.CancelledError
        value = self.messages.pop(0)
        if isinstance(value, BaseException):
            raise value
        return json.dumps(value)


def run_session(store, messages, pages):
    async def run():
        client, requests = recovery_client(pages)
        socket = Socket(messages)
        async with client:
            with pytest.raises(asyncio.CancelledError):
                await coinbase_stream_session(socket, client, store, Settings(_env_file=None))
        assert socket.sent == [
            {"type": "subscribe", "product_ids": ["ETH-USD"], "channels": ["matches", "heartbeat"]}
        ]
        return requests

    return asyncio.run(run())


def test_first_launch_establishes_one_baseline_without_claiming_prior_history(store):
    async def run():
        client, requests = recovery_client([([103, 102, 101], None)])
        async with client:
            assert await recover_coinbase(client, store, Settings(_env_file=None)) == 103
        assert len(requests) == 1

    asyncio.run(run())
    assert [row.provider_event_id for row in store.list()] == ["103"]
    state = store.stream_states()["coinbase"]
    assert state["baseline_trade_id"] == state["last_trade_id"] == 103
    assert state["updated_at"].tzinfo is not None


def test_paginated_recovery_uses_opaque_after_cursor_and_survives_restart(store):
    bootstrap(store)

    async def run():
        client, requests = recovery_client(
            [([104, 103], "opaque-a"), ([103, 102, 101], "opaque-b")]
        )
        async with client:
            assert await recover_coinbase(client, store, Settings(_env_file=None)) == 104
        assert requests[1]["after"] == "opaque-a"

    asyncio.run(run())
    assert {row.provider_event_id for row in store.list()} == {"100", "101", "102", "103", "104"}
    engine = build_engine(str(store.engine.url))
    try:
        reopened = ObservationStore(engine)
        assert reopened.stream_states()["coinbase"]["last_trade_id"] == 104
        assert reopened.stream_states()["coinbase"]["baseline_trade_id"] == 100
    finally:
        engine.dispose()
    assert store.health()["coinbase"]["received"] == 5


@pytest.mark.parametrize(
    "pages,options",
    [
        ([([104, 102, 101], None)], {}),
        ([([104, 103], None)], {}),
        ([([104, 103], "same"), ([104, 103], "same")], {}),
        ([([104, 103], "next")], {"coinbase_recovery_max_pages": 1}),
        ([([104, 103], "next"), ([], None)], {}),
        ([([104, 103], "next"), ([102, 101], None)], {"coinbase_recovery_page_size": 1}),
        ([([104, 104, 101], None)], {}),
        ([([101, 104], None)], {}),
        ([([99, 98], None)], {}),
    ],
)
def test_failed_recovery_keeps_original_evidence_checkpoint_and_counts(store, pages, options):
    bootstrap(store)
    before_health = store.health()

    async def run():
        client, _ = recovery_client(pages)
        async with client:
            with pytest.raises(RecoveryGap):
                await recover_coinbase(client, store, Settings(_env_file=None, **options))

    asyncio.run(run())
    assert store.stream_states()["coinbase"]["last_trade_id"] == 100
    assert store.stream_states()["coinbase"]["status"] == "recovering"
    assert store.list() == [observation(100)]
    assert store.health() == before_health


def test_rest_lagging_heartbeat_target_does_not_advance_checkpoint(store):
    bootstrap(store)

    async def run():
        client, _ = recovery_client([([102, 101], None)])
        async with client:
            with pytest.raises(RecoveryGap, match="behind the stream target"):
                await recover_coinbase(
                    client, store, Settings(_env_file=None), required_trade_id=103
                )

    asyncio.run(run())
    assert store.stream_states()["coinbase"]["last_trade_id"] == 100


def test_conflicting_recovery_rolls_back_checkpoint_batch_and_health(store):
    bootstrap(store)
    store.add(observation(102, "200"))
    health = store.health()
    with pytest.raises(ObservationConflict):
        store.record_coinbase_stream_batch(
            [observation(101), observation(102)], expected_trade_id=100
        )
    assert store.stream_states()["coinbase"]["last_trade_id"] == 100
    assert {row.provider_event_id for row in store.list()} == {"100", "102"}
    assert store.health() == health


def test_stale_worker_cannot_overwrite_checkpoint_or_partially_insert(store):
    bootstrap(store)
    store.record_coinbase_stream_batch([observation(101)], expected_trade_id=100)
    health = store.health()
    with pytest.raises(CheckpointConflict):
        store.record_coinbase_stream_batch(
            [observation(101), observation(102)], expected_trade_id=100
        )
    assert store.stream_states()["coinbase"]["last_trade_id"] == 101
    assert {row.provider_event_id for row in store.list()} == {"100", "101"}
    assert store.health() == health


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([], 100),
        ([observation(102)], 100),
        ([observation(101), observation(103)], 100),
        ([observation(101), observation(101)], 100),
        ([observation(101), observation(102)], None),
        ([observation(101).model_copy(update={"dataset": "replay:test"})], 100),
    ],
)
def test_checkpoint_rejects_noncontiguous_and_replay_input(store, rows, expected):
    bootstrap(store)
    with pytest.raises(ValueError):
        store.record_coinbase_stream_batch(rows, expected_trade_id=expected)
    assert store.stream_states()["coinbase"]["last_trade_id"] == 100


def test_stream_overlap_and_out_of_order_messages_preserve_first_receipt(store):
    bootstrap(store)
    first = store.list()[0]
    messages = [ACK, trade(100, stream=True), trade(101, stream=True), trade(100, stream=True)]
    run_session(store, messages, [([100, 99], None)])
    assert store.stream_states()["coinbase"]["last_trade_id"] == 101
    assert store.list(source="coinbase")[-1] == first
    assert len(store.list()) == 2
    assert store.health()["coinbase"]["inserted"] == 2


@pytest.mark.parametrize("kind", ["match", "last_match", "heartbeat"])
def test_stream_gaps_recover_missing_trades_before_accepting_progress(store, kind):
    bootstrap(store)
    message = trade(103, stream=True)
    message["type"] = kind
    if kind == "heartbeat":
        message = {
            "type": kind,
            "last_trade_id": 103,
            "product_id": "ETH-USD",
            "time": BASE.isoformat(),
        }
    requests = run_session(store, [ACK, message], [([100], None), ([103, 102, 101], None)])
    assert len(requests) == 2
    state = store.stream_states()["coinbase"]
    assert state["last_trade_id"] == 103
    assert state["status"] == "streaming"
    assert {row.provider_event_id for row in store.list()} == {"100", "101", "102", "103"}
    if kind == "heartbeat":
        assert state["last_heartbeat"].tzinfo is not None


@pytest.mark.parametrize(
    "message",
    [
        [],
        {"type": "error", "message": "private provider details"},
        {**trade(101, stream=True), "product_id": "ETH-USDT"},
        {**trade(101, stream=True), "trade_id": True},
        {**trade(101, stream=True), "price": 100.1},
        {
            "type": "heartbeat",
            "last_trade_id": True,
            "product_id": "ETH-USD",
            "time": BASE.isoformat(),
        },
    ],
)
def test_malformed_stream_input_cannot_advance_checkpoint(store, message):
    bootstrap(store)

    async def run():
        client, _ = recovery_client([([100], None)])
        async with client:
            with pytest.raises(ValueError):
                await coinbase_stream_session(
                    Socket([ACK, message]), client, store, Settings(_env_file=None)
                )

    asyncio.run(run())
    assert store.stream_states()["coinbase"]["last_trade_id"] == 100
    assert len(store.list()) == 1


@pytest.mark.parametrize(
    "channels",
    [
        None,
        [],
        [{"name": "matches", "product_ids": ["ETH-USD"]}],
        [{"name": ["matches"], "product_ids": ["ETH-USD"]}],
        [{"name": "matches", "product_ids": ["BTC-USD"]}],
        [None],
    ],
)
def test_both_pinned_subscriptions_must_be_acknowledged(channels):
    with pytest.raises(StreamProtocolError):
        validate_subscription({"channels": channels})


def test_heartbeat_watchdog_is_not_satisfied_by_other_messages(store, monkeypatch):
    bootstrap(store)
    real_wait_for = asyncio.wait_for
    timeouts = []

    async def limited_wait(awaitable, timeout):
        timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout=min(timeout, 0.1))

    class SilentSocket(Socket):
        async def recv(self):
            if self.messages:
                if self.messages[0].get("type") == "unknown":
                    await asyncio.sleep(0.02)
                return await super().recv()
            await asyncio.Event().wait()

    monkeypatch.setattr("verdict.streaming.asyncio.wait_for", limited_wait)

    async def run():
        client, _ = recovery_client([([100], None)])
        async with client:
            with pytest.raises(TimeoutError):
                await coinbase_stream_session(
                    SilentSocket([ACK, {"type": "unknown"}]),
                    client,
                    store,
                    Settings(_env_file=None),
                )

    asyncio.run(run())
    assert timeouts[-1] < timeouts[-2]


def test_worker_reconnects_resubscribes_and_recovers_with_bounded_backoff(store, monkeypatch):
    bootstrap(store)
    sockets = [Socket([ACK, OSError("lost")]), Socket([ACK, trade(103, stream=True)])]
    clients, _ = recovery_client([([100], None), ([102, 101], None)])
    monkeypatch.setattr("verdict.streaming.httpx.AsyncClient", lambda **kwargs: clients)
    opened = []

    @asynccontextmanager
    async def connection(url, **kwargs):
        assert kwargs["max_queue"] == 1024
        assert kwargs["max_size"] == 65536
        opened.append(url)
        yield sockets.pop(0)

    monkeypatch.setattr("verdict.streaming.connect", connection)
    delays = []

    async def sleep(delay):
        delays.append(delay)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ingest_coinbase_stream(store, Settings(_env_file=None), sleep=sleep))
    assert len(opened) == 2
    assert delays == [2]
    assert store.stream_states()["coinbase"]["last_trade_id"] == 103
    assert store.stream_states()["coinbase"]["status"] == "disconnected"
    assert store.health()["coinbase"]["errors"] == 1
    assert store.health()["coinbase"]["consecutive_failures"] == 0


def test_handshake_retry_after_is_respected_and_error_bodies_are_not_stored(store, monkeypatch):
    error = InvalidStatus(
        Response(429, "rate limited", Headers({"Retry-After": "120"}), b"private provider body")
    )
    assert retry_delay(error, 1, 2) == 120

    @asynccontextmanager
    async def connection(*args, **kwargs):
        raise error
        yield

    monkeypatch.setattr("verdict.streaming.connect", connection)

    async def sleep(delay):
        assert delay == 120
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ingest_coinbase_stream(store, Settings(_env_file=None), sleep=sleep))
    health = store.health()["coinbase"]
    assert health["last_http_status"] == 429
    assert health["last_error_type"] == "InvalidStatus"
    assert "private provider body" not in str(health)
    assert store.list() == []


def test_stream_status_is_visible_only_in_live_source_api(store):
    bootstrap(store)
    store.set_coinbase_stream_status("streaming", heartbeat_at=datetime.now(UTC))
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        state = client.get("/v1/sources").json()[0]["stream"]
        assert state["last_trade_id"] == 100
        assert state["status"] == "streaming"
        assert state["last_heartbeat"].endswith("Z")
        historical = client.get("/v1/sources", params={"dataset": "replay:test"}).json()
        assert all(row["stream"] is None for row in historical)


def test_stream_mode_routes_only_continuous_coinbase_worker(store, monkeypatch):
    stream = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr("verdict.streaming.ingest_coinbase_stream", stream)
    settings = Settings(_env_file=None, coinbase_stream_enabled=True)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ingest_source("coinbase", store, settings))
    assert stream.await_count == 1
    from verdict.connectors.coinbase import CoinbaseConnector

    monkeypatch.setattr(CoinbaseConnector, "fetch", AsyncMock(return_value=observation(100)))
    result = asyncio.run(ingest_source("coinbase", store, settings, once=True))
    assert result["status"] == "ok"
    assert stream.await_count == 1


def test_all_mode_keeps_other_workers_independent(store, monkeypatch):
    reached = []

    async def worker(source, *args, **kwargs):
        reached.append(source)
        await asyncio.Event().wait()

    monkeypatch.setattr("verdict.ingestion.ingest_source", worker)

    async def run():
        task = asyncio.create_task(
            ingest_all(store, Settings(_env_file=None, coinbase_stream_enabled=True))
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert set(reached) == {"coinbase", "kraken", "binance_us", "chainlink", "pyth", "uniswap_v3"}
