"""At-least-once polling delivery with SQL inbox deduplication and stale claims."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import ConnectionError, ResponseError, TimeoutError
from sqlalchemy.exc import SQLAlchemyError

from verdict.config import Settings
from verdict.queue_models import QueuedAttempt
from verdict.storage import ObservationStore

logger = logging.getLogger(__name__)
RETRYABLE = (ConnectionError, TimeoutError)


class QueueDataLoss(RuntimeError):
    """Redis reports pending entries whose payloads have been removed."""


def redis_client(settings: Settings) -> Redis:
    return Redis.from_url(
        settings.redis_url.get_secret_value(),
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=max(5, settings.redis_block_ms / 1000 + 5),
        health_check_interval=30,
    )


async def publish_attempt(
    client: Redis,
    settings: Settings,
    attempt: QueuedAttempt,
    *,
    once: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    # Keep this identity and payload across ambiguous XADD outcomes. Do not refetch.
    payload = attempt.model_dump_json()
    failures = 0
    while True:
        try:
            # No MAXLEN: trimming can destroy pending/unread evidence.
            return await client.xadd(settings.redis_stream, {"payload": payload})
        except RETRYABLE as error:
            if once:
                raise
            failures += 1
            delay = min(60, 2 ** min(failures, 6))
            logger.warning(
                "queue_publish_failed error_type=%s retry_seconds=%s", type(error).__name__, delay
            )
            await sleep(delay)


async def run_queued_ingestion(
    store: ObservationStore | None, settings: Settings, *, source: str = "all", once: bool = False
):
    from verdict.ingestion import ingest_all, ingest_source

    if settings.coinbase_stream_enabled and not once and source in ("all", "coinbase"):
        raise ValueError("Redis transport currently supports polling; disable Coinbase streaming")
    async with redis_client(settings) as client:

        async def publish(attempt: QueuedAttempt):
            return await publish_attempt(client, settings, attempt, once=once)

        if source == "all":
            return await ingest_all(store, settings, once=once, publisher=publish)
        return await ingest_source(source, store, settings, once=once, publisher=publish)


class QueueConsumer:
    def __init__(
        self,
        client: Redis,
        store: ObservationStore,
        settings: Settings,
        consumer: str | None = None,
    ):
        self.client = client
        self.store = store
        self.settings = settings
        self.consumer = consumer or f"worker-{uuid4().hex}"
        self.claim_cursor = "0-0"

    async def ensure_group(self) -> None:
        try:
            # Starting at 0 retains entries produced before the first consumer starts.
            await self.client.xgroup_create(
                self.settings.redis_stream, self.settings.redis_group, id="0-0", mkstream=True
            )
        except ResponseError as error:
            if not str(error).startswith("BUSYGROUP"):
                raise

    async def process(self, message_id: str, fields: dict) -> str:
        try:
            if set(fields) != {"payload"}:
                raise ValueError("expected one versioned attempt payload")
            attempt = QueuedAttempt.from_payload(fields["payload"])
            outcome = await asyncio.to_thread(self.store.consume_attempt, attempt)
        except ValueError as error:
            # Retain poison/conflicting messages for operator inspection. Other entries proceed.
            logger.error(
                "queue_entry_rejected message_id=%s error_type=%s",
                message_id,
                type(error).__name__,
            )
            return "rejected"
        # SQL has committed before acknowledging. A crash/XACK failure is safe to replay.
        await self.client.xack(self.settings.redis_stream, self.settings.redis_group, message_id)
        return outcome

    async def consume_once(self, *, block: bool = False) -> dict[str, int]:
        settings = self.settings
        claimed = await self.client.xautoclaim(
            settings.redis_stream,
            settings.redis_group,
            self.consumer,
            settings.redis_claim_idle_ms,
            start_id=self.claim_cursor,
            count=settings.redis_batch_size,
        )
        self.claim_cursor = claimed[0]
        if len(claimed) != 3:
            raise RuntimeError("Redis 7+ is required for pending payload loss detection")
        if claimed[2]:
            logger.critical("queue_pending_payloads_lost message_ids=%s", claimed[2])
            raise QueueDataLoss(
                "pending Redis payloads were removed; evidence recovery is required"
            )
        counts: dict[str, int] = {}

        async def process(entries):
            for message_id, fields in entries:
                outcome = await self.process(message_id, fields)
                counts[outcome] = counts.get(outcome, 0) + 1

        await process(claimed[1])
        # Always service new entries too, even with a backlog of rejected/pending messages.
        fresh = await self.client.xreadgroup(
            settings.redis_group,
            self.consumer,
            {settings.redis_stream: ">"},
            count=settings.redis_batch_size,
            block=settings.redis_block_ms if block and not claimed[1] else None,
        )
        for _, entries in fresh:
            await process(entries)
        return counts


async def consume_queue(
    store: ObservationStore,
    settings: Settings,
    *,
    consumer: str | None = None,
    once: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, int]:
    async with redis_client(settings) as client:
        worker = QueueConsumer(client, store, settings, consumer)
        failures = 0
        while True:
            try:
                await worker.ensure_group()
                counts = await worker.consume_once(block=not once)
            except (*RETRYABLE, SQLAlchemyError) as error:
                if once:
                    raise
                failures += 1
                delay = min(60, 2 ** min(failures, 6))
                logger.warning(
                    "queue_consumer_failed error_type=%s retry_seconds=%s",
                    type(error).__name__,
                    delay,
                )
                await sleep(delay)
                continue
            if once:
                return counts
            failures = 0
            if counts:
                logger.info("queue_consumed outcomes=%s", counts)
