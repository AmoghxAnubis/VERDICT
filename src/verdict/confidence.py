"""Uncalibrated quality breakdowns and fail-closed availability assessments."""

from datetime import datetime, timedelta
from fractions import Fraction
from typing import TYPE_CHECKING, Literal

from pydantic import AwareDatetime, Field, model_validator

from verdict.aggregation import AggregationReport, weighted_median
from verdict.dependencies import DependencyRegistry, FrozenModel, Identifier, Sha256, checksum
from verdict.models import SOURCES, Asset, Dataset, PositiveDecimal, Source
from verdict.scoring import Bps, Count, ScoringReport
from verdict.weighting import WeightingPolicy, dependency_caps

if TYPE_CHECKING:
    from verdict.storage import ObservationStore


class ConfidencePolicy(FrozenModel):
    version: Literal["confidence-v1"] = "confidence-v1"
    minimum_sources: int = Field(default=3, ge=3, le=6, strict=True)
    minimum_independent_domains: int = Field(default=3, ge=3, le=6, strict=True)
    minimum_total_weight_bps: int = Field(default=5000, ge=1, le=10000, strict=True)
    minimum_quality_bps: int = Field(default=7500, ge=1, le=10000, strict=True)
    maximum_source_share_bps: int = Field(default=4000, ge=1, le=9999, strict=True)
    maximum_economic_share_bps: int = Field(default=5000, ge=1, le=9999, strict=True)
    maximum_unknown_share_bps: int = Field(default=0, ge=0, le=9999, strict=True)
    full_disagreement_bps: int = Field(default=100, ge=1, le=10000, strict=True)
    registry_max_age_seconds: int = Field(default=604800, ge=1, le=31536000, strict=True)


class QualityContribution(FrozenModel):
    source: Source
    weight_bps: Bps
    value: PositiveDecimal | None
    uptime_bps: Bps
    freshness_bps: Bps
    consistency_bps: Bps | None
    comparison_coverage_bps: Bps
    unknown_dependencies: tuple[Identifier, ...]


class QualityComponents(FrozenModel):
    uptime_bps: Bps
    freshness_bps: Bps
    consistency_bps: Bps
    comparison_coverage_bps: Bps
    allocation_bps: Bps
    agreement_bps: Bps
    resolved_dependency_share_bps: Bps


class EconomicExposure(FrozenModel):
    node_id: Identifier
    sources: tuple[Source, ...]
    weight_bps: Bps
    share_bps: Bps | None


GateName = Literal[
    "candidate",
    "eligible_sources",
    "allocated_budget",
    "quality",
    "source_concentration",
    "economic_concentration",
    "unknown_share",
    "registry_not_future",
    "registry_not_expired",
    "independent_domains",
]


class AvailabilityGate(FrozenModel):
    name: GateName
    observed: int | None
    required: int
    comparison: Literal["gte", "lte"]
    passed: bool
    reason: Literal["passed", "unknown", "below_minimum", "above_maximum"]


def ceiling(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def share(amount: int, total: int) -> int | None:
    return ceiling(Fraction(amount * 10000, total)) if total else None


def components(
    contributions: tuple[QualityContribution, ...], policy: ConfidencePolicy
) -> tuple[QualityComponents, int | None]:
    positive = [item for item in contributions if item.weight_bps]
    total = sum(item.weight_bps for item in positive)
    candidate = weighted_median((item.value, item.weight_bps) for item in positive)
    deviation = None
    if candidate is not None:
        deviation = max(
            abs(Fraction(item.value) - Fraction(candidate)) / Fraction(candidate)
            for item in positive
        )
    weighted = {
        name: sum(item.weight_bps * (getattr(item, name) or 0) for item in positive) // total
        if total
        else 0
        for name in ("uptime_bps", "freshness_bps", "consistency_bps", "comparison_coverage_bps")
    }
    unknown = sum(item.weight_bps for item in positive if item.unknown_dependencies)
    return QualityComponents(
        **weighted,
        allocation_bps=total,
        agreement_bps=max(0, 10000 - ceiling(deviation * 100000000 / policy.full_disagreement_bps))
        if deviation is not None
        else 0,
        resolved_dependency_share_bps=10000 - share(unknown, total) if total else 0,
    ), ceiling(deviation * 10000) if deviation is not None else None


def gates(
    *,
    policy: ConfidencePolicy,
    candidate: bool,
    sources: int,
    total: int,
    quality: int,
    source_share: int | None,
    economic_share: int | None,
    unknown_share: int | None,
    registry_age_microseconds: int,
) -> tuple[AvailabilityGate, ...]:
    # Registry v1 has no independence attestation. Complete-looking paths or
    # provider identities cannot supply this missing observation.
    checks = (
        ("candidate", int(candidate), 1, "gte"),
        ("eligible_sources", sources, policy.minimum_sources, "gte"),
        ("allocated_budget", total, policy.minimum_total_weight_bps, "gte"),
        ("quality", quality, policy.minimum_quality_bps, "gte"),
        ("source_concentration", source_share, policy.maximum_source_share_bps, "lte"),
        ("economic_concentration", economic_share, policy.maximum_economic_share_bps, "lte"),
        ("unknown_share", unknown_share, policy.maximum_unknown_share_bps, "lte"),
        ("registry_not_future", registry_age_microseconds, 0, "gte"),
        (
            "registry_not_expired",
            registry_age_microseconds,
            policy.registry_max_age_seconds * 1000000,
            "lte",
        ),
        ("independent_domains", None, policy.minimum_independent_domains, "gte"),
    )
    results = []
    for name, observed, required, comparison in checks:
        passed = observed is not None and (
            observed >= required if comparison == "gte" else observed <= required
        )
        results.append(
            AvailabilityGate(
                name=name,
                observed=observed,
                required=required,
                comparison=comparison,
                passed=passed,
                reason="unknown"
                if observed is None
                else "passed"
                if passed
                else "below_minimum"
                if comparison == "gte"
                else "above_maximum",
            )
        )
    return tuple(results)


class ConfidenceData(FrozenModel):
    schema_version: Literal[1] = 1
    asset: Asset = "ETH-USD"
    dataset: Dataset
    cutoff: AwareDatetime
    policy: ConfidencePolicy
    policy_sha256: Sha256
    aggregation_report_id: Sha256
    scoring_report_id: Sha256
    registry_id: Sha256
    registry_reviewed_at: AwareDatetime
    input_sha256: Sha256
    contributions: tuple[QualityContribution, ...]
    components: QualityComponents
    quality_score_bps: Bps
    dispersion_bps: Count | None
    economic_exposures: tuple[EconomicExposure, ...]
    source_concentration_bps: Bps | None
    economic_concentration_bps: Bps | None
    unknown_share_bps: Bps | None
    eligible_source_count: int = Field(ge=0, le=6, strict=True)
    total_weight_bps: Bps
    candidate_value: PositiveDecimal | None
    gates: tuple[AvailabilityGate, ...]
    reasons: tuple[str, ...]
    # v1 cannot establish independent domains. No policy override bypasses this.
    independent_domain_count: None = None
    status: Literal["unavailable"] = "unavailable"
    value: None = None
    confidence: None = None
    publication_ready: Literal[False] = False


def breakdown(
    contributions: tuple[QualityContribution, ...],
    exposures: tuple[EconomicExposure, ...],
    policy: ConfidencePolicy,
    cutoff: datetime,
    reviewed_at: datetime,
) -> dict:
    if tuple(item.source for item in contributions) != tuple(sorted(SOURCES)):
        raise ValueError("confidence contributions must cover every source in canonical order")
    positive = [item for item in contributions if item.weight_bps]
    if any(item.value is None or item.consistency_bps is None for item in positive):
        raise ValueError("positive confidence weight requires price and consistency evidence")
    total = sum(item.weight_bps for item in positive)
    weights = {item.source: item.weight_bps for item in contributions}
    if tuple(item.node_id for item in exposures) != tuple(
        sorted({item.node_id for item in exposures})
    ):
        raise ValueError("confidence economic exposures must be unique and canonical")
    for exposure in exposures:
        if len(exposure.sources) < 2 or tuple(sorted(set(exposure.sources))) != exposure.sources:
            raise ValueError("confidence economic exposure sources must be unique and canonical")
        amount = sum(weights[source] for source in exposure.sources)
        if exposure.weight_bps != amount or exposure.share_bps != share(amount, total):
            raise ValueError("confidence economic exposure mass mismatch")
    parts, dispersion = components(contributions, policy)
    quality = min(parts.model_dump().values())
    candidate = weighted_median((item.value, item.weight_bps) for item in positive)
    source_share = share(max(weights.values()), total)
    economic_share = max((item.share_bps for item in exposures), default=0) if total else None
    unknown_share = share(
        sum(item.weight_bps for item in positive if item.unknown_dependencies), total
    )
    age = (cutoff - reviewed_at) // timedelta(microseconds=1)
    checks = gates(
        policy=policy,
        candidate=candidate is not None,
        sources=len(positive),
        total=total,
        quality=quality,
        source_share=source_share,
        economic_share=economic_share,
        unknown_share=unknown_share,
        registry_age_microseconds=age,
    )
    return {
        "components": parts,
        "quality_score_bps": quality,
        "dispersion_bps": dispersion,
        "source_concentration_bps": source_share,
        "economic_concentration_bps": economic_share,
        "unknown_share_bps": unknown_share,
        "eligible_source_count": len(positive),
        "total_weight_bps": total,
        "candidate_value": candidate,
        "gates": checks,
        "reasons": tuple(f"{item.name}:{item.reason}" for item in checks if not item.passed),
    }


class ConfidenceReport(ConfidenceData):
    report_id: Sha256

    @model_validator(mode="after")
    def validate_report(self):
        if self.policy_sha256 != checksum(self.policy.model_dump(mode="json")):
            raise ValueError("confidence policy checksum mismatch")
        if self.report_id != checksum(self.model_dump(mode="json", exclude={"report_id"})):
            raise ValueError("confidence report checksum mismatch")
        expected = breakdown(
            self.contributions,
            self.economic_exposures,
            self.policy,
            self.cutoff,
            self.registry_reviewed_at,
        )
        if any(getattr(self, key) != value for key, value in expected.items()):
            raise ValueError("confidence breakdown does not reproduce")
        return self


def assess_confidence(
    aggregation: AggregationReport,
    score: ScoringReport,
    registry: DependencyRegistry,
    *,
    policy: ConfidencePolicy | None = None,
) -> ConfidenceReport:
    aggregation = AggregationReport.model_validate(aggregation)
    score = ScoringReport.model_validate(score)
    registry = DependencyRegistry.model_validate(registry)
    policy = ConfidencePolicy.model_validate(policy or ConfidencePolicy())
    if (
        aggregation.scoring_report_id,
        aggregation.dataset,
        aggregation.cutoff,
        aggregation.registry_id,
        aggregation.input_sha256,
        registry.reviewed_at,
    ) != (
        score.report_id,
        score.dataset,
        score.cutoff,
        registry.registry_id,
        score.input_sha256,
        score.registry_reviewed_at,
    ) or score.registry_id != registry.registry_id:
        raise ValueError("confidence aggregation, score, and registry evidence mismatch")
    if (
        not score.ticks
        or score.ticks[-1].cutoff != score.cutoff
        or tuple(item.source for item in score.sources) != tuple(sorted(SOURCES))
        or tuple(item.sample for item in aggregation.inputs) != score.ticks[-1].sources
    ):
        raise ValueError("confidence selected scoring evidence mismatch")
    unknown_nodes = {node.id for node in registry.nodes if node.kind == "unknown"}
    roots = {item.source: item.node_id for item in registry.sources}
    contributions = []
    for item, quality in zip(aggregation.inputs, score.sources, strict=True):
        if item.weight.weight_bps and (
            quality.cold_start or quality.score_bps is None or quality.score_bps == 0
        ):
            raise ValueError("confidence positive weight requires established quality")
        contributions.append(
            QualityContribution(
                source=quality.source,
                weight_bps=item.weight.weight_bps,
                value=item.sample.value,
                uptime_bps=quality.uptime_bps,
                freshness_bps=item.sample.freshness_bps,
                consistency_bps=quality.consistency_bps,
                comparison_coverage_bps=quality.comparison_coverage_bps,
                unknown_dependencies=tuple(
                    sorted(registry.upstreams(roots[quality.source]) & unknown_nodes)
                ),
            )
        )
    contributions = tuple(contributions)
    total = aggregation.total_weight_bps
    weights = {item.source: item.weight_bps for item in contributions}
    # Only membership is used here; confidence thresholds come from its own policy.
    exposures = tuple(
        EconomicExposure(
            node_id=cap.id.removeprefix("economic:"),
            sources=cap.sources,
            weight_bps=sum(weights[source] for source in cap.sources),
            share_bps=share(sum(weights[source] for source in cap.sources), total),
        )
        for cap in dependency_caps(registry, WeightingPolicy())
        if cap.kind == "economic"
    )
    data = ConfidenceData(
        dataset=aggregation.dataset,
        cutoff=aggregation.cutoff,
        policy=policy,
        policy_sha256=checksum(policy.model_dump(mode="json")),
        aggregation_report_id=aggregation.report_id,
        scoring_report_id=score.report_id,
        registry_id=registry.registry_id,
        registry_reviewed_at=registry.reviewed_at,
        input_sha256=score.input_sha256,
        contributions=contributions,
        economic_exposures=exposures,
        **breakdown(contributions, exposures, policy, aggregation.cutoff, registry.reviewed_at),
    )
    return ConfidenceReport(**data.model_dump(), report_id=checksum(data.model_dump(mode="json")))


def build_confidence_report(
    store: "ObservationStore",
    *,
    aggregation_report_id: str,
    dataset: str = "live",
    policy: ConfidencePolicy | None = None,
) -> ConfidenceReport:
    aggregation = store.get_aggregation_round(aggregation_report_id, dataset=dataset)
    if aggregation is None:
        raise ValueError("aggregation report not found in selected dataset")
    score = store.get_scoring_round(aggregation["scoring_report_id"], dataset=dataset)
    registry = store.get_dependency_registry(aggregation["registry_id"])
    if score is None or registry is None:
        raise ValueError("confidence scoring or registry evidence is missing")
    return assess_confidence(
        AggregationReport.model_validate(aggregation),
        ScoringReport.model_validate(score),
        registry,
        policy=policy,
    )
