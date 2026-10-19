"""Fixed-grid diagnostic quality scores; no voting weights or truth probabilities."""

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from statistics import median
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import AwareDatetime, Field, TypeAdapter, field_validator, model_validator

from verdict.dependencies import DependencyRegistry, FrozenModel, Sha256, checksum
from verdict.models import SOURCES, Asset, Dataset, Observation, Source

if TYPE_CHECKING:
    from verdict.storage import ObservationStore

Bps = Annotated[int, Field(ge=0, le=10000, strict=True)]
Count = Annotated[int, Field(ge=0, strict=True)]
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class SourceTiming(FrozenModel):
    source: Source
    freshness_seconds: int = Field(default=30, ge=1, le=3600, strict=True)
    max_future_skew_seconds: int = Field(default=5, ge=0, le=60, strict=True)


class ScoringPolicy(FrozenModel):
    version: Literal["scoring-v1"] = "scoring-v1"
    window_seconds: int = Field(default=600, ge=10, le=86400, strict=True)
    step_seconds: int = Field(default=10, ge=1, le=300, strict=True)
    minimum_peers: int = Field(default=3, ge=3, le=5, strict=True)
    mad_multiplier: int = Field(default=6, ge=1, le=20, strict=True)
    minimum_deviation_bps: int = Field(default=100, ge=1, le=10000, strict=True)
    minimum_fresh_samples: int = Field(default=30, ge=1, le=360, strict=True)
    minimum_comparisons: int = Field(default=30, ge=1, le=360, strict=True)
    minimum_unique_observations: int = Field(default=10, ge=1, le=360, strict=True)
    cold_start_cap_bps: Bps = 2500
    max_observations: int = Field(default=100000, ge=6, le=100000, strict=True)
    source_timing: tuple[SourceTiming, ...] = tuple(SourceTiming(source=s) for s in sorted(SOURCES))

    @field_validator("source_timing")
    @classmethod
    def timing_order(cls, value):
        if len(value) != len(SOURCES) or {item.source for item in value} != set(SOURCES):
            raise ValueError("timing policy must cover every provider exactly once")
        return tuple(sorted(value, key=lambda item: item.source))

    @model_validator(mode="after")
    def grid_limits(self):
        count, remainder = divmod(self.window_seconds, self.step_seconds)
        if remainder or not 1 <= count <= 360:
            raise ValueError("window must contain 1..360 whole sampling steps")
        if (
            max(
                self.minimum_fresh_samples,
                self.minimum_comparisons,
                self.minimum_unique_observations,
            )
            > count
        ):
            raise ValueError("cold-start requirements exceed the sampling window")
        return self


class ScoreSample(FrozenModel):
    source: Source
    observation_id: Sha256 | None = None
    value: Decimal | None = None
    age_seconds: Decimal | None = None
    availability: Literal["fresh", "stale", "clock_skew", "missing"]
    freshness_bps: Bps = 0
    comparison: Literal["inlier", "outlier", "unscored"] = "unscored"
    comparison_reason: Literal["source_unavailable", "insufficient_peers", "peer_comparison"]
    peer_observation_ids: tuple[Sha256, ...] = ()
    peer_median: Decimal | None = None
    deviation_threshold: Decimal | None = None


class ScoringTick(FrozenModel):
    cutoff: AwareDatetime
    sources: tuple[ScoreSample, ...]


class SourceScore(FrozenModel):
    source: Source
    scheduled_samples: Count
    fresh_samples: Count
    missing_samples: Count
    stale_samples: Count
    clock_skew_samples: Count
    comparisons: Count
    inlier_samples: Count
    outlier_samples: Count
    unique_fresh_observations: Count
    uptime_bps: Bps
    mean_freshness_bps: Bps
    current_freshness_bps: Bps
    consistency_bps: Bps | None
    comparison_coverage_bps: Bps
    uncapped_score_bps: Bps | None
    score_bps: Bps | None
    status: Literal["unavailable", "unscored", "cold_start", "established"]
    cold_start: bool
    reasons: tuple[str, ...]
    unknown_dependencies: tuple[str, ...]


class ScoringData(FrozenModel):
    schema_version: Literal[1] = 1
    dataset: Dataset
    asset: Asset = "ETH-USD"
    cutoff: AwareDatetime
    window_start_exclusive: AwareDatetime
    policy: ScoringPolicy
    policy_sha256: Sha256
    registry_id: Sha256
    registry_reviewed_at: AwareDatetime
    registry_review_after_cutoff: bool
    input_observation_ids: tuple[Sha256, ...] = Field(max_length=2160)
    input_sha256: Sha256
    ticks: tuple[ScoringTick, ...]
    sources: tuple[SourceScore, ...]
    independent_domain_count: None = None
    confidence: None = None
    publication_ready: Literal[False] = False


class ScoringReport(ScoringData):
    report_id: Sha256

    @model_validator(mode="after")
    def validate_hashes(self):
        if self.policy_sha256 != checksum(self.policy.model_dump(mode="json")):
            raise ValueError("scoring policy checksum mismatch")
        if self.report_id != checksum(self.model_dump(mode="json", exclude={"report_id"})):
            raise ValueError("scoring report checksum mismatch")
        return self


def aligned_cutoff(cutoff: datetime, step_seconds: int) -> datetime:
    if cutoff.tzinfo is None:
        raise ValueError("scoring cutoff must be timezone-aware")
    cutoff = cutoff.astimezone(UTC)
    elapsed = (cutoff - EPOCH) // timedelta(seconds=step_seconds)
    return EPOCH + timedelta(seconds=elapsed * step_seconds)


def event_order(row: Observation) -> tuple:
    return row.timestamp, row.event_sequence, row.observation_id


def availability(row: Observation | None, cutoff: datetime, timing: SourceTiming) -> dict:
    if row is None:
        return {"availability": "missing"}
    age_delta = cutoff - row.timestamp
    age = Decimal(age_delta.days * 86400 + age_delta.seconds) + (
        Decimal(age_delta.microseconds) / 1000000
    )
    state = "fresh"
    if row.timestamp - row.received_at > timedelta(seconds=timing.max_future_skew_seconds):
        state = "clock_skew"
    elif age > timing.freshness_seconds:
        state = "stale"
    freshness = (
        int(Decimal(10000) * (-age / timing.freshness_seconds).exp()) if state == "fresh" else 0
    )
    return {
        "observation_id": row.observation_id,
        "value": row.value,
        "age_seconds": age,
        "availability": state,
        "freshness_bps": freshness,
    }


def summarize(
    source: Source, ticks: list[ScoringTick], policy: ScoringPolicy, unknowns: tuple[str, ...]
) -> SourceScore:
    samples = [next(item for item in tick.sources if item.source == source) for tick in ticks]
    fresh = [item for item in samples if item.availability == "fresh"]
    compared = [item for item in samples if item.comparison != "unscored"]
    inliers = sum(item.comparison == "inlier" for item in compared)
    unique = len({item.observation_id for item in fresh})
    uptime = len(fresh) * 10000 // len(samples)
    mean_freshness = sum(item.freshness_bps for item in samples) // len(samples)
    consistency = inliers * 10000 // len(compared) if compared else None
    comparison_coverage = len(compared) * 10000 // len(samples)
    reasons = []
    if len(fresh) < policy.minimum_fresh_samples:
        reasons.append("insufficient_fresh_history")
    if len(compared) < policy.minimum_comparisons:
        reasons.append("insufficient_comparison_history")
    if unique < policy.minimum_unique_observations:
        reasons.append("insufficient_unique_observations")
    cold = bool(reasons)
    current = samples[-1]
    raw_score = score = None
    if current.availability != "fresh":
        status = "unavailable"
        raw_score = score = 0
        reasons.append(f"current_{current.availability}")
    elif current.comparison == "unscored":
        status = "unscored"
        reasons.append("current_insufficient_peers")
    else:
        # Current outliers cannot borrow a healthy score from preceding agreement.
        raw_score = min(
            uptime, mean_freshness, consistency, comparison_coverage, current.freshness_bps
        )
        if current.comparison == "outlier":
            raw_score = 0
            reasons.append("current_outlier")
        score = min(raw_score, policy.cold_start_cap_bps) if cold else raw_score
        status = "cold_start" if cold else "established"
    if unknowns:
        reasons.append("upstream_dependencies_unresolved")
    return SourceScore(
        source=source,
        scheduled_samples=len(samples),
        fresh_samples=len(fresh),
        missing_samples=sum(item.availability == "missing" for item in samples),
        stale_samples=sum(item.availability == "stale" for item in samples),
        clock_skew_samples=sum(item.availability == "clock_skew" for item in samples),
        comparisons=len(compared),
        inlier_samples=inliers,
        outlier_samples=len(compared) - inliers,
        unique_fresh_observations=unique,
        uptime_bps=uptime,
        mean_freshness_bps=mean_freshness,
        current_freshness_bps=current.freshness_bps,
        consistency_bps=consistency,
        comparison_coverage_bps=comparison_coverage,
        uncapped_score_bps=raw_score,
        score_bps=score,
        status=status,
        cold_start=cold,
        reasons=tuple(reasons),
        unknown_dependencies=unknowns,
    )


def score_observations(
    observations: list[Observation],
    *,
    dataset: str,
    cutoff: datetime,
    registry: DependencyRegistry,
    policy: ScoringPolicy | None = None,
) -> ScoringReport:
    policy = ScoringPolicy.model_validate(policy or ScoringPolicy())
    registry = DependencyRegistry.model_validate(registry)
    dataset = TypeAdapter(Dataset).validate_python(dataset)
    end = aligned_cutoff(cutoff, policy.step_seconds)
    start = end - timedelta(seconds=policy.window_seconds)
    unique = {}
    for row in observations:
        if row.dataset != dataset or row.timestamp > end or row.received_at > end:
            continue
        row = Observation.model_validate(row.model_dump())
        payload = row.model_dump(mode="json")
        if (
            row.observation_id in unique
            and unique[row.observation_id].model_dump(mode="json") != payload
        ):
            raise ValueError("conflicting scoring observation identity")
        unique[row.observation_id] = row
        if len(unique) > policy.max_observations:
            raise ValueError("scoring observation limit exceeded")
    pending = sorted(
        unique.values(), key=lambda row: (max(row.timestamp, row.received_at), event_order(row))
    )
    latest = {}
    selected = {}
    cursor = 0
    ticks = []
    timings = {item.source: item for item in policy.source_timing}
    with localcontext() as context:
        context.prec = 60
        context.rounding = ROUND_HALF_EVEN
        for step in range(1, policy.window_seconds // policy.step_seconds + 1):
            at = start + timedelta(seconds=step * policy.step_seconds)
            while (
                cursor < len(pending)
                and max(pending[cursor].timestamp, pending[cursor].received_at) <= at
            ):
                row = pending[cursor]
                if row.source not in latest or event_order(row) > event_order(latest[row.source]):
                    latest[row.source] = row
                cursor += 1
            states = {
                source: availability(latest.get(source), at, timings[source])
                for source in sorted(SOURCES)
            }
            samples = []
            for source, state in states.items():
                peers = [
                    item
                    for name, item in states.items()
                    if name != source and item["availability"] == "fresh"
                ]
                comparison = {"comparison_reason": "source_unavailable"}
                if state["availability"] == "fresh":
                    comparison = {
                        "comparison_reason": "insufficient_peers",
                        "peer_observation_ids": tuple(item["observation_id"] for item in peers),
                    }
                    if len(peers) >= policy.minimum_peers:
                        reference = median([item["value"] for item in peers])
                        mad = median([abs(item["value"] - reference) for item in peers])
                        threshold = max(
                            mad * policy.mad_multiplier,
                            reference * policy.minimum_deviation_bps / 10000,
                        )
                        comparison = {
                            "comparison_reason": "peer_comparison",
                            "comparison": "outlier"
                            if abs(state["value"] - reference) > threshold
                            else "inlier",
                            "peer_observation_ids": tuple(item["observation_id"] for item in peers),
                            "peer_median": reference,
                            "deviation_threshold": threshold,
                        }
                samples.append(ScoreSample(source=source, **state, **comparison))
                if source in latest:
                    selected[latest[source].observation_id] = latest[source]
            ticks.append(ScoringTick(cutoff=at, sources=tuple(samples)))
    unknown_nodes = {node.id for node in registry.nodes if node.kind == "unknown"}
    unknowns = {
        item.source: tuple(sorted(registry.upstreams(item.node_id) & unknown_nodes))
        for item in registry.sources
    }
    data = ScoringData(
        dataset=dataset,
        cutoff=end,
        window_start_exclusive=start,
        policy=policy,
        policy_sha256=checksum(policy.model_dump(mode="json")),
        registry_id=registry.registry_id,
        registry_reviewed_at=registry.reviewed_at,
        registry_review_after_cutoff=registry.reviewed_at > end,
        input_observation_ids=tuple(sorted(selected)),
        input_sha256=checksum(
            {identity: row.model_dump(mode="json") for identity, row in selected.items()}
        ),
        ticks=tuple(ticks),
        sources=tuple(
            summarize(source, ticks, policy, unknowns[source]) for source in sorted(SOURCES)
        ),
    )
    return ScoringReport(**data.model_dump(), report_id=checksum(data.model_dump(mode="json")))


def build_scoring_report(
    store: "ObservationStore",
    *,
    dataset: str,
    cutoff: datetime,
    registry_id: str,
    policy: ScoringPolicy | None = None,
) -> ScoringReport:
    policy = ScoringPolicy.model_validate(policy or ScoringPolicy())
    registry = store.get_dependency_registry(registry_id)
    if registry is None:
        raise ValueError("dependency registry not found; register the selected version first")
    end = aligned_cutoff(cutoff, policy.step_seconds)
    first = end - timedelta(seconds=policy.window_seconds - policy.step_seconds)
    rows = store.scoring_observations(
        dataset=dataset, first=first, cutoff=end, max_observations=policy.max_observations
    )
    return score_observations(rows, dataset=dataset, cutoff=end, registry=registry, policy=policy)
