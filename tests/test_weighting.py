import asyncio
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select, update

from verdict.api import create_app
from verdict.cli import main
from verdict.config import Settings
from verdict.dependencies import DependencyRegistry, bundled_registry
from verdict.models import SOURCES
from verdict.replay import load_observations
from verdict.scoring import ScoringPolicy, score_observations
from verdict.storage import ObservationStore, WeightingHeadRow, WeightingRoundRow, build_engine
from verdict.weighting import (
    DependencyCap,
    WeightingPolicy,
    WeightingReport,
    advance_weights,
    constrain_weights,
    dependency_caps,
    transition,
)

BASE = datetime(2026, 10, 16, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def registry():
    # Synthetic completeness assumption, only for exercising nonzero allocations.
    data = bundled_registry().model_dump(mode="json")
    unknown = {node["id"] for node in data["nodes"] if node["kind"] == "unknown"}
    data["nodes"] = [node for node in data["nodes"] if node["id"] not in unknown]
    data["edges"] = [edge for edge in data["edges"] if edge["upstream"] not in unknown]
    data["version"] = "synthetic-complete-graph"
    data["scope"] = "Synthetic TEST assumption of complete dependencies, not provider evidence."
    return DependencyRegistry.model_validate(data)


@pytest.fixture
def scoring():
    templates = {
        row.source: row for row in load_observations(ROOT / "simulations/six-provider.json")
    }

    def make(at, registry, *, price=None, include=None, policy=None, dataset="replay:weights"):
        rows = [
            templates[source].model_copy(
                update={
                    "dataset": dataset,
                    "provider_event_id": f"{source}-{time}",
                    "timestamp": BASE + timedelta(seconds=time),
                    "received_at": BASE + timedelta(seconds=time),
                    "value": Decimal(price(source, time) if price else "100"),
                    "raw_payload": {
                        "fixture": "synthetic-weighting",
                        "time": time,
                        "source": source,
                    },
                }
            )
            for time in range(10, at + 1, 10)
            for source in SOURCES
            if include is None or include(source, time)
        ]
        report = score_observations(
            rows,
            dataset=dataset,
            cutoff=BASE + timedelta(seconds=at),
            registry=registry,
            policy=policy,
        )
        return report, rows

    return make


def weight(report, source="coinbase"):
    return next(item for item in report.sources if item.source == source)


def fast_policy():
    return WeightingPolicy(recovery_new_observations=3, recovery_seconds=20)


def chain(scoring, registry, times, *, policy=None, **kwargs):
    previous = None
    result = []
    for at in times:
        score, _ = scoring(at, registry, **kwargs)
        previous = advance_weights(
            score, registry, previous=previous, policy=policy or fast_policy()
        )
        result.append(previous)
    return result


def test_probation_requires_both_new_evidence_and_elapsed_time_then_ramps(scoring, registry):
    reports = chain(scoring, registry, range(600, 651, 10))
    assert [weight(report).state for report in reports] == ["probation"] * 2 + ["active"] * 4
    assert [weight(report).preliminary_weight_bps for report in reports] == [
        0,
        0,
        250,
        500,
        750,
        1000,
    ]
    assert reports[2].total_weight_bps == 1500
    assert all(not report.publication_ready and report.confidence is None for report in reports)
    slower = chain(
        scoring,
        registry,
        range(600, 641, 10),
        policy=WeightingPolicy(recovery_new_observations=2, recovery_seconds=40),
    )
    assert all(weight(report).state == "probation" for report in slower[:-1])
    assert weight(slower[-1]).state == "active"


def test_default_unknown_registry_cannot_gain_all_weight_by_normalization(scoring):
    registry = bundled_registry()
    reports = chain(scoring, registry, range(600, 701, 10), policy=WeightingPolicy())
    assert weight(reports[-1]).state == "active"
    assert weight(reports[-1]).preliminary_weight_bps > 0
    assert reports[-1].total_weight_bps == 0
    assert reports[-1].unallocated_budget_bps == 10000
    assert reports[-1].status == "no_allocatable_weight"
    unknown = next(cap for cap in reports[-1].caps if cap.kind == "unknown")
    assert set(unknown.sources) == set(SOURCES)
    assert unknown.actual_share_bps is None


def test_attack_removes_weight_immediately_and_recovery_resets_on_failure(scoring, registry):
    reports = chain(
        scoring,
        registry,
        range(600, 731, 10),
        price=lambda source, t: "135" if source == "coinbase" and t in (650, 680) else "100",
    )
    by_time = dict(zip(range(600, 731, 10), reports, strict=True))
    assert weight(by_time[640]).preliminary_weight_bps == 750
    assert weight(by_time[650]).state == "quarantined"
    assert weight(by_time[650]).weight_bps == 0
    assert "current_outlier" in weight(by_time[650]).reasons
    assert weight(by_time[660]).state == "recovering"
    assert weight(by_time[670]).good_observations == 2
    assert weight(by_time[680]).good_observations == 0
    assert weight(by_time[700]).state == "recovering"
    assert weight(by_time[710]).state == "active"
    assert weight(by_time[710]).preliminary_weight_bps == 250


def test_missing_rounds_fail_closed_and_stale_rounds_cannot_roll_back(scoring, registry):
    before = chain(scoring, registry, range(600, 641, 10))[-1]
    score, _ = scoring(660, registry)
    gap = advance_weights(score, registry, previous=before, policy=fast_policy())
    assert all(item.state == "quarantined" and item.weight_bps == 0 for item in gap.sources)
    assert all(item.reasons == ("missing_weighting_rounds",) for item in gap.sources)
    assert gap.total_weight_bps == 0
    for at in (640, 630):
        score, _ = scoring(at, registry)
        with pytest.raises(ValueError, match="advance strictly"):
            advance_weights(score, registry, previous=before, policy=fast_policy())


def test_same_observation_does_not_earn_recovery_credit(scoring, registry):
    timing = ScoringPolicy()
    timing = timing.model_copy(
        update={
            "source_timing": tuple(
                item.model_copy(update={"freshness_seconds": 300}) for item in timing.source_timing
            )
        }
    )
    previous = None
    for at in (600, 610, 620):
        score, _ = scoring(
            at, registry, policy=timing, include=lambda source, t: source != "coinbase" or t <= 600
        )
        previous = advance_weights(score, registry, previous=previous, policy=fast_policy())
    assert weight(previous).good_observations == 1
    assert weight(previous).state == "probation"
    assert weight(previous).weight_bps == 0
    assert "awaiting_new_observation" in weight(previous).reasons
    assert weight(previous, "pyth").state == "active"


def test_hysteresis_and_fast_penalties_do_not_bypass_recovery(scoring, registry):
    before = chain(scoring, registry, range(600, 721, 10))[-1]
    score, _ = scoring(730, registry)
    current = next(item for item in score.sources if item.source == "coinbase")
    sample = next(item for item in score.ticks[-1].sources if item.source == "coinbase")
    result = transition(
        current.model_copy(update={"score_bps": 6000}),
        sample,
        previous=weight(before),
        cutoff=score.cutoff,
        gap=False,
        policy=fast_policy(),
    )
    assert result.state == "active"
    assert result.preliminary_weight_bps == 1500
    failed = transition(
        current.model_copy(update={"score_bps": 4999}),
        sample,
        previous=result,
        cutoff=score.cutoff,
        gap=False,
        policy=fast_policy(),
    )
    assert failed.state == "quarantined"
    assert failed.preliminary_weight_bps == 0
    recovery = transition(
        current.model_copy(update={"score_bps": 6000}),
        sample,
        previous=failed,
        cutoff=score.cutoff,
        gap=False,
        policy=fast_policy(),
    )
    assert recovery.state == "quarantined"
    assert recovery.good_observations == 0
    assert "recovery_quality_not_met" in recovery.reasons


@pytest.mark.parametrize("failure", ["missing", "stale", "clock_skew", "unscored", "cold"])
def test_unavailable_or_lost_history_removes_active_weight(scoring, registry, failure):
    before = chain(scoring, registry, range(600, 631, 10))[-1]
    score, _ = scoring(640, registry)
    current = next(item for item in score.sources if item.source == "coinbase")
    sample = next(item for item in score.ticks[-1].sources if item.source == "coinbase")
    if failure == "cold":
        current = current.model_copy(update={"cold_start": True})
    elif failure == "unscored":
        sample = sample.model_copy(update={"comparison": "unscored"})
    else:
        sample = sample.model_copy(update={"availability": failure})
    result = transition(
        current,
        sample,
        previous=weight(before),
        cutoff=score.cutoff,
        gap=False,
        policy=fast_policy(),
    )
    assert result.state == "quarantined"
    assert result.good_observations == result.preliminary_weight_bps == 0


def test_cold_start_never_receives_provisional_weight(scoring, registry):
    score, _ = scoring(10, registry)
    report = advance_weights(score, registry)
    assert all(
        item.state == "probation" and item.preliminary_weight_bps == 0 for item in report.sources
    )
    assert report.total_weight_bps == 0


def test_economic_paths_include_protocol_conversion_but_not_collector_or_rpc(registry):
    caps = dependency_caps(registry, WeightingPolicy())
    economic = [cap for cap in caps if cap.kind == "economic"]
    assert [cap.id for cap in economic] == ["economic:chainlink_protocol"]
    assert economic[0].sources == ("chainlink", "uniswap_v3")
    assert len([cap for cap in caps if cap.kind == "source"]) == 6


def test_overlapping_caps_and_rounding_hold_on_final_normalized_shares():
    caps = (
        DependencyCap(id="a", kind="economic", sources=("coinbase", "kraken"), max_share_bps=3500),
        DependencyCap(id="b", kind="economic", sources=("kraken", "pyth"), max_share_bps=3500),
        DependencyCap(id="c", kind="unknown", sources=("uniswap_v3",), max_share_bps=1000),
        *(
            DependencyCap(id=source, kind="source", sources=(source,), max_share_bps=4000)
            for source in SOURCES
        ),
    )
    randomizer = random.Random(738)
    for _ in range(100):
        original = {source: randomizer.randrange(10001) for source in SOURCES}
        weights = constrain_weights(original, caps)
        assert weights == constrain_weights(
            dict(reversed(list(original.items()))), tuple(reversed(caps))
        )
        assert sum(weights.values()) <= 10000
        assert all(0 <= weights[source] <= original[source] for source in SOURCES)
        for cap in caps:
            assert (
                sum(weights[source] for source in cap.sources) * 10000
                <= sum(weights.values()) * cap.max_share_bps
            )


def test_caps_dont_refill_suppressed_weight_or_invent_feasible_solo_domain():
    caps = (DependencyCap(id="unknown", kind="unknown", sources=("coinbase",), max_share_bps=2000),)
    original = dict.fromkeys(SOURCES, 0)
    original.update(coinbase=5000, kraken=1000)
    weights = constrain_weights(original, caps)
    assert weights["coinbase"] == 250
    assert weights["kraken"] == 1000
    assert sum(weights.values()) == 1250
    original["kraken"] = 0
    assert sum(constrain_weights(original, caps).values()) == 0
    assert constrain_weights(dict.fromkeys(SOURCES, 0), caps) == dict.fromkeys(SOURCES, 0)


@pytest.mark.parametrize(
    "changes",
    [
        {"quarantine_below_bps": 7500},
        {"source_max_share_bps": 10000},
        {"unknown_max_share_bps": 10000},
        {"recovery_new_observations": 1},
        {"recovery_seconds": 0},
        {"increase_per_round_bps": True},
    ],
)
def test_invalid_policy_is_rejected(changes):
    with pytest.raises(ValidationError):
        WeightingPolicy.model_validate({**WeightingPolicy().model_dump(), **changes})


def test_existing_chain_rejects_policy_registry_and_dataset_changes(scoring, registry):
    initial, _ = scoring(600, registry)
    previous = advance_weights(initial, registry)
    next_score, _ = scoring(610, registry)
    with pytest.raises(ValueError, match="cannot change"):
        advance_weights(next_score, registry, previous=previous, policy=fast_policy())
    for selected, dataset, scoring_policy in [
        (registry.model_copy(update={"version": "changed"}), "replay:weights", None),
        (registry, "live", None),
        (registry, "replay:weights", ScoringPolicy(minimum_peers=4)),
    ]:
        revised, _ = scoring(610, selected, dataset=dataset, policy=scoring_policy)
        with pytest.raises(ValueError, match="cannot change"):
            advance_weights(revised, selected, previous=previous)
    with pytest.raises(ValueError, match="registry mismatch"):
        advance_weights(next_score, bundled_registry())


def persist_score(store, scoring, registry, at, **kwargs):
    report, observations = scoring(at, registry, **kwargs)
    store.save_dependency_registry(registry)
    store.add_many(observations)
    store.save_scoring_round(report)
    return report


def test_storage_chain_is_idempotent_reopenable_and_rejects_rollback(store, scoring, registry):
    first = persist_score(store, scoring, registry, 600)
    report = store.apply_weighting(first.report_id, dataset=first.dataset)
    assert store.apply_weighting(first.report_id, dataset=first.dataset) == report
    second = persist_score(store, scoring, registry, 610)
    next_report = store.apply_weighting(second.report_id, dataset=second.dataset)
    assert next_report.previous_report_id == report.report_id
    assert store.apply_weighting(first.report_id, dataset=first.dataset) == report
    with pytest.raises(ValueError, match="different weighting policy"):
        store.apply_weighting(first.report_id, dataset=first.dataset, policy=fast_policy())
    older = persist_score(store, scoring, registry, 590)
    with pytest.raises(ValueError, match="advance strictly"):
        store.apply_weighting(older.report_id, dataset=older.dataset)
    reopened = build_engine(str(store.engine.url))
    try:
        other = ObservationStore(reopened)
        assert other.get_weighting_round(
            report.report_id, dataset=report.dataset
        ) == report.model_dump(mode="json")
        assert other.get_weighting_round(report.report_id) is None
        assert len(other.weighting_rounds(dataset=report.dataset)) == 2
        assert other.weighting_rounds() == []
    finally:
        reopened.dispose()
    with store.session() as session:
        assert session.get(WeightingHeadRow, first.dataset).report_id == next_report.report_id


def test_concurrent_duplicate_and_successor_applications_do_not_fork(store, scoring, registry):
    scores = [persist_score(store, scoring, registry, at) for at in (600, 610, 620)]

    async def run():
        first = await asyncio.gather(
            *(
                asyncio.to_thread(
                    store.apply_weighting, scores[0].report_id, dataset=scores[0].dataset
                )
                for _ in range(4)
            )
        )
        assert len({report.report_id for report in first}) == 1
        return await asyncio.gather(
            *(
                asyncio.to_thread(store.apply_weighting, score.report_id, dataset=score.dataset)
                for score in scores[1:]
            ),
            return_exceptions=True,
        )

    outcomes = asyncio.run(run())
    assert all(isinstance(item, WeightingReport | ValueError) for item in outcomes)
    reports = store.weighting_rounds(dataset=scores[0].dataset)
    by_id = {row["report_id"]: row for row in reports}
    cursor = reports[0]
    visited = []
    while cursor:
        visited.append(cursor["report_id"])
        cursor = by_id.get(cursor["previous_report_id"])
    assert len(visited) == len(reports)
    with store.session() as session:
        assert session.get(WeightingHeadRow, scores[0].dataset).report_id == reports[0]["report_id"]


def test_failed_application_rolls_back_head_and_report(store, scoring, registry, monkeypatch):
    score = persist_score(store, scoring, registry, 600)

    def fail(*args, **kwargs):
        raise ValueError("injected transition failure")

    monkeypatch.setattr("verdict.storage.advance_weights", fail)
    with pytest.raises(ValueError, match="injected"):
        store.apply_weighting(score.report_id, dataset=score.dataset)
    with store.session() as session:
        assert session.get(WeightingHeadRow, score.dataset) is None
        assert session.scalar(select(func.count()).select_from(WeightingRoundRow)) == 0
    with pytest.raises(ValueError, match="not found"):
        store.apply_weighting(score.report_id, dataset="live")


def test_cli_and_api_keep_weights_dataset_scoped_and_publication_disabled(
    store, scoring, monkeypatch, capsys
):
    score = persist_score(store, scoring, bundled_registry(), 600)
    monkeypatch.setattr(
        "sys.argv", ["verdict", "weight", score.report_id, "--dataset", score.dataset]
    )
    main()
    report = WeightingReport.model_validate_json(capsys.readouterr().out)
    main()
    assert WeightingReport.model_validate_json(capsys.readouterr().out) == report
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        path = f"/v1/weights/{report.report_id}"
        assert client.get(path).status_code == 404
        assert client.get("/v1/weights").json() == []
        assert client.get(path, params={"dataset": score.dataset}).json() == report.model_dump(
            mode="json"
        )
        assert client.get("/v1/weights", params={"dataset": score.dataset}).json() == [
            report.model_dump(mode="json")
        ]
        assert client.get("/v1/weights", params={"limit": 101}).status_code == 422
        assert client.get("/v1/weights/bad").status_code == 422
        assert client.post(path, json={}).status_code == 405
        assert client.get("/v1/feed/ETH-USD").status_code == 503
    monkeypatch.setattr("sys.argv", ["verdict", "weight", score.report_id])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_corrupt_report_or_head_cannot_drive_new_state(store, scoring, registry):
    first = persist_score(store, scoring, registry, 600)
    report = store.apply_weighting(first.report_id, dataset=first.dataset)
    with pytest.raises(ValueError, match="checksum mismatch"):
        WeightingReport.model_validate(report.model_copy(update={"total_weight_bps": 123}))
    second = persist_score(store, scoring, registry, 610)
    with store.session() as session, session.begin():
        session.execute(update(WeightingRoundRow).values(scoring_report_id="0" * 64))
    with pytest.raises(ValueError, match="metadata mismatch"):
        store.apply_weighting(second.report_id, dataset=second.dataset)
    with store.session() as session:
        assert session.get(WeightingHeadRow, first.dataset).report_id == report.report_id


def test_upgrade_from_0007_preserves_scoring_evidence(store, scoring, registry):
    config = Config(str(ROOT / "alembic.ini"))
    command.downgrade(config, "0007")
    score = persist_score(store, scoring, registry, 600)
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        assert client.get("/health/ready").status_code == 503
    command.upgrade(config, "head")
    command.check(config)
    store.check_ready()
    assert store.get_scoring_round(score.report_id, dataset=score.dataset) == score.model_dump(
        mode="json"
    )
    report = store.apply_weighting(score.report_id, dataset=score.dataset)
    assert report.previous_report_id is None


@pytest.mark.parametrize("damage", ["reset", "removed", "older", "missing_report"])
def test_damaged_head_cannot_fork_retained_history(store, scoring, registry, damage):
    scores = [persist_score(store, scoring, registry, at) for at in (600, 610, 620)]
    first, second = [
        store.apply_weighting(score.report_id, dataset=score.dataset) for score in scores[:2]
    ]
    with store.session() as session, session.begin():
        head = session.get(WeightingHeadRow, first.dataset)
        if damage == "removed":
            session.delete(head)
        else:
            head.report_id = {"reset": None, "older": first.report_id, "missing_report": "0" * 64}[
                damage
            ]
    for score in (scores[2], scores[0]):
        with pytest.raises(ValueError, match="head does not match latest"):
            store.apply_weighting(score.report_id, dataset=score.dataset)
    assert [row["report_id"] for row in store.weighting_rounds(dataset=first.dataset)] == [
        second.report_id,
        first.report_id,
    ]
    with store.session() as session:
        head = session.get(WeightingHeadRow, first.dataset)
        assert head is None if damage == "removed" else head.report_id != second.report_id
