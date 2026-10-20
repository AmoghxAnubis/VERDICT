"""Deterministic weighted-median candidates; publication gates remain separate."""

from collections.abc import Iterable
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from pydantic import AwareDatetime, Field, model_validator

from verdict.dependencies import FrozenModel, Sha256, checksum
from verdict.models import SOURCES, Asset, Dataset, PositiveDecimal, Source
from verdict.scoring import Bps, ScoreSample, ScoringReport
from verdict.weighting import SourceWeight, WeightingReport

if TYPE_CHECKING:
    from verdict.storage import ObservationStore


def weighted_median(values: Iterable[tuple[Decimal, int]]) -> Decimal | None:
    """Return the lower median at exact half weight; ignore zero-weight entries."""
    positive = []
    for value, weight in values:
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise ValueError("weighted median prices must be finite positive Decimals")
        if type(weight) is not int or not 0 <= weight <= 10000:
            raise ValueError("weighted median weights must be integer basis points")
        if weight:
            positive.append((value, weight))
    total = sum(weight for _, weight in positive)
    cumulative = 0
    for value, weight in sorted(positive):
        cumulative += weight
        if cumulative * 2 >= total:
            # Decimal.normalize() rounds under the caller's context. Strip lexical
            # trailing fractional zeros without arithmetic for canonical JSON.
            lexical = format(value, "f")
            return Decimal(lexical.rstrip("0").rstrip(".") if "." in lexical else lexical)
    return None


class AggregationPolicy(FrozenModel):
    version: Literal["aggregation-v1"] = "aggregation-v1"
    exact_half_tie: Literal["lower_value"] = "lower_value"


class AggregationInput(FrozenModel):
    sample: ScoreSample
    weight: SourceWeight
    exclusion_reasons: tuple[str, ...]


def exclusions(sample: ScoreSample, weight: SourceWeight) -> tuple[str, ...]:
    reasons = []
    if weight.state != "active":
        reasons.append(f"state_{weight.state}")
    if sample.availability != "fresh":
        reasons.append(f"current_{sample.availability}")
    if sample.comparison != "inlier":
        reasons.append(f"comparison_{sample.comparison}")
    if not weight.weight_bps:
        reasons.append("zero_weight")
    return tuple(reasons)


class AggregationData(FrozenModel):
    schema_version: Literal[1] = 1
    asset: Asset = "ETH-USD"
    dataset: Dataset
    cutoff: AwareDatetime
    policy: AggregationPolicy
    policy_sha256: Sha256
    scoring_report_id: Sha256
    weighting_report_id: Sha256
    scoring_policy_sha256: Sha256
    weighting_policy_sha256: Sha256
    registry_id: Sha256
    input_sha256: Sha256
    inputs: tuple[AggregationInput, ...]
    ordered_sources: tuple[Source, ...]
    eligible_source_count: int = Field(ge=0, le=6, strict=True)
    total_weight_bps: Bps
    unallocated_budget_bps: Bps
    candidate_value: PositiveDecimal | None
    status: Literal["diagnostic_candidate", "unavailable"]
    reasons: tuple[str, ...]
    independent_domain_count: None = None
    confidence: None = None
    publication_ready: Literal[False] = False


class AggregationReport(AggregationData):
    report_id: Sha256

    @model_validator(mode="after")
    def validate_report(self):
        if self.policy_sha256 != checksum(self.policy.model_dump(mode="json")):
            raise ValueError("aggregation policy checksum mismatch")
        if self.report_id != checksum(self.model_dump(mode="json", exclude={"report_id"})):
            raise ValueError("aggregation report checksum mismatch")
        if tuple(item.sample.source for item in self.inputs) != tuple(sorted(SOURCES)):
            raise ValueError("aggregation must cover every source in canonical order")
        for item in self.inputs:
            sample, weight = item.sample, item.weight
            if (
                sample.source != weight.source
                or sample.observation_id != weight.last_observation_id
            ):
                raise ValueError("aggregation source or observation mismatch")
            if item.exclusion_reasons != exclusions(sample, weight):
                raise ValueError("aggregation exclusion breakdown mismatch")
            if weight.weight_bps and (
                item.exclusion_reasons or sample.value is None or sample.observation_id is None
            ):
                # Never drop weighted invalid evidence then normalize the rest:
                # doing so could violate the dependency limits of the pinned round.
                raise ValueError("positive weight requires eligible observation evidence")
        positive = [item for item in self.inputs if item.weight.weight_bps]
        total = sum(item.weight.weight_bps for item in positive)
        if total != self.total_weight_bps or self.unallocated_budget_bps != 10000 - total:
            raise ValueError("aggregation weight totals mismatch")
        ordered = tuple(
            item.sample.source
            for item in sorted(positive, key=lambda item: (item.sample.value, item.sample.source))
        )
        candidate = weighted_median(
            (item.sample.value, item.weight.weight_bps) for item in positive
        )
        if ordered != self.ordered_sources or len(positive) != self.eligible_source_count:
            raise ValueError("aggregation source ordering or count mismatch")
        if self.candidate_value != candidate:
            raise ValueError("aggregation weighted median mismatch")
        if self.status != ("diagnostic_candidate" if total else "unavailable"):
            raise ValueError("aggregation status mismatch")
        expected = ("confidence_and_availability_gates_pending",)
        if not total:
            expected = ("zero_total_weight", *expected)
        if self.reasons != expected:
            raise ValueError("aggregation reason breakdown mismatch")
        return self


def aggregate(score: ScoringReport, weights: WeightingReport) -> AggregationReport:
    score = ScoringReport.model_validate(score)
    weights = WeightingReport.model_validate(weights)
    if (
        weights.scoring_report_id,
        weights.dataset,
        weights.cutoff,
        weights.registry_id,
        weights.scoring_policy_sha256,
    ) != (score.report_id, score.dataset, score.cutoff, score.registry_id, score.policy_sha256):
        raise ValueError("aggregation scoring and weighting evidence mismatch")
    if not score.ticks or score.ticks[-1].cutoff != score.cutoff:
        raise ValueError("aggregation scoring report lacks its final tick")
    if any(
        tuple(item.source for item in collection) != tuple(sorted(SOURCES))
        for collection in (score.sources, score.ticks[-1].sources)
    ):
        raise ValueError("aggregation scoring sources must be complete and canonical")
    inputs = []
    for sample, weight, quality in zip(
        score.ticks[-1].sources, weights.sources, score.sources, strict=True
    ):
        if weight.weight_bps and (
            quality.cold_start or quality.score_bps is None or quality.score_bps == 0
        ):
            raise ValueError("positive weight requires established nonzero quality")
        inputs.append(
            AggregationInput(
                sample=sample, weight=weight, exclusion_reasons=exclusions(sample, weight)
            )
        )
    positive = [item for item in inputs if item.weight.weight_bps]
    # Validate eligibility before sorting or computing a price.
    if any(item.exclusion_reasons or item.sample.value is None for item in positive):
        raise ValueError("positive weight requires eligible observation evidence")
    policy = AggregationPolicy()
    total = weights.total_weight_bps
    data = AggregationData(
        dataset=score.dataset,
        cutoff=score.cutoff,
        policy=policy,
        policy_sha256=checksum(policy.model_dump(mode="json")),
        scoring_report_id=score.report_id,
        weighting_report_id=weights.report_id,
        scoring_policy_sha256=score.policy_sha256,
        weighting_policy_sha256=weights.policy_sha256,
        registry_id=score.registry_id,
        input_sha256=score.input_sha256,
        inputs=tuple(inputs),
        ordered_sources=tuple(
            item.sample.source
            for item in sorted(positive, key=lambda item: (item.sample.value, item.sample.source))
        ),
        eligible_source_count=len(positive),
        total_weight_bps=total,
        unallocated_budget_bps=weights.unallocated_budget_bps,
        candidate_value=weighted_median(
            (item.sample.value, item.weight.weight_bps) for item in positive
        ),
        status="diagnostic_candidate" if total else "unavailable",
        reasons=("confidence_and_availability_gates_pending",)
        if total
        else ("zero_total_weight", "confidence_and_availability_gates_pending"),
    )
    return AggregationReport(**data.model_dump(), report_id=checksum(data.model_dump(mode="json")))


def build_aggregation_report(
    store: "ObservationStore", *, weighting_report_id: str, dataset: str = "live"
) -> AggregationReport:
    weights = store.get_weighting_round(weighting_report_id, dataset=dataset)
    if weights is None:
        raise ValueError("weighting report not found in selected dataset")
    score = store.get_scoring_round(weights["scoring_report_id"], dataset=dataset)
    if score is None:
        raise ValueError("aggregation scoring evidence is missing")
    return aggregate(ScoringReport.model_validate(score), WeightingReport.model_validate(weights))
