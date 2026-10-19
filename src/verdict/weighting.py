"""Persistent quarantine transitions and conservative dependency-constrained weights."""

from datetime import timedelta
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from verdict.dependencies import DependencyRegistry, FrozenModel, Sha256, checksum
from verdict.models import SOURCES, Dataset, Source
from verdict.scoring import Bps, Count, ScoreSample, ScoringReport, SourceScore


class WeightingPolicy(FrozenModel):
    version: Literal["weighting-v1"] = "weighting-v1"
    quarantine_below_bps: Bps = 5000
    recovery_at_least_bps: Bps = 7500
    recovery_new_observations: int = Field(default=10, ge=2, le=360, strict=True)
    recovery_seconds: int = Field(default=60, ge=1, le=86400, strict=True)
    increase_per_round_bps: int = Field(default=250, ge=1, le=10000, strict=True)
    source_budget_bps: int = Field(default=2500, ge=1, le=10000, strict=True)
    source_max_share_bps: int = Field(default=4000, ge=1, le=9999, strict=True)
    economic_max_share_bps: int = Field(default=5000, ge=1, le=9999, strict=True)
    unknown_max_share_bps: int = Field(default=2000, ge=0, le=9999, strict=True)

    @model_validator(mode="after")
    def hysteresis(self):
        if self.recovery_at_least_bps <= self.quarantine_below_bps:
            raise ValueError("recovery threshold must exceed quarantine threshold")
        return self


class SourceWeight(FrozenModel):
    source: Source
    state: Literal["probation", "quarantined", "recovering", "active"]
    last_observation_id: Sha256 | None
    good_observations: Count
    recovery_started_at: AwareDatetime | None
    preliminary_weight_bps: Bps
    weight_bps: Bps = 0
    reasons: tuple[str, ...]


class DependencyCap(FrozenModel):
    id: str
    kind: Literal["source", "economic", "unknown"]
    sources: tuple[Source, ...]
    max_share_bps: Bps


class CapResult(DependencyCap):
    weight_bps: Bps
    actual_share_bps: Bps | None


class WeightingData(FrozenModel):
    schema_version: Literal[1] = 1
    dataset: Dataset
    cutoff: AwareDatetime
    scoring_report_id: Sha256
    scoring_policy_sha256: Sha256
    registry_id: Sha256
    previous_report_id: Sha256 | None
    policy: WeightingPolicy
    policy_sha256: Sha256
    sources: tuple[SourceWeight, ...]
    caps: tuple[CapResult, ...]
    total_weight_bps: Bps
    unallocated_budget_bps: Bps
    status: Literal["weighted", "no_allocatable_weight"]
    independent_domain_count: None = None
    confidence: None = None
    publication_ready: Literal[False] = False


class WeightingReport(WeightingData):
    report_id: Sha256

    @model_validator(mode="after")
    def validate_report(self):
        if self.policy_sha256 != checksum(self.policy.model_dump(mode="json")):
            raise ValueError("weighting policy checksum mismatch")
        if self.report_id != checksum(self.model_dump(mode="json", exclude={"report_id"})):
            raise ValueError("weighting report checksum mismatch")
        if tuple(item.source for item in self.sources) != tuple(sorted(SOURCES)):
            raise ValueError("weighting report must cover every source in canonical order")
        total = sum(item.weight_bps for item in self.sources)
        if total != self.total_weight_bps or self.unallocated_budget_bps != 10000 - total:
            raise ValueError("weighting totals do not match")
        for item in self.sources:
            if item.weight_bps > item.preliminary_weight_bps or (
                item.state != "active" and item.preliminary_weight_bps != 0
            ):
                raise ValueError("inactive sources cannot receive weight")
        weights = {item.source: item.weight_bps for item in self.sources}
        for cap in self.caps:
            amount = sum(weights[source] for source in cap.sources)
            if amount != cap.weight_bps or amount * 10000 > total * cap.max_share_bps:
                raise ValueError("dependency share cap exceeded")
        return self


def transition(
    current: SourceScore,
    sample: ScoreSample,
    *,
    previous: SourceWeight | None,
    cutoff,
    gap: bool,
    policy: WeightingPolicy,
) -> SourceWeight:
    state = previous.state if previous else "probation"
    good = previous.good_observations if previous else 0
    started = previous.recovery_started_at if previous else None
    preliminary = 0
    reasons = []
    failure = None
    if gap:
        failure = "missing_weighting_rounds"
    elif sample.availability != "fresh":
        failure = f"current_{sample.availability}"
    elif sample.comparison != "inlier":
        failure = "current_outlier" if sample.comparison == "outlier" else "current_unscored"
    elif current.score_bps is None:
        failure = "quality_unscored"
    elif current.cold_start:
        if state != "probation":
            failure = "history_lost"
    elif current.score_bps < policy.quarantine_below_bps:
        failure = "low_quality"
    if failure:
        state, good, started = "quarantined", 0, None
        reasons.append(failure)
    elif current.cold_start:
        good, started = 0, None
        reasons.append("cold_start")
    elif state == "active":
        desired = current.score_bps * policy.source_budget_bps // 10000
        preliminary = min(desired, previous.preliminary_weight_bps + policy.increase_per_round_bps)
        reasons.append("active_quality_update")
    elif current.score_bps >= policy.recovery_at_least_bps:
        if previous is None or sample.observation_id != previous.last_observation_id:
            good += 1
            started = started or cutoff
            reasons.append("new_good_observation")
        else:
            reasons.append("awaiting_new_observation")
        if state == "quarantined":
            state = "recovering"
        if (
            good >= policy.recovery_new_observations
            and started is not None
            and (cutoff - started >= timedelta(seconds=policy.recovery_seconds))
        ):
            state = "active"
            preliminary = min(
                current.score_bps * policy.source_budget_bps // 10000, policy.increase_per_round_bps
            )
            reasons.append("recovery_complete")
    else:
        good, started = 0, None
        reasons.append("recovery_quality_not_met")
    return SourceWeight(
        source=current.source,
        state=state,
        last_observation_id=sample.observation_id,
        good_observations=good,
        recovery_started_at=started,
        preliminary_weight_bps=preliminary,
        reasons=tuple(reasons),
    )


def dependency_caps(
    registry: DependencyRegistry, policy: WeightingPolicy
) -> tuple[DependencyCap, ...]:
    # Economic paths exclude transport/settlement edges. Unknowns on any path
    # still enter one conservative pool; none are declared independent.
    adjacency = {}
    for edge in registry.edges:
        if edge.relationship in ("price", "conversion", "protocol"):
            adjacency.setdefault(edge.downstream, set()).add(edge.upstream)
    exposure = {}
    unresolved = []
    unknown_nodes = {node.id for node in registry.nodes if node.kind == "unknown"}
    for source in registry.sources:
        pending = list(adjacency.get(source.node_id, ()))
        reached = set()
        while pending:
            node = pending.pop()
            if node not in reached:
                reached.add(node)
                pending.extend(adjacency.get(node, ()))
        exposure[source.source] = reached
        if registry.upstreams(source.node_id) & unknown_nodes:
            unresolved.append(source.source)
    caps = [
        DependencyCap(
            id=f"source:{source}",
            kind="source",
            sources=(source,),
            max_share_bps=policy.source_max_share_bps,
        )
        for source in sorted(SOURCES)
    ]
    for node in registry.nodes:
        affected = tuple(
            sorted(source for source, reached in exposure.items() if node.id in reached)
        )
        if len(affected) > 1:
            caps.append(
                DependencyCap(
                    id=f"economic:{node.id}",
                    kind="economic",
                    sources=affected,
                    max_share_bps=policy.economic_max_share_bps,
                )
            )
    if unresolved:
        caps.append(
            DependencyCap(
                id="unknown:pool",
                kind="unknown",
                sources=tuple(sorted(unresolved)),
                max_share_bps=policy.unknown_max_share_bps,
            )
        )
    return tuple(sorted(caps, key=lambda item: item.id))


def constrain_weights(
    preliminary: dict[Source, int], caps: tuple[DependencyCap, ...]
) -> dict[Source, int]:
    if set(preliminary) != set(SOURCES) or any(
        type(v) is not int or not 0 <= v <= 10000 for v in preliminary.values()
    ):
        raise ValueError("invalid preliminary weights")
    weights = dict(preliminary)
    total = sum(weights.values())
    if total > 10000:
        weights = {source: weight * 10000 // total for source, weight in weights.items()}
    # Every correction strictly decreases integer mass; convergence is bounded
    # by the initial <=10,000 units. Revisit overlaps after each pass. No refill.
    while True:
        changed = False
        for cap in sorted(caps, key=lambda item: item.id):
            amount = sum(weights[source] for source in cap.sources)
            other = sum(weights.values()) - amount
            if cap.max_share_bps == 10000:
                continue
            maximum = other * cap.max_share_bps // (10000 - cap.max_share_bps)
            if amount > maximum:
                for source in cap.sources:
                    weights[source] = weights[source] * maximum // amount
                changed = True
        if not changed:
            return weights


def advance_weights(
    score: ScoringReport,
    registry: DependencyRegistry,
    *,
    policy: WeightingPolicy | None = None,
    previous: WeightingReport | None = None,
) -> WeightingReport:
    score = ScoringReport.model_validate(score)
    registry = DependencyRegistry.model_validate(registry)
    policy = WeightingPolicy.model_validate(policy or WeightingPolicy())
    policy_hash = checksum(policy.model_dump(mode="json"))
    if registry.registry_id != score.registry_id:
        raise ValueError("scoring registry mismatch")
    if not score.ticks or score.ticks[-1].cutoff != score.cutoff:
        raise ValueError("scoring report lacks its final tick")
    if any(
        tuple(item.source for item in collection) != tuple(sorted(SOURCES))
        for collection in (score.sources, score.ticks[-1].sources)
    ):
        raise ValueError("scoring report must contain every source in canonical order")
    if previous is not None:
        previous = WeightingReport.model_validate(previous)
        if (
            previous.dataset,
            previous.registry_id,
            previous.scoring_policy_sha256,
            previous.policy_sha256,
        ) != (score.dataset, score.registry_id, score.policy_sha256, policy_hash):
            raise ValueError("weighting chain dataset, registry, and policies cannot change")
        if score.cutoff <= previous.cutoff:
            raise ValueError("weighting cutoff must advance strictly")
    gap = previous is not None and score.cutoff - previous.cutoff != timedelta(
        seconds=score.policy.step_seconds
    )
    before = {item.source: item for item in previous.sources} if previous else {}
    states = [
        transition(
            current,
            sample,
            previous=before.get(current.source),
            cutoff=score.cutoff,
            gap=gap,
            policy=policy,
        )
        for current, sample in zip(score.sources, score.ticks[-1].sources, strict=True)
    ]
    caps = dependency_caps(registry, policy)
    weights = constrain_weights({item.source: item.preliminary_weight_bps for item in states}, caps)
    total = sum(weights.values())
    results = tuple(
        CapResult(
            **cap.model_dump(),
            weight_bps=sum(weights[source] for source in cap.sources),
            actual_share_bps=sum(weights[source] for source in cap.sources) * 10000 // total
            if total
            else None,
        )
        for cap in caps
    )
    data = WeightingData(
        dataset=score.dataset,
        cutoff=score.cutoff,
        scoring_report_id=score.report_id,
        scoring_policy_sha256=score.policy_sha256,
        registry_id=registry.registry_id,
        previous_report_id=previous.report_id if previous else None,
        policy=policy,
        policy_sha256=policy_hash,
        sources=tuple(
            item.model_copy(
                update={
                    "weight_bps": weights[item.source],
                    "reasons": (*item.reasons, "dependency_or_budget_cap")
                    if weights[item.source] < item.preliminary_weight_bps
                    else item.reasons,
                }
            )
            for item in states
        ),
        caps=results,
        total_weight_bps=total,
        unallocated_budget_bps=10000 - total,
        status="weighted" if total else "no_allocatable_weight",
    )
    return WeightingReport(**data.model_dump(), report_id=checksum(data.model_dump(mode="json")))
