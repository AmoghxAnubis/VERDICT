import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis.aioredis
import httpx
import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError, ResponseError
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from verdict.config import Settings
from verdict.connectors.registry import CONNECTORS
from verdict.ingestion import ingest_all, ingest_source
from verdict.queue import (
    QueueConsumer,
    QueueDataLoss,
    consume_queue,
    publish_attempt,
    run_queued_ingestion,
)
from verdict.queue_models import QueuedAttempt
from verdict.storage import (
    AttemptConflict,
    IngestionReceiptRow,
    ObservationConflict,
    ObservationStore,
    build_engine,
)


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


def attempt(observation):
    return QueuedAttempt(source=observation.source, observation=observation)


def receipts(store):
    with store.session() as session:
        return session.scalar(select(func.count()).select_from(IngestionReceiptRow))


def test_inbox_deduplicates_delivery_but_counts_distinct_poll_attempts(store, market_observation):
    observation = market_observation(dataset="live")
    first = attempt(observation)
    assert store.consume_attempt(first) == "inserted"
    before = store.health()
    assert store.consume_attempt(first) == "duplicate_attempt"
    assert store.health() == before
    assert store.consume_attempt(attempt(observation)) == "duplicate_observation"
    error = QueuedAttempt(source="coinbase", error_type="ConnectError")
    assert store.consume_attempt(error) == "recorded_error"
    assert store.consume_attempt(error) == "duplicate_attempt"
    health = store.health()["coinbase"]
    assert (health["attempts"], health["received"], health["inserted"], health["errors"]) == (
        3,
        2,
        1,
        1,
    )
    assert receipts(store) == 3
    assert store.list() == [observation]


def test_receipt_persists_across_database_connections(store, market_observation):
    queued = attempt(market_observation(dataset="live"))
    store.consume_attempt(queued)
    before = store.health()
    engine = build_engine(str(store.engine.url))
    try:
        assert ObservationStore(engine).consume_attempt(queued) == "duplicate_attempt"
    finally:
        engine.dispose()
    assert store.health() == before


def test_reused_attempt_identity_and_conflicting_observation_roll_back(store, market_observation):
    observation = market_observation(dataset="live")
    queued = attempt(observation)
    store.consume_attempt(queued)
    before = store.health()
    changed = queued.model_copy(
        update={"observation": observation.model_copy(update={"value": Decimal("999")})}
    )
    with pytest.raises(AttemptConflict):
        store.consume_attempt(changed)
    with pytest.raises(ObservationConflict):
        store.consume_attempt(attempt(changed.observation))
    assert receipts(store) == 1
    assert store.health() == before
    assert store.list() == [observation]


def test_database_failure_rolls_back_receipt_and_evidence(store, market_observation, monkeypatch):
    queued = attempt(market_observation(dataset="live"))
    original = store._record_health

    def fail(*args, **kwargs):
        raise OperationalError("database unavailable", {}, Exception())

    monkeypatch.setattr(store, "_record_health", fail)
    with pytest.raises(OperationalError):
        store.consume_attempt(queued)
    assert receipts(store) == 0
    assert store.list() == []
    monkeypatch.setattr(store, "_record_health", original)
    assert store.consume_attempt(queued) == "inserted"


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"version": True},
        {"version": 1.0},
        {"source": "kraken"},
        {"http_status": 200},
        {"error_type": "ValueError"},
        {"attempted_at": "2026-10-16T00:00:00"},
        {"attempt_id": "bad"},
        {"unrecognized": True},
    ],
)
def test_queue_envelope_rejects_invalid_fields(market_observation, changes):
    payload = attempt(market_observation(dataset="live")).model_dump(mode="json")
    payload.update(changes)
    with pytest.raises(ValueError):
        QueuedAttempt.model_validate(payload)


def test_replay_is_rejected_at_producer_and_storage_boundary(store, market_observation):
    with pytest.raises(ValueError, match="live source"):
        attempt(market_observation())
    bypassed = attempt(market_observation(dataset="live")).model_copy(
        update={"observation": market_observation()}
    )
    with pytest.raises(ValueError):
        store.consume_attempt(bypassed)
    assert store.health() == {}
    assert receipts(store) == 0


@pytest.mark.parametrize("missing", ["version", "attempt_id", "attempted_at", "source"])
def test_wire_requires_stable_identity_and_time(market_observation, missing):
    payload = attempt(market_observation(dataset="live")).model_dump(mode="json")
    del payload[missing]
    with pytest.raises(ValueError):
        QueuedAttempt.from_payload(json.dumps(payload))


def test_missing_attempt_identity_is_never_recorded_or_acknowledged(store):
    async def run():
        config = settings()
        async with fakeredis.aioredis.FakeRedis(decode_responses=True) as client:
            worker = QueueConsumer(client, store, config)
            await worker.ensure_group()
            payload = json.dumps({"version": 1, "source": "coinbase", "error_type": "ConnectError"})
            await client.xadd(config.redis_stream, {"payload": payload})
            assert await worker.consume_once() == {"rejected": 1}
            assert (await client.xpending(config.redis_stream, config.redis_group))["pending"] == 1
            assert receipts(store) == 0
            assert store.health() == {}

    asyncio.run(run())


async def leave_pending(client, config, queued, consumer="crashed"):
    message_id = await publish_attempt(client, config, queued, once=True)
    await client.xreadgroup(config.redis_group, consumer, {config.redis_stream: ">"}, count=1)
    # Simulate the configured idle deadline without a wall-clock sleep.
    await client.xclaim(
        config.redis_stream,
        config.redis_group,
        consumer,
        0,
        [message_id],
        idle=config.redis_claim_idle_ms,
    )
    return message_id


def test_consumer_recovers_crash_before_commit_and_after_commit_before_ack(
    store, market_observation
):
    async def run():
        async with fakeredis.aioredis.FakeRedis(decode_responses=True) as client:
            config = settings()
            worker = QueueConsumer(client, store, config, "replacement")
            await worker.ensure_group()
            queued = attempt(market_observation(dataset="live"))
            await leave_pending(client, config, queued)
            assert await worker.consume_once() == {"inserted": 1}
            queued2 = attempt(market_observation(dataset="live", time=1))
            message_id = await publish_attempt(client, config, queued2, once=True)
            await client.xreadgroup(config.redis_group, "crashed", {config.redis_stream: ">"})
            original = client.xack
            client.xack = AsyncMock(side_effect=ConnectionError("lost acknowledgement"))
            with pytest.raises(ConnectionError):
                await worker.process(message_id, {"payload": queued2.model_dump_json()})
            before = store.health()
            assert before["coinbase"]["received"] == 2
            client.xack = original
            await client.xclaim(
                config.redis_stream,
                config.redis_group,
                "crashed",
                0,
                [message_id],
                idle=config.redis_claim_idle_ms,
            )
            assert await worker.consume_once() == {"duplicate_attempt": 1}
            assert store.health() == before
            assert (await client.xpending(config.redis_stream, config.redis_group))["pending"] == 0

    asyncio.run(run())


def test_group_reads_backlog_and_preserves_decimal_receipt_and_provider_evidence(
    store, market_observation
):
    async def run():
        config = settings()
        async with fakeredis.aioredis.FakeRedis(decode_responses=True) as client:
            observation = market_observation(dataset="live", price="1234.123456789123456789")
            queued = attempt(observation)
            await publish_attempt(client, config, queued, once=True)
            worker = QueueConsumer(client, store, config)
            await worker.ensure_group()
            await worker.ensure_group()  # BUSYGROUP does not hide other response errors.
            assert await worker.consume_once() == {"inserted": 1}
            assert store.list() == [observation]
            assert await worker.consume_once() == {}

    asyncio.run(run())


def test_invalid_and_conflicting_entries_stay_pending_while_valid_entries_progress(
    store, market_observation, caplog
):
    async def run():
        config = settings(redis_batch_size=10)
        async with fakeredis.aioredis.FakeRedis(decode_responses=True) as client:
            worker = QueueConsumer(client, store, config)
            await worker.ensure_group()
            await client.xadd(config.redis_stream, {"payload": "private malformed provider body"})
            queued = attempt(market_observation(dataset="live"))
            await publish_attempt(client, config, queued, once=True)
            conflict = attempt(queued.observation.model_copy(update={"value": Decimal("999")}))
            await publish_attempt(client, config, conflict, once=True)
            await publish_attempt(
                client, config, attempt(market_observation("kraken", dataset="live"))
            )
            assert await worker.consume_once() == {"rejected": 2, "inserted": 2}
            assert (await client.xpending(config.redis_stream, config.redis_group))["pending"] == 2
            assert receipts(store) == 2
            assert {row.source for row in store.list()} == {"coinbase", "kraken"}

    asyncio.run(run())
    assert "private malformed provider body" not in caplog.text
    assert "queue_entry_rejected" in caplog.text


def test_trimmed_pending_payloads_fail_loudly(store, market_observation):
    async def run():
        config = settings()
        async with fakeredis.aioredis.FakeRedis(decode_responses=True) as client:
            worker = QueueConsumer(client, store, config)
            await worker.ensure_group()
            message_id = await leave_pending(
                client, config, attempt(market_observation(dataset="live"))
            )
            await client.xdel(config.redis_stream, message_id)
            with pytest.raises(QueueDataLoss):
                await worker.consume_once()
            assert store.list() == []

    asyncio.run(run())


def test_claim_scan_advances_and_always_services_new_entries(store, market_observation):
    async def run():
        config = settings(redis_batch_size=1)
        async with fakeredis.aioredis.FakeRedis(decode_responses=True) as client:
            worker = QueueConsumer(client, store, config)
            await worker.ensure_group()
            for number in range(3):
                await leave_pending(
                    client, config, attempt(market_observation(dataset="live", time=number))
                )
            await publish_attempt(
                client, config, attempt(market_observation("kraken", dataset="live"))
            )
            assert await worker.consume_once() == {"inserted": 2}
            assert worker.claim_cursor != "0-0"
            assert await worker.consume_once() == {"inserted": 1}
            assert await worker.consume_once() == {"inserted": 1}
            assert worker.claim_cursor == "0-0"
            assert (await client.xpending(config.redis_stream, config.redis_group))["pending"] == 0

    asyncio.run(run())


def test_publisher_retries_ambiguous_xadd_with_same_attempt_without_database_write(
    store, market_observation
):
    async def run():
        config = settings()
        async with fakeredis.aioredis.FakeRedis(decode_responses=True) as client:
            original = client.xadd
            calls = []

            async def ambiguous(*args, **kwargs):
                result = await original(*args, **kwargs)
                calls.append(args)
                if len(calls) == 1:
                    raise ConnectionError("reply was lost")
                return result

            client.xadd = ambiguous
            sleep = AsyncMock()
            await publish_attempt(
                client, config, attempt(market_observation(dataset="live")), sleep=sleep
            )
            assert calls[0] == calls[1]
            sleep.assert_awaited_once_with(2)
            assert store.list() == []
            worker = QueueConsumer(client, store, config)
            await worker.ensure_group()
            assert await worker.consume_once() == {"inserted": 1, "duplicate_attempt": 1}
            assert store.health()["coinbase"]["attempts"] == 1

    asyncio.run(run())


def test_failed_database_delivery_is_not_acknowledged(store, market_observation, monkeypatch):
    async def run():
        config = settings()
        async with fakeredis.aioredis.FakeRedis(decode_responses=True) as client:
            worker = QueueConsumer(client, store, config)
            await worker.ensure_group()
            await publish_attempt(client, config, attempt(market_observation(dataset="live")))
            monkeypatch.setattr(
                store,
                "consume_attempt",
                lambda _: (_ for _ in ()).throw(OperationalError("offline", {}, Exception())),
            )
            with pytest.raises(OperationalError):
                await worker.consume_once()
            assert (await client.xpending(config.redis_stream, config.redis_group))["pending"] == 1

    asyncio.run(run())


def test_publisher_once_fails_and_cancellation_does_not_fabricate_health(store):
    async def run():
        client = AsyncMock()
        client.xadd.side_effect = ConnectionError("offline")
        queued = QueuedAttempt(source="coinbase", error_type="ConnectError")
        with pytest.raises(ConnectionError):
            await publish_attempt(client, settings(), queued, once=True)
        sleep = AsyncMock(side_effect=asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError):
            await publish_attempt(client, settings(), queued, sleep=sleep)
        assert store.health() == {}

    asyncio.run(run())


def test_all_six_polling_adapters_queue_success_and_sanitized_failures(store, monkeypatch):
    from verdict.replay import load_observations

    observations = load_observations(Path("simulations/six-provider.json"))
    by_source = {row.source: row.model_copy(update={"dataset": "live"}) for row in observations}
    for source, cls in CONNECTORS.items():
        monkeypatch.setattr(cls, "fetch", AsyncMock(return_value=by_source[source]))
    queued = []

    async def publish(item):
        queued.append(item)
        return "1-0"

    results = asyncio.run(ingest_all(store, settings(), once=True, publisher=publish))
    assert all(result["status"] == "queued" and "inserted" not in result for result in results)
    assert {item.source for item in queued} == set(CONNECTORS)
    assert store.list() == []
    assert store.health() == {}
    response = httpx.Response(429, request=httpx.Request("GET", "https://private-provider"))
    error = httpx.HTTPStatusError(
        "private provider secret", request=response.request, response=response
    )
    monkeypatch.setattr(CONNECTORS["coinbase"], "fetch", AsyncMock(side_effect=error))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(ingest_source("coinbase", store, settings(), once=True, publisher=publish))
    assert queued[-1].error_type == "HTTPStatusError"
    assert queued[-1].http_status == 429
    assert "private" not in queued[-1].model_dump_json()


def test_continuous_collector_retries_publish_without_refetching(
    store, market_observation, monkeypatch
):
    fetch = AsyncMock(return_value=market_observation(dataset="live"))
    monkeypatch.setattr(CONNECTORS["coinbase"], "fetch", fetch)

    async def run():
        client = AsyncMock()
        client.xadd.side_effect = [ConnectionError("offline"), "1-0"]
        sleep = AsyncMock()

        async def publish(item):
            return await publish_attempt(client, settings(), item, sleep=sleep)

        async def stop(_):
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await ingest_source("coinbase", store, settings(), publisher=publish, sleep=stop)
        assert fetch.await_count == 1
        assert client.xadd.call_args_list[0] == client.xadd.call_args_list[1]
        assert store.health() == {}

    asyncio.run(run())


def test_consumer_loop_retries_connection_failures_and_cancels_cleanly(store, monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        xgroup_create = AsyncMock(side_effect=ConnectionError("credential should not be logged"))

    monkeypatch.setattr("verdict.queue.redis_client", lambda _: Client())
    sleep = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(consume_queue(store, settings(), sleep=sleep))
    sleep.assert_awaited_once_with(2)


def test_group_errors_other_than_busygroup_are_not_ignored(store):
    client = AsyncMock()
    client.xgroup_create.side_effect = ResponseError("WRONGTYPE")
    with pytest.raises(ResponseError):
        asyncio.run(QueueConsumer(client, store, settings()).ensure_group())


def test_recent_pending_messages_are_not_stolen(store, market_observation):
    async def run():
        config = settings()
        async with fakeredis.aioredis.FakeRedis(decode_responses=True) as client:
            worker = QueueConsumer(client, store, config)
            await worker.ensure_group()
            await publish_attempt(client, config, attempt(market_observation(dataset="live")))
            await client.xreadgroup(config.redis_group, "active", {config.redis_stream: ">"})
            assert await worker.consume_once() == {}
            assert store.list() == []
            pending = await client.xpending_range(
                config.redis_stream, config.redis_group, "-", "+", 1
            )
            assert pending[0]["consumer"] == "active"

    asyncio.run(run())


def test_concurrent_database_consumers_only_apply_an_attempt_once(store, market_observation):
    async def run():
        queued = attempt(market_observation(dataset="live"))
        outcomes = await asyncio.gather(
            *(asyncio.to_thread(store.consume_attempt, queued) for _ in range(4))
        )
        assert sorted(outcomes) == ["duplicate_attempt"] * 3 + ["inserted"]
        assert receipts(store) == 1
        assert store.health()["coinbase"]["attempts"] == 1

    asyncio.run(run())


def test_recovery_of_error_attempts_does_not_recount_failures(store):
    async def run():
        config = settings()
        async with fakeredis.aioredis.FakeRedis(decode_responses=True) as client:
            worker = QueueConsumer(client, store, config)
            await worker.ensure_group()
            queued = QueuedAttempt(source="coinbase", error_type="HTTPStatusError", http_status=429)
            await leave_pending(client, config, queued)
            store.consume_attempt(queued)
            before = store.health()
            assert await worker.consume_once() == {"duplicate_attempt": 1}
            assert store.health() == before
            assert before["coinbase"]["errors"] == 1

    asyncio.run(run())


def test_queued_ingestion_lifecycle_closes_redis_and_supports_configured_transport(
    market_observation, monkeypatch
):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("verdict.queue.redis_client", lambda _: client)
    monkeypatch.setattr(
        CONNECTORS["coinbase"], "fetch", AsyncMock(return_value=market_observation(dataset="live"))
    )
    close = AsyncMock(wraps=client.aclose)
    monkeypatch.setattr(client, "aclose", close)
    result = asyncio.run(
        run_queued_ingestion(
            None, settings(ingestion_transport="redis"), source="coinbase", once=True
        )
    )
    assert result == {"source": "coinbase", "status": "queued"}
    close.assert_awaited_once()


@pytest.mark.parametrize("source", ["coinbase", "all"])
def test_stream_queue_combination_is_rejected_before_connecting(store, source):
    with pytest.raises(ValueError, match="polling"):
        asyncio.run(
            run_queued_ingestion(store, settings(coinbase_stream_enabled=True), source=source)
        )


def test_cli_queued_producer_does_not_require_database(monkeypatch, capsys):
    from verdict.cli import main

    monkeypatch.setattr("sys.argv", ["verdict", "ingest", "--transport", "redis", "--once"])
    monkeypatch.setattr(
        "verdict.queue.run_queued_ingestion", AsyncMock(return_value=[{"status": "queued"}])
    )
    monkeypatch.setattr(
        "verdict.cli.build_engine", lambda _: pytest.fail("producer opened database")
    )
    main()
    assert json.loads(capsys.readouterr().out) == [{"status": "queued"}]


def test_cli_consumer_rejections_exit_unsuccessfully(store, monkeypatch, capsys):
    from verdict.cli import main

    monkeypatch.setattr("sys.argv", ["verdict", "consume", "--once", "--consumer", "test"])
    monkeypatch.setattr("verdict.queue.consume_queue", AsyncMock(return_value={"rejected": 1}))
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out) == {"rejected": 1}


def test_cli_rejects_stream_and_redis_transport(monkeypatch):
    from verdict.cli import main

    monkeypatch.setattr(
        "sys.argv", ["verdict", "ingest", "--transport", "redis", "--coinbase-stream"]
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_cli_end_to_end_queue_consumption_and_dataset_api_isolation(store, monkeypatch, capsys):
    from fastapi.testclient import TestClient

    from verdict.api import create_app
    from verdict.cli import main
    from verdict.replay import load_observations, replay

    replay(Path("simulations/six-provider.json"), store)
    observations = load_observations(Path("simulations/six-provider.json"))
    by_source = {row.source: row.model_copy(update={"dataset": "live"}) for row in observations}
    for source, cls in CONNECTORS.items():
        monkeypatch.setattr(cls, "fetch", AsyncMock(return_value=by_source[source]))
    server = fakeredis.FakeServer()
    monkeypatch.setattr(
        "verdict.queue.redis_client",
        lambda _: fakeredis.aioredis.FakeRedis(server=server, decode_responses=True),
    )
    monkeypatch.setattr("sys.argv", ["verdict", "ingest", "--transport", "redis", "--once"])
    main()
    assert all(item["status"] == "queued" for item in json.loads(capsys.readouterr().out))
    assert store.list() == []
    monkeypatch.setattr("sys.argv", ["verdict", "consume", "--once"])
    main()
    assert json.loads(capsys.readouterr().out) == {"inserted": 6}
    assert len(store.list()) == 6
    assert len(store.list(dataset="replay:six-provider")) == 6
    with TestClient(create_app(settings(), store)) as client:
        assert len(client.get("/v1/observations").json()) == 6
        assert all(item["dataset"] == "live" for item in client.get("/v1/observations").json())
        response = client.get("/v1/observations", params={"dataset": "replay:six-provider"})
        assert all(item["dataset"] == "replay:six-provider" for item in response.json())
        assert client.get("/v1/feed/ETH-USD").status_code == 503


@pytest.mark.skipif(not os.getenv("VERDICT_TEST_REDIS_URL"), reason="Redis URL not provided")
def test_real_redis_delivery_and_stale_claim_contract(store, market_observation):
    async def run():
        config = settings(
            redis_url=os.environ["VERDICT_TEST_REDIS_URL"],
            redis_stream=f"verdict-test:{uuid4().hex}",
            redis_group=f"group-{uuid4().hex}",
        )
        async with Redis.from_url(
            config.redis_url.get_secret_value(), decode_responses=True
        ) as client:
            try:
                worker = QueueConsumer(client, store, config, "replacement")
                await worker.ensure_group()
                queued = attempt(market_observation(dataset="live"))
                await leave_pending(client, config, queued)
                assert await worker.consume_once() == {"inserted": 1}
                await publish_attempt(client, config, queued, once=True)
                assert await worker.consume_once() == {"duplicate_attempt": 1}
                assert (await client.xpending(config.redis_stream, config.redis_group))[
                    "pending"
                ] == 0
                assert store.health()["coinbase"]["attempts"] == 1
            finally:
                await client.delete(config.redis_stream)

    asyncio.run(run())
