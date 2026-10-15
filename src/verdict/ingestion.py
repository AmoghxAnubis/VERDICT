import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

from verdict.config import Settings
from verdict.connectors.coinbase import CoinbaseConnector
from verdict.storage import ObservationStore

logger = logging.getLogger(__name__)


async def ingest_coinbase(
    store: ObservationStore,
    settings: Settings,
    *,
    once: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    failures = 0
    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds, headers={"User-Agent": "VERDICT/0.1.0"}
    ) as client:
        connector = CoinbaseConnector(client, settings.max_future_skew_seconds)
        while True:
            try:
                observation = await connector.fetch()
                inserted = await asyncio.to_thread(store.add, observation)
            except (httpx.HTTPError, ValueError) as error:
                if once:
                    raise
                failures += 1
                delay = max(
                    settings.poll_interval_seconds, min(60, 2 ** min(failures, 6))
                )
                logger.warning(
                    "coinbase_fetch_failed error_type=%s retry_seconds=%s",
                    type(error).__name__,
                    delay,
                )
                await sleep(delay)
                continue
            failures = 0
            logger.info(
                "observation source=coinbase event=%s inserted=%s",
                observation.provider_event_id,
                inserted,
            )
            if once:
                return
            await sleep(settings.poll_interval_seconds)
