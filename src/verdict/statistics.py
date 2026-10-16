"""Deterministic statistical diagnostics; never an oracle publication decision."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from statistics import median
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from verdict.models import SOURCES, Asset, Dataset, Observation, Source
from verdict.storage import ObservationStore

BPS = Decimal(10000)


class AnalysisPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["statistics-v1"] = "statistics-v1"
    sources: tuple[Source, ...] = SOURCES
    history_seconds: int = Field(default=300, ge=10, le=3600)
    freshness_seconds: int = Field(default=30, ge=1, le=300)
    max_future_skew_seconds: int = Field(default=5, ge=0, le=60)
    minimum_sources: int = Field(default=3, ge=3, le=3)
    mad_multiplier: int = Field(default=6, ge=1, le=20)
    minimum_deviation_bps: int = Field(default=100, ge=1, le=10000)
    velocity_signal_bps_per_second: int = Field(default=200, ge=1)
    max_observations: int = Field(default=10000, ge=3, le=100000)


class SourceAnalysis(BaseModel):
    source: Source
    observation_id: str | None = None
    value: Decimal | None = None
    timestamp: AwareDatetime | None = None
    age_seconds: Decimal | None = None
    freshness: Decimal | None = None
    deviation_bps: Decimal | None = None
    twap: Decimal | None = None
    twap_coverage_seconds: Decimal = Decimal(0)
    twap_coverage_fraction: Decimal = Decimal(0)
    velocity_bps_per_second: Decimal | None = None
    rms_return_bps: Decimal | None = None
    exclusion_reasons: list[str] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)


class AnalysisData(BaseModel):
    schema_version: Literal[1] = 1
    dataset: Dataset
    asset: Asset = "ETH-USD"
    cutoff: AwareDatetime
    policy: AnalysisPolicy
    policy_sha256: str
    status: Literal["analyzed", "insufficient_data"]
    median_reference: Decimal | None
    mad: Decimal | None
    outlier_threshold: Decimal | None
    fresh_sources: int
    inlier_sources: int
    sources: list[SourceAnalysis]
    input_observation_ids: list[str]
    publication_ready: Literal[False] = False


class AnalysisReport(AnalysisData):
    round_id: str


def digest(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def seconds(delta: timedelta) -> Decimal:
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1_000_000


def history_metrics(rows: list[Observation], cutoff: datetime, policy: AnalysisPolicy) -> dict:
    start = cutoff - timedelta(seconds=policy.history_seconds)
    unique = {row.timestamp: row for row in rows}
    rows = sorted(unique.values(), key=lambda row: row.timestamp)
    total, coverage = Decimal(0), Decimal(0)
    for index, row in enumerate(rows):
        next_time = rows[index + 1].timestamp if index + 1 < len(rows) else cutoff
        end = min(next_time, cutoff, row.timestamp + timedelta(seconds=policy.freshness_seconds))
        duration = seconds(end - max(start, row.timestamp))
        if duration > 0:
            total += row.value * duration
            coverage += duration
    returns = []
    velocity = None
    for previous, current in zip(rows, rows[1:]):
        gap = seconds(current.timestamp - previous.timestamp)
        if 0 < gap <= policy.freshness_seconds and current.timestamp >= start:
            change = (current.value / previous.value - 1) * BPS
            returns.append(change)
            if current is rows[-1]:
                velocity = change / gap
    return {
        "twap": total / coverage if coverage else None,
        "twap_coverage_seconds": coverage,
        "twap_coverage_fraction": coverage / policy.history_seconds,
        "velocity_bps_per_second": velocity,
        "rms_return_bps": (sum(value * value for value in returns) / len(returns)).sqrt()
        if returns else None,
    }


def analyze(
    observations: list[Observation], *, cutoff: datetime, dataset: str,
    policy: AnalysisPolicy | None = None,
) -> AnalysisReport:
    policy = policy or AnalysisPolicy()
    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")
    if len(set(policy.sources)) != len(policy.sources):
        raise ValueError("analysis sources must be unique")
    cutoff = cutoff.astimezone(UTC)
    rows = sorted((row for row in observations if (
        row.dataset == dataset and row.timestamp <= cutoff and row.received_at <= cutoff
        and row.source in policy.sources
    )), key=lambda row: (row.source, row.timestamp, row.observation_id))
    if len(rows) > policy.max_observations:
        raise ValueError("analysis observation limit exceeded")
    with localcontext() as context:
        context.prec = 60
        decisions = []
        fresh = []
        for source in policy.sources:
            history = [row for row in rows if row.source == source]
            latest = history[-1] if history else None
            decision = SourceAnalysis(source=source)
            if latest is None:
                decision.exclusion_reasons.append("missing")
            else:
                age = seconds(cutoff - latest.timestamp)
                decision.observation_id = latest.observation_id
                decision.value = latest.value
                decision.timestamp = latest.timestamp
                decision.age_seconds = age
                decision.freshness = (-age / policy.freshness_seconds).exp()
                if latest.clock_skew_seconds() > policy.max_future_skew_seconds:
                    decision.exclusion_reasons.append("clock_skew")
                if age > policy.freshness_seconds:
                    decision.exclusion_reasons.append("stale")
                metrics = history_metrics([
                    row for row in history
                    if row.clock_skew_seconds() <= policy.max_future_skew_seconds
                ], cutoff, policy)
                for key, value in metrics.items():
                    setattr(decision, key, value)
                velocity = decision.velocity_bps_per_second
                if velocity is not None and abs(velocity) > policy.velocity_signal_bps_per_second:
                    decision.signals.append("high_price_velocity")
                if not decision.exclusion_reasons:
                    fresh.append(decision)
            decisions.append(decision)
        reference = mad = threshold = None
        if len(fresh) >= policy.minimum_sources:
            reference = median([decision.value for decision in fresh])
            mad = median([abs(decision.value - reference) for decision in fresh])
            threshold = max(mad * policy.mad_multiplier,
                            reference * policy.minimum_deviation_bps / BPS)
            for decision in fresh:
                difference = abs(decision.value - reference)
                decision.deviation_bps = difference / reference * BPS
                if difference > threshold:
                    decision.exclusion_reasons.append("extreme_deviation")
        else:
            for decision in fresh:
                decision.exclusion_reasons.append("insufficient_sources")
        inliers = sum(not decision.exclusion_reasons for decision in decisions)
        data = AnalysisData(
            dataset=dataset, cutoff=cutoff, policy=policy,
            policy_sha256=digest(policy.model_dump(mode="json")),
            status="analyzed" if inliers >= policy.minimum_sources else "insufficient_data",
            median_reference=reference, mad=mad, outlier_threshold=threshold,
            fresh_sources=len(fresh), inlier_sources=inliers, sources=decisions,
            input_observation_ids=[row.observation_id for row in rows],
        )
        return AnalysisReport(**data.model_dump(), round_id=digest(data.model_dump(mode="json")))


def build_report(
    store: ObservationStore, *, dataset: str, cutoff: datetime, policy: AnalysisPolicy | None = None
) -> AnalysisReport:
    policy = policy or AnalysisPolicy()
    observations = store.snapshot(
        dataset=dataset, cutoff=cutoff, history_seconds=policy.history_seconds,
        freshness_seconds=policy.freshness_seconds, max_observations=policy.max_observations,
    )
    return analyze(observations, dataset=dataset, cutoff=cutoff, policy=policy)
