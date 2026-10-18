"""Coinbase matches/heartbeat ingestion with durable, bounded REST recovery."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

import httpx
from pydantic import AwareDatetime, BaseModel, Field
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from verdict.config import Settings
from verdict.connectors.coinbase import BASE_URL, normalize_ticker
from verdict.connectors.common import check_clock
from verdict.event_identity import same_event_content
from verdict.ingestion import retry_delay
from verdict.models import Observation
from verdict.storage import ObservationStore

logger = logging.getLogger(__name__)


class RecoveryGap(ValueError):
    """REST evidence cannot establish every trade after the retained checkpoint."""


class StreamProtocolError(ValueError):
    pass


class Heartbeat(BaseModel):
    type: Literal["heartbeat"]
    product_id: Literal["ETH-USD"]
    last_trade_id: int = Field(ge=0, le=9223372036854775807, strict=True)
    time: AwareDatetime


async def recover_coinbase(
    client: httpx.AsyncClient,
    store: ObservationStore,
    settings: Settings,
    *,
    required_trade_id: int | None = None,
    required_observation: Observation | None = None,
) -> int:
    """Fetch newest-to-oldest pages; commit only a complete contiguous range."""
    await asyncio.to_thread(store.set_coinbase_stream_status, "recovering")
    state = (await asyncio.to_thread(store.stream_states))["coinbase"]
    checkpoint = state["last_trade_id"]
    after = None
    cursors = set()
    collected: dict[int, Observation] = {}
    target = None
    for _ in range(settings.coinbase_recovery_max_pages):
        params = {"limit": settings.coinbase_recovery_page_size}
        if after is not None:
            params["after"] = after
        started = time.perf_counter()
        response = await client.get(f"{BASE_URL}/products/ETH-USD/trades", params=params)
        received_at = datetime.now(UTC)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        payload = response.json()
        if (
            not isinstance(payload, list)
            or not payload
            or len(payload) > settings.coinbase_recovery_page_size
        ):
            raise RecoveryGap("invalid or empty Coinbase recovery page")
        observations = [
            check_clock(
                normalize_ticker(
                    item,
                    received_at=received_at,
                    latency_ms=elapsed_ms,
                ),
                settings.max_future_skew_seconds,
            )
            for item in payload
        ]
        ids = [int(row.provider_event_id) for row in observations]
        if ids != sorted(set(ids), reverse=True):
            raise RecoveryGap("Coinbase recovery page is not strictly newest-first")
        if target is None:
            target = ids[0]
            if required_trade_id is not None and target < required_trade_id:
                raise RecoveryGap("REST is behind the stream target")
            if checkpoint is None:
                # First launch establishes a baseline; it does not claim older history.
                await asyncio.to_thread(
                    store.record_coinbase_stream_batch, [observations[0]], expected_trade_id=None
                )
                logger.info("coinbase_stream_bootstrap last_trade_id=%s", target)
                return target
            if target < checkpoint:
                raise RecoveryGap("REST is behind the retained checkpoint")
            if target == checkpoint:
                await asyncio.to_thread(
                    store.record_attempt, "coinbase", observation=observations[0]
                )
                return checkpoint
        for trade_id, row in zip(ids, observations, strict=True):
            if checkpoint < trade_id <= target:
                previous = collected.get(trade_id)
                if previous is not None and not same_event_content(previous, row):
                    raise RecoveryGap("conflicting trade in overlapping REST pages")
                collected.setdefault(trade_id, row)
        if ids[-1] <= checkpoint + 1:
            ordered_ids = sorted(collected)
            if (
                len(ordered_ids) != target - checkpoint
                or ordered_ids[0] != checkpoint + 1
                or ordered_ids[-1] != target
            ):
                raise RecoveryGap("REST recovery contains missing trade IDs")
            if required_observation is not None:
                recovered = collected.get(int(required_observation.provider_event_id))
                if recovered is None or not same_event_content(recovered, required_observation):
                    raise RecoveryGap("REST recovery conflicts with the triggering match")
            inserted = await asyncio.to_thread(
                store.record_coinbase_stream_batch,
                [collected[trade_id] for trade_id in ordered_ids],
                expected_trade_id=checkpoint,
            )
            logger.info(
                "coinbase_stream_recovered from_trade_id=%s last_trade_id=%s inserted=%s",
                checkpoint + 1,
                target,
                inserted,
            )
            return target
        next_cursor = response.headers.get("CB-AFTER")
        if not next_cursor or next_cursor in cursors:
            raise RecoveryGap("REST recovery pagination stopped making progress")
        cursors.add(next_cursor)
        after = next_cursor
    raise RecoveryGap("Coinbase recovery page budget exceeded")


def validate_subscription(payload: dict) -> None:
    channels = payload.get("channels")
    if not isinstance(channels, list):
        raise StreamProtocolError("invalid Coinbase subscription response")
    names = {
        channel.get("name")
        for channel in channels
        if isinstance(channel, dict)
        and channel.get("product_ids") == ["ETH-USD"]
        and channel.get("name") in ("matches", "heartbeat")
    }
    if not {"matches", "heartbeat"}.issubset(names):
        raise StreamProtocolError("Coinbase did not acknowledge both required channels")


async def coinbase_stream_session(
    websocket,
    client: httpx.AsyncClient,
    store: ObservationStore,
    settings: Settings,
    *,
    on_progress: Callable[[], None] = lambda: None,
) -> None:
    await websocket.send(
        json.dumps(
            {
                "type": "subscribe",
                "product_ids": ["ETH-USD"],
                "channels": ["matches", "heartbeat"],
            }
        )
    )
    subscribed = False
    checkpoint = None
    loop = asyncio.get_running_loop()
    heartbeat_deadline = loop.time() + settings.coinbase_stream_timeout_seconds
    while True:
        remaining = heartbeat_deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("Coinbase heartbeat deadline exceeded")
        message = await asyncio.wait_for(websocket.recv(), timeout=remaining)
        received_at = datetime.now(UTC)
        payload = json.loads(message)
        if not isinstance(payload, dict):
            raise StreamProtocolError("expected a Coinbase stream object")
        kind = payload.get("type")
        if kind == "error":
            raise StreamProtocolError("Coinbase stream reported an error")
        if kind == "subscriptions":
            validate_subscription(payload)
            if not subscribed:
                checkpoint = await recover_coinbase(client, store, settings)
                await asyncio.to_thread(store.set_coinbase_stream_status, "streaming")
                subscribed = True
                logger.info("coinbase_stream_ready last_trade_id=%s", checkpoint)
                heartbeat_deadline = loop.time() + settings.coinbase_stream_timeout_seconds
            continue
        if not subscribed:
            # REST bootstrap follows the acknowledgement and covers earlier buffered matches.
            continue
        if kind == "heartbeat":
            heartbeat = Heartbeat.model_validate(payload)
            if (heartbeat.time - received_at).total_seconds() > settings.max_future_skew_seconds:
                raise StreamProtocolError("Coinbase heartbeat is in the future")
            if heartbeat.last_trade_id > checkpoint:
                checkpoint = await recover_coinbase(
                    client, store, settings, required_trade_id=heartbeat.last_trade_id
                )
            await asyncio.to_thread(
                store.set_coinbase_stream_status, "streaming", heartbeat_at=received_at
            )
            heartbeat_deadline = loop.time() + settings.coinbase_stream_timeout_seconds
            on_progress()
        elif kind in ("match", "last_match"):
            observation = check_clock(
                normalize_ticker(
                    payload,
                    received_at=received_at,
                    latency_ms=0,
                ),
                settings.max_future_skew_seconds,
            )
            trade_id = int(observation.provider_event_id)
            if trade_id > checkpoint + 1:
                checkpoint = await recover_coinbase(
                    client,
                    store,
                    settings,
                    required_trade_id=trade_id,
                    required_observation=observation,
                )
                await asyncio.to_thread(store.set_coinbase_stream_status, "streaming")
                heartbeat_deadline = loop.time() + settings.coinbase_stream_timeout_seconds
            if trade_id == checkpoint + 1:
                await asyncio.to_thread(
                    store.record_coinbase_stream_batch, [observation], expected_trade_id=checkpoint
                )
                checkpoint = trade_id
                on_progress()
            else:
                # Overlap is checked against retained evidence, preserving the first receipt.
                await asyncio.to_thread(store.record_attempt, "coinbase", observation=observation)


async def ingest_coinbase_stream(
    store: ObservationStore,
    settings: Settings,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    failures = 0

    def progress():
        nonlocal failures
        failures = 0

    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        headers={"User-Agent": "VERDICT/0.1.0"},
    ) as client:
        try:
            while True:
                await asyncio.to_thread(store.set_coinbase_stream_status, "connecting")
                try:
                    async with connect(
                        settings.coinbase_websocket_url,
                        open_timeout=settings.request_timeout_seconds,
                        close_timeout=5,
                        ping_interval=20,
                        ping_timeout=20,
                        max_size=65536,
                        max_queue=1024,
                        user_agent_header="VERDICT/0.1.0",
                    ) as websocket:
                        await coinbase_stream_session(
                            websocket, client, store, settings, on_progress=progress
                        )
                except (
                    httpx.HTTPError,
                    WebSocketException,
                    OSError,
                    TimeoutError,
                    ValueError,
                ) as error:
                    status = (
                        error.response.status_code
                        if isinstance(error, (httpx.HTTPStatusError, InvalidStatus))
                        else None
                    )
                    await asyncio.to_thread(
                        store.record_attempt,
                        "coinbase",
                        error_type=type(error).__name__,
                        http_status=status,
                    )
                    await asyncio.to_thread(store.set_coinbase_stream_status, "disconnected")
                    failures += 1
                    delay = retry_delay(error, failures, settings.poll_interval_seconds)
                    logger.warning(
                        "coinbase_stream_failed error_type=%s http_status=%s "
                        "retry_seconds=%s reason=%s",
                        type(error).__name__,
                        status,
                        delay,
                        str(error)
                        if isinstance(error, (RecoveryGap, StreamProtocolError))
                        else "provider_or_transport_error",
                    )
                    await sleep(delay)
        finally:
            await asyncio.to_thread(store.set_coinbase_stream_status, "disconnected")
