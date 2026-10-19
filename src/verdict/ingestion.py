import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from websockets.exceptions import InvalidStatus

from verdict.config import Settings
from verdict.connectors.registry import build_connector, poll_interval
from verdict.models import SOURCES, Source
from verdict.queue_models import QueuedAttempt
from verdict.storage import ObservationStore

logger = logging.getLogger(__name__)


def retry_delay(error: Exception, failures: int, minimum: float) -> float:
    delay = max(minimum, min(60, 2 ** min(failures, 6)))
    if isinstance(error, (httpx.HTTPStatusError, InvalidStatus)):
        value = error.response.headers.get("Retry-After")
        if value:
            try:
                requested = float(value)
            except ValueError:
                try:
                    requested = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    requested = 0
            delay = max(delay, min(300, requested))
    return delay


async def ingest_source(
    source: Source,
    store: ObservationStore,
    settings: Settings,
    *,
    once: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    publisher: Callable[[QueuedAttempt], Awaitable[str]] | None = None,
) -> dict:
    if settings.ingestion_transport == "redis" and publisher is None:
        raise ValueError("Redis ingestion requires run_queued_ingestion")
    if source == "coinbase" and settings.coinbase_stream_enabled and not once:
        if publisher is not None:
            raise ValueError(
                "Redis transport currently supports polling; disable Coinbase streaming"
            )
        from verdict.streaming import ingest_coinbase_stream

        await ingest_coinbase_stream(store, settings, sleep=sleep)
        return {}
    failures = 0
    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds, headers={"User-Agent": "VERDICT/0.1.0"}
    ) as client:
        connector = build_connector(source, client, settings)
        interval = poll_interval(source, settings)
        while True:
            try:
                observation = await connector.fetch()
                if publisher is None:
                    inserted = await asyncio.to_thread(
                        store.record_attempt, source, observation=observation
                    )
                else:
                    await publisher(QueuedAttempt(source=source, observation=observation))
                    inserted = None
            except (httpx.HTTPError, ValueError) as error:
                status = (
                    error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
                )
                if publisher is None:
                    await asyncio.to_thread(
                        store.record_attempt,
                        source,
                        error_type=type(error).__name__,
                        http_status=status,
                    )
                else:
                    await publisher(
                        QueuedAttempt(
                            source=source, error_type=type(error).__name__, http_status=status
                        )
                    )
                if once:
                    raise
                failures += 1
                delay = retry_delay(error, failures, interval)
                logger.warning(
                    "provider_fetch_failed source=%s error_type=%s http_status=%s retry_seconds=%s",
                    source,
                    type(error).__name__,
                    status,
                    delay,
                )
                await sleep(delay)
                continue
            failures = 0
            logger.info(
                "observation source=%s event=%s inserted=%s",
                source,
                observation.provider_event_id,
                inserted,
            )
            if once:
                if publisher is not None:
                    return {"source": source, "status": "queued"}
                return {"source": source, "status": "ok", "inserted": inserted}
            await sleep(interval)


async def ingest_coinbase(
    store: ObservationStore,
    settings: Settings,
    *,
    once: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    await ingest_source("coinbase", store, settings, once=once, sleep=sleep)


async def ingest_all(
    store: ObservationStore,
    settings: Settings,
    *,
    once: bool = False,
    publisher: Callable[[QueuedAttempt], Awaitable[str]] | None = None,
) -> list[dict]:
    if not once:
        await asyncio.gather(
            *(ingest_source(source, store, settings, publisher=publisher) for source in SOURCES)
        )
        return []
    results = await asyncio.gather(
        *(
            ingest_source(source, store, settings, once=True, publisher=publisher)
            for source in SOURCES
        ),
        return_exceptions=True,
    )
    return [
        {"source": source, "status": "error", "error_type": type(result).__name__}
        if isinstance(result, Exception)
        else result
        for source, result in zip(SOURCES, results, strict=True)
    ]
