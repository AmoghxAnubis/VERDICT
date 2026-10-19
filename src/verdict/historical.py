"""Bounded public exchange-history capture, separate from live ingestion."""

import asyncio
import gzip
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Literal

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from verdict.connectors.coinbase import normalize_ticker
from verdict.connectors.common import epoch_seconds
from verdict.connectors.kraken import normalize_trades
from verdict.event_identity import same_event_content
from verdict.models import EXCHANGE_SOURCES, Observation
from verdict.statistics import AnalysisPolicy, digest

Exchange = Literal["coinbase", "kraken", "binance_us"]
ENDPOINTS = {
    "coinbase": "https://api.exchange.coinbase.com/products/ETH-USD/trades",
    "kraken": "https://api.kraken.com/0/public/Trades",
    "binance_us": "https://api.binance.us/api/v3/aggTrades",
}
logger = logging.getLogger(__name__)


class HistoricalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    split: Literal["calibration", "holdout"]
    regime: str = Field(min_length=1, max_length=100)
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def bounds(self):
        if self.start.microsecond or self.end.microsecond:
            raise ValueError("historical boundaries must use whole seconds")
        if not timedelta(seconds=120) <= self.end - self.start <= timedelta(hours=1):
            raise ValueError("historical cases require 120..3600 seconds")
        return self


class EvaluationTargets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    minimum_baseline_availability: Decimal = Field(ge=0, le=1)
    maximum_unlabelled_exclusion_fraction: Decimal = Field(ge=0, le=1)
    minimum_spike_detection_fraction: Decimal = Field(ge=0, le=1)


class HistoricalSuite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["historical-v1"]
    description: str
    receipt_delay_ms: int = Field(ge=0, le=60000)
    step_seconds: int = Field(ge=1, le=60)
    policy: AnalysisPolicy
    targets: EvaluationTargets
    cases: tuple[HistoricalCase, ...] = Field(min_length=2, max_length=20)

    @model_validator(mode="after")
    def split_boundaries(self):
        if len({case.name for case in self.cases}) != len(self.cases):
            raise ValueError("historical case names must be unique")
        calibration = [case for case in self.cases if case.split == "calibration"]
        holdouts = [case for case in self.cases if case.split == "holdout"]
        if not calibration or not holdouts:
            raise ValueError("calibration and held-out cases are required")
        # Include full warmup in separation: no event can occur in both splits.
        margin = timedelta(seconds=self.policy.history_seconds + self.policy.freshness_seconds)
        ordered = sorted(self.cases, key=lambda case: case.start)
        if any(left.end > right.start - margin for left, right in pairwise(ordered)):
            raise ValueError("historical cases and warmup histories must not overlap")
        if max(case.end for case in calibration) > min(case.start - margin for case in holdouts):
            raise ValueError("calibration must precede every held-out history")
        if len(set(self.policy.sources)) != len(self.policy.sources):
            raise ValueError("analysis sources must be unique")
        if not set(EXCHANGE_SOURCES) <= set(self.policy.sources):
            raise ValueError("historical policy must include all three exchange sources")
        return self

    @property
    def sha256(self) -> str:
        return digest(self.model_dump(mode="json"))


def load_suite(path: Path) -> HistoricalSuite:
    return HistoricalSuite.model_validate_json(path.read_text(encoding="utf-8"))


class HistoryCaptureError(ValueError):
    pass


class HistoryFetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        source: Exchange,
        *,
        max_requests: int = 100,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.client, self.source = client, source
        self.max_requests, self.sleep = max_requests, sleep
        self.pages: list[dict] = []
        self.requests = 0

    async def get(self, params: dict):
        for retry in range(3):
            if self.requests >= self.max_requests:
                raise HistoryCaptureError("historical request budget exceeded")
            # One request at a time per venue, including cursor discovery and retries.
            await self.sleep(0.4 if self.source == "coinbase" else 1)
            self.requests += 1
            try:
                response = await self.client.get(ENDPOINTS[self.source], params=params)
                response.raise_for_status()
                break
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code not in (
                    429,
                    500,
                    502,
                    503,
                    504,
                ):
                    raise
                if retry == 2:
                    raise
                delay = 2 ** (retry + 1)
                if isinstance(error, httpx.HTTPStatusError):
                    try:
                        delay = max(
                            delay, min(60, float(error.response.headers.get("Retry-After", "0")))
                        )
                    except ValueError:
                        pass
                logger.warning(
                    "history_request_retry source=%s error_type=%s",
                    self.source,
                    type(error).__name__,
                )
                await self.sleep(delay)
        payload = response.json(parse_float=str)
        self.pages.append(
            {
                "params": params,
                "body": response.text,
                "body_sha256": hashlib.sha256(response.content).hexdigest(),
                "cursor_after": response.headers.get("CB-AFTER"),
                "fetched_at": datetime.now(UTC).isoformat(),
            }
        )
        return payload, response.headers

    async def coinbase(self, start: datetime, end: datetime):
        latest, _ = await self.get({"limit": 1})
        if not isinstance(latest, list) or len(latest) != 1:
            raise HistoryCaptureError("invalid Coinbase discovery response")
        newest = normalize_ticker(latest[0], received_at=end, latency_ms=0)
        if newest.timestamp < end:
            raise HistoryCaptureError("requested history is not complete yet")
        # Numeric trade IDs seed a binary search only. The retrieved range is then
        # checked for contiguous IDs and both time boundaries; headers paginate it.
        lower, upper = 1, int(newest.provider_event_id)
        while lower < upper:
            middle = (lower + upper) // 2
            payload, _ = await self.get({"after": str(middle + 1), "limit": 1})
            if not isinstance(payload, list) or len(payload) != 1:
                raise HistoryCaptureError("Coinbase cannot resolve historical trade ID")
            row = normalize_ticker(payload[0], received_at=end, latency_ms=0)
            if int(row.provider_event_id) != middle:
                raise HistoryCaptureError("Coinbase historical ID lookup is not exact")
            if row.timestamp < end:
                lower = middle + 1
            else:
                upper = middle
        cursor = str(lower + 1)
        previous = lower + 1
        while True:
            payload, headers = await self.get({"after": cursor, "limit": 1000})
            if not isinstance(payload, list) or not payload:
                raise HistoryCaptureError("Coinbase history ended before requested start")
            rows = [normalize_ticker(item, received_at=end, latency_ms=0) for item in payload]
            ids = [int(row.provider_event_id) for row in rows]
            if ids != list(range(previous - 1, previous - 1 - len(ids), -1)):
                raise HistoryCaptureError("Coinbase history contains missing or unordered IDs")
            if any(left.timestamp < right.timestamp for left, right in pairwise(rows)):
                raise HistoryCaptureError("Coinbase historical event clock is not monotonic")
            if rows[-1].timestamp <= start:
                return
            next_cursor = headers.get("CB-AFTER")
            if next_cursor != str(ids[-1]) or next_cursor == cursor:
                raise HistoryCaptureError("Coinbase historical cursor stopped advancing")
            cursor, previous = next_cursor, ids[-1]

    async def kraken(self, start: datetime, end: datetime):
        cursor = str(int(start.timestamp()) * 1_000_000_000)
        seen = set()
        while True:
            payload, _ = await self.get({"pair": "ETHUSD", "since": cursor, "count": 1000})
            if not isinstance(payload, dict) or payload.get("error") != []:
                raise HistoryCaptureError("Kraken historical provider error")
            result = payload.get("result", {})
            if set(result) != {"XETHZUSD", "last"} or not result["XETHZUSD"]:
                raise HistoryCaptureError("Kraken history ended before requested boundary")
            rows = result["XETHZUSD"]
            latest = normalize_trades(payload, received_at=end, latency_ms=0)
            if any(
                epoch_seconds(left[2]) > epoch_seconds(right[2]) for left, right in pairwise(rows)
            ):
                raise HistoryCaptureError("Kraken historical events are not ordered")
            if latest.timestamp >= end:
                return
            next_cursor = result["last"]
            if not isinstance(next_cursor, str) or next_cursor in seen or next_cursor == cursor:
                raise HistoryCaptureError("Kraken historical cursor stopped advancing")
            if any(epoch_seconds(row[2]) < start for row in rows):
                raise HistoryCaptureError("Kraken history precedes requested boundary")
            seen.add(cursor)
            cursor = next_cursor

    async def binance_us(self, start: datetime, end: datetime):
        params = {
            "symbol": "ETHUSD",
            "startTime": int(start.timestamp() * 1000),
            "endTime": int(end.timestamp() * 1000) - 1,
            "limit": 1000,
        }
        previous = -1
        while True:
            payload, _ = await self.get(params)
            if not isinstance(payload, list):
                raise HistoryCaptureError("invalid Binance.US aggregate trade response")
            if not payload:
                return
            for item in payload:
                if (
                    not isinstance(item, dict)
                    or type(item.get("a")) is not int
                    or type(item.get("T")) is not int
                    or item["a"] <= previous
                ):
                    raise HistoryCaptureError("invalid or unordered Binance.US aggregate IDs")
                previous = item["a"]
            if payload[-1]["T"] >= int(end.timestamp() * 1000) or len(payload) < 1000:
                return
            # fromId includes its ID; increment it to handle >1000 aggregates in one millisecond.
            params = {"symbol": "ETHUSD", "fromId": previous + 1, "limit": 1000}

    async def capture(self, suite: HistoricalSuite, case: HistoricalCase) -> dict:
        start = case.start - timedelta(
            seconds=suite.policy.history_seconds + suite.policy.freshness_seconds
        )
        await getattr(self, self.source)(start, case.end)
        return {
            "version": 1,
            "suite_sha256": suite.sha256,
            "case": case.name,
            "source": self.source,
            "endpoint": ENDPOINTS[self.source],
            "start": start.isoformat(),
            "end": case.end.isoformat(),
            "pages": self.pages,
        }


def capture_path(directory: Path, case: HistoricalCase, source: Exchange) -> Path:
    return directory / f"{case.name}-{source}.json.gz"


async def fetch_history(suite_path: Path, directory: Path) -> list[dict]:
    suite = load_suite(suite_path)
    if any(case.end >= datetime.now(UTC) for case in suite.cases):
        raise ValueError("historical capture requires completed past windows")
    directory.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "VERDICT/0.1.0"}) as client:

        async def source_history(source):
            results = []
            for case in suite.cases:
                path = capture_path(directory, case, source)
                if path.exists():
                    load_capture(
                        json.loads(gzip.decompress(path.read_bytes())), suite, case, source
                    )
                    results.append({"case": case.name, "source": source, "retained": True})
                    continue
                fetcher = HistoryFetcher(client, source)
                data = await fetcher.capture(suite, case)
                # Validate the retained representation before creating the archive.
                load_capture(data, suite, case, source)
                encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
                with path.open("xb") as output:
                    output.write(gzip.compress(encoded, mtime=0))
                result = {"case": case.name, "source": source, "requests": len(fetcher.pages)}
                print(json.dumps(result), flush=True)
                results.append(result)
            return results

        outcomes = await asyncio.gather(
            *(source_history(source) for source in EXCHANGE_SOURCES), return_exceptions=True
        )
    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    if failures:
        raise HistoryCaptureError(
            "; ".join(f"{type(error).__name__}: {error}" for error in failures)
        )
    return [result for outcome in outcomes for result in outcome]


def load_capture(
    data: dict, suite: HistoricalSuite, case: HistoricalCase, source: Exchange
) -> list[Observation]:
    start = case.start - timedelta(
        seconds=suite.policy.history_seconds + suite.policy.freshness_seconds
    )
    if (
        data.get("version") != 1
        or data.get("suite_sha256") != suite.sha256
        or data.get("case") != case.name
        or data.get("source") != source
        or data.get("endpoint") != ENDPOINTS[source]
        or datetime.fromisoformat(data["start"]) != start
        or datetime.fromisoformat(data["end"]) != case.end
        or not isinstance(data.get("pages"), list)
        or not data["pages"]
    ):
        raise HistoryCaptureError("history capture does not match the frozen suite")
    collected: dict[str, Observation] = {}
    dataset = f"replay:hist-{case.name}"
    delay = timedelta(milliseconds=suite.receipt_delay_ms)
    payloads = []
    for page in data["pages"]:
        body = page["body"]
        if hashlib.sha256(body.encode()).hexdigest() != page["body_sha256"]:
            raise HistoryCaptureError("historical response checksum mismatch")
        payload = json.loads(body, parse_float=str)
        payloads.append(payload)
        if source == "coinbase":
            rows = [
                normalize_ticker(item, dataset=dataset, received_at=case.end, latency_ms=0)
                for item in payload
            ]
        elif source == "kraken":
            rows = [
                normalize_trades(
                    {
                        "error": [],
                        "result": {"XETHZUSD": [item], "last": payload["result"]["last"]},
                    },
                    dataset=dataset,
                    received_at=case.end,
                    latency_ms=0,
                )
                for item in payload["result"]["XETHZUSD"]
            ]
        else:
            rows = [
                Observation(
                    source="binance_us",
                    dataset=dataset,
                    provider_event_id=f"agg:{item['a']}",
                    value=item["p"],
                    timestamp=epoch_seconds(Decimal(item["T"]) / 1000),
                    received_at=case.end,
                    latency_ms=0,
                    raw_payload={"symbol": "ETHUSD", "aggregate_trade": item},
                )
                for item in payload
            ]
        for row in rows:
            if not start <= row.timestamp < case.end:
                continue
            row = row.model_copy(update={"received_at": row.timestamp + delay})
            previous = collected.get(row.observation_id)
            if previous and not same_event_content(previous, row):
                raise HistoryCaptureError("conflicting historical provider event")
            collected.setdefault(row.observation_id, row)
    validate_progress(data["pages"], payloads, source, start, case.end)
    return sorted(collected.values(), key=lambda row: (row.timestamp, row.observation_id))


def validate_progress(pages: list[dict], payloads: list, source: Exchange, start, end) -> None:
    """Recheck range completion when reading archives, not only while capturing them."""
    if source == "coinbase":
        ranges = [
            (page, body)
            for page, body in zip(pages, payloads, strict=True)
            if page["params"].get("limit") == 1000
        ]
        if not ranges or not ranges[0][1] or not ranges[-1][1]:
            raise HistoryCaptureError("Coinbase archive lacks its historical range")
        if datetime.fromisoformat(ranges[0][1][0]["time"]) < end:
            raise HistoryCaptureError("Coinbase archive lacks its ending boundary")
        if datetime.fromisoformat(ranges[-1][1][-1]["time"]) > start:
            raise HistoryCaptureError("Coinbase archive lacks its starting boundary")
        cursor = ranges[0][0]["params"]["after"]
        for page, body in ranges:
            ids = [item["trade_id"] for item in body]
            if not ids or ids != list(range(int(cursor) - 1, int(cursor) - 1 - len(ids), -1)):
                raise HistoryCaptureError("Coinbase archive contains missing or unordered IDs")
            if page["params"]["after"] != cursor or page["cursor_after"] != str(ids[-1]):
                raise HistoryCaptureError("Coinbase archive cursor chain is incomplete")
            times = [datetime.fromisoformat(item["time"]) for item in body]
            if any(left < right for left, right in pairwise(times)):
                raise HistoryCaptureError("Coinbase archive timestamps are not ordered")
            cursor = str(ids[-1])
    elif source == "kraken":
        cursor = str(int(start.timestamp()) * 1_000_000_000)
        previous = start
        for page, body in zip(pages, payloads, strict=True):
            if body.get("error") != [] or set(body.get("result", {})) != {"XETHZUSD", "last"}:
                raise HistoryCaptureError("Kraken archive contains a provider error or wrong pair")
            if page["params"] != {"pair": "ETHUSD", "since": cursor, "count": 1000}:
                raise HistoryCaptureError("Kraken archive cursor chain is incomplete")
            times = [epoch_seconds(item[2]) for item in body["result"]["XETHZUSD"]]
            if (
                not times
                or times[0] < previous
                or any(left > right for left, right in pairwise(times))
            ):
                raise HistoryCaptureError("Kraken archive timestamps are not ordered")
            previous, cursor = times[-1], body["result"]["last"]
        if previous < end:
            raise HistoryCaptureError("Kraken archive lacks its ending boundary")
    else:
        expected = {
            "symbol": "ETHUSD",
            "startTime": int(start.timestamp() * 1000),
            "endTime": int(end.timestamp() * 1000) - 1,
            "limit": 1000,
        }
        previous_id, previous_time = -1, int(start.timestamp() * 1000)
        for index, (page, body) in enumerate(zip(pages, payloads, strict=True)):
            if page["params"] != expected or not isinstance(body, list):
                raise HistoryCaptureError("Binance.US archive cursor chain is incomplete")
            for item in body:
                if (
                    type(item.get("a")) is not int
                    or type(item.get("T")) is not int
                    or item["a"] <= previous_id
                    or item["T"] < previous_time
                ):
                    raise HistoryCaptureError("Binance.US archive identities/times are not ordered")
                previous_id, previous_time = item["a"], item["T"]
            complete = len(body) < 1000 or previous_time >= int(end.timestamp() * 1000)
            if complete != (index == len(pages) - 1):
                raise HistoryCaptureError("Binance.US archive range is incomplete")
            expected = {"symbol": "ETHUSD", "fromId": previous_id + 1, "limit": 1000}
