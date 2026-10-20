import asyncio
import random
from datetime import UTC, datetime, timedelta
from decimal import ROUND_UP, Decimal, localcontext
from itertools import permutations
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete, func, select, update

from verdict.aggregation import (
    AggregationReport,
    aggregate,
    build_aggregation_report,
    weighted_median,
)
from verdict.api import create_app
from verdict.cli import main
from verdict.config import Settings
from verdict.dependencies import DependencyRegistry, bundled_registry, checksum
from verdict.replay import load_observations
from verdict.scoring import ScoringPolicy, score_observations
from verdict.storage import (
    AggregationRoundRow,
    DependencyRegistryRow,
    ObservationRow,
    ObservationStore,
    ScoringRoundRow,
    WeightingHeadRow,
    WeightingRoundRow,
    build_engine,
)
from verdict.weighting import WeightingPolicy, advance_weights

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 10, 16, tzinfo=UTC)


@pytest.mark.parametrize(
    ("pairs", "expected"),
    [
        ([], None),
        ([("100", 0), ("200", 0)], None),
        ([("100", 1)], "100"),
        ([("100", 5000), ("200", 5000)], "100"),
        ([("100", 4999), ("200", 5001)], "200"),
        ([("100", 1), ("200", 1), ("300", 1)], "200"),
        ([("1", 0), ("100", 2), ("100000", 0)], "100"),
        ([("100.00", 1), ("100.0", 1), ("200", 2)], "100"),
        ([("0.000000000000000001", 2), ("99999999999999999999", 1)], "0.000000000000000001"),
    ],
)
def test_weighted_median_boundaries(pairs, expected):
    result = weighted_median((Decimal(value), weight) for value, weight in pairs)
    assert result == (Decimal(expected) if expected is not None else None)
    if result is not None:
        assert format(result, "f") == expected


@pytest.mark.parametrize(
    "pair",
    [
        (1.0, 1),
        ("100", 1),
        (Decimal("NaN"), 1),
        (Decimal("Infinity"), 1),
        (Decimal("-1"), 0),
        (Decimal("0"), 1),
        (Decimal("100"), True),
        (Decimal("100"), -1),
        (Decimal("100"), 10001),
        (Decimal("100"), 1.0),
    ],
)
def test_invalid_median_inputs_are_rejected_even_at_zero_weight(pair):
    with pytest.raises(ValueError):
        weighted_median([pair])


def test_median_is_order_and_decimal_context_independent():
    pairs = [(Decimal("100.0000"), 2), (Decimal("100.0"), 2), (Decimal("101"), 4)]
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        for ordered in permutations(pairs):
            assert str(weighted_median(ordered)) == "100"
    randomizer = random.Random(91)
    for _ in range(50):
        pairs = [
            (Decimal(randomizer.randrange(1, 1000)), randomizer.randrange(100)) for _ in range(6)
        ]
        result = weighted_median(pairs)
        total = sum(weight for _, weight in pairs)
        if total:
            assert result in [value for value, weight in pairs if weight]
            assert sum(weight for value, weight in pairs if value < result) * 2 < total
            assert sum(weight for value, weight in pairs if value <= result) * 2 >= total


@pytest.fixture
def evidence():
    def make(*, complete=True, outlier=False, omit=()):
        data = bundled_registry().model_dump(mode="json")
        if complete:
            # Synthetic assumption for nonzero tests; never provider evidence.
            unknown = {node["id"] for node in data["nodes"] if node["kind"] == "unknown"}
            data["nodes"] = [node for node in data["nodes"] if node["id"] not in unknown]
            data["edges"] = [edge for edge in data["edges"] if edge["upstream"] not in unknown]
            data["version"] = "synthetic-aggregation-complete"
            data["scope"] = "TEST ONLY: assumes complete upstream evidence for candidate tests."
        registry = DependencyRegistry.model_validate(data)
        prices = {
            "binance_us": "99.8",
            "chainlink": "99.9",
            "coinbase": "100",
            "kraken": "100.1",
            "pyth": "100.2",
            "uniswap_v3": "100.3",
        }
        templates = load_observations(ROOT / "simulations/six-provider.json")
        rows = [
            row.model_copy(
                update={
                    "dataset": "replay:aggregation",
                    "provider_event_id": f"{row.source}-{time}",
                    "timestamp": BASE + timedelta(seconds=time),
                    "received_at": BASE + timedelta(seconds=time),
                    "value": Decimal(
                        "135"
                        if outlier and row.source == "coinbase" and time == 80
                        else prices[row.source]
                    ),
                    "raw_payload": {"synthetic": "aggregation", "time": time, "source": row.source},
                }
            )
            for time in range(10, 81, 10)
            for row in templates
            if not (row.source in omit and time > 40)
        ]
        scoring_policy = ScoringPolicy(
            window_seconds=60,
            minimum_fresh_samples=3,
            minimum_comparisons=3,
            minimum_unique_observations=3,
        )
        weighting_policy = WeightingPolicy(recovery_new_observations=2, recovery_seconds=10)
        scores, weights = [], []
        for time in (60, 70, 80):
            score = score_observations(
                rows,
                dataset="replay:aggregation",
                cutoff=BASE + timedelta(seconds=time),
                registry=registry,
                policy=scoring_policy,
            )
            scores.append(score)
            weights.append(
                advance_weights(
                    score,
                    registry,
                    policy=weighting_policy,
                    previous=weights[-1] if weights else None,
                )
            )
        return registry, rows, scores, weights

    return make


def persist(store, evidence):
    registry, rows, scores, weights = evidence
    store.save_dependency_registry(registry)
    store.add_many(rows)
    for score, expected in zip(scores, weights, strict=True):
        store.save_scoring_round(score)
        assert (
            store.apply_weighting(score.report_id, dataset=score.dataset, policy=expected.policy)
            == expected
        )
    return aggregate(scores[-1], weights[-1])


def test_candidate_retains_weights_exclusions_and_exact_half_rule(evidence):
    _, _, scores, weights = evidence()
    report = aggregate(scores[-1], weights[-1])
    assert report.candidate_value == Decimal("100")
    assert report.ordered_sources == (
        "binance_us",
        "chainlink",
        "coinbase",
        "kraken",
        "pyth",
        "uniswap_v3",
    )
    assert report.total_weight_bps == 3000
    assert report.unallocated_budget_bps == 7000
    assert report.eligible_source_count == 6
    assert not any(item.exclusion_reasons for item in report.inputs)
    assert report.status == "diagnostic_candidate"
    assert report.confidence is report.independent_domain_count is None
    assert not report.publication_ready
    with localcontext() as context:
        context.prec = 2
        assert aggregate(scores[-1], weights[-1]).model_dump_json() == report.model_dump_json()


def test_outlier_has_no_vote_and_keeps_its_evidence(evidence):
    _, _, scores, weights = evidence(outlier=True)
    report = aggregate(scores[-1], weights[-1])
    target = next(item for item in report.inputs if item.sample.source == "coinbase")
    assert target.sample.value == Decimal("135")
    assert target.weight.weight_bps == 0
    assert "comparison_outlier" in target.exclusion_reasons
    assert "state_quarantined" in target.exclusion_reasons
    assert "coinbase" not in report.ordered_sources
    assert report.candidate_value == Decimal("100.1")


def test_unknown_dependencies_and_probation_yield_no_candidate(evidence):
    _, _, scores, weights = evidence(complete=False)
    for score, weight in ((scores[-1], weights[-1]), (scores[0], weights[0])):
        report = aggregate(score, weight)
        assert report.candidate_value is None
        assert report.status == "unavailable"
        assert report.total_weight_bps == report.eligible_source_count == 0
        assert report.ordered_sources == ()
        assert "zero_total_weight" in report.reasons


def test_stale_sources_stay_excluded_without_falling_back(evidence):
    _, _, scores, weights = evidence(omit=("coinbase",))
    report = aggregate(scores[-1], weights[-1])
    target = next(item for item in report.inputs if item.sample.source == "coinbase")
    assert target.sample.availability == "stale"
    assert "current_stale" in target.exclusion_reasons
    assert target.weight.weight_bps == 0


def test_evidence_mismatch_or_unvalidated_model_copies_are_rejected(evidence):
    _, _, scores, weights = evidence()
    with pytest.raises(ValueError, match="evidence mismatch"):
        aggregate(scores[0], weights[-1])
    with pytest.raises(ValueError, match="checksum mismatch"):
        aggregate(scores[-1], weights[-1].model_copy(update={"dataset": "live"}))
    report = aggregate(scores[-1], weights[-1])
    with pytest.raises(ValueError, match="checksum mismatch"):
        AggregationReport.model_validate(
            report.model_copy(update={"candidate_value": Decimal("1")})
        )
    data = report.model_dump(mode="json", exclude={"report_id"})
    data["candidate_value"] = "101"
    with pytest.raises(ValueError, match="weighted median mismatch"):
        AggregationReport.model_validate({**data, "report_id": checksum(data)})
    with pytest.raises(ValidationError):
        AggregationReport.model_validate({**report.model_dump(), "publication_ready": True})


def resign(model, **updates):
    data = model.model_dump(mode="json", exclude={"report_id"})
    data.update(updates)
    return type(model).model_validate({**data, "report_id": checksum(data)})


@pytest.mark.parametrize("failure", ["stale", "outlier", "cold_start", "unscored", "wrong_id"])
def test_inconsistent_positive_weight_is_rejected_without_redistributing(evidence, failure):
    _, _, scores, weights = evidence()
    score = scores[-1]
    data = score.model_dump(mode="json", exclude={"report_id"})
    sample = data["ticks"][-1]["sources"][0]
    if failure == "stale":
        sample["availability"] = "stale"
    elif failure == "outlier":
        sample["comparison"] = "outlier"
    elif failure == "wrong_id":
        sample["observation_id"] = "0" * 64
    elif failure == "cold_start":
        data["sources"][0]["cold_start"] = True
    else:
        data["sources"][0]["score_bps"] = None
    score = type(score).model_validate({**data, "report_id": checksum(data)})
    weight = resign(weights[-1], scoring_report_id=score.report_id)
    with pytest.raises(ValueError, match="positive weight|observation mismatch"):
        aggregate(score, weight)


def test_empty_history_and_real_cold_start_have_no_candidate(evidence):
    registry, rows, _, _ = evidence()
    for inputs in ([], rows):
        score = score_observations(
            inputs,
            dataset="replay:aggregation",
            cutoff=BASE + timedelta(seconds=10),
            registry=registry,
        )
        weights = advance_weights(score, registry)
        report = aggregate(score, weights)
        assert report.candidate_value is None
        assert report.eligible_source_count == 0
        assert all(item.weight.weight_bps == 0 for item in report.inputs)


def test_storage_reopens_and_does_not_follow_later_observations(store, evidence):
    retained = evidence()
    report = persist(store, retained)
    assert store.save_aggregation_round(report)
    assert not store.save_aggregation_round(report)
    _, rows, _, weights = retained
    later = rows[-1].model_copy(
        update={
            "provider_event_id": "after-aggregation",
            "value": Decimal("9999"),
            "timestamp": BASE + timedelta(seconds=90),
            "received_at": BASE + timedelta(seconds=90),
        }
    )
    store.add(later)
    assert (
        build_aggregation_report(
            store, weighting_report_id=weights[-1].report_id, dataset=report.dataset
        )
        == report
    )
    reopened = build_engine(str(store.engine.url))
    try:
        other = ObservationStore(reopened)
        assert other.get_aggregation_round(report.report_id, dataset=report.dataset) == (
            report.model_dump(mode="json")
        )
        assert other.get_aggregation_round(report.report_id) is None
        assert other.aggregation_rounds() == []
    finally:
        reopened.dispose()


def test_concurrent_duplicate_saves_are_idempotent(store, evidence):
    report = persist(store, evidence())

    async def run():
        return await asyncio.gather(
            *(asyncio.to_thread(store.save_aggregation_round, report) for _ in range(4))
        )

    results = asyncio.run(run())
    assert results.count(True) == 1
    assert len(store.aggregation_rounds(dataset=report.dataset)) == 1


@pytest.mark.parametrize("damage", ["observation", "changed_input", "registry", "score", "weights"])
def test_missing_or_changed_evidence_rolls_back_new_report(store, evidence, damage):
    report = persist(store, evidence())
    with store.session() as session, session.begin():
        if damage == "changed_input":
            row = session.get(ObservationRow, report.inputs[0].sample.observation_id)
            row.payload = row.payload.replace('"synthetic":"aggregation"', '"synthetic":"changed"')
        else:
            table = {
                "observation": ObservationRow,
                "registry": DependencyRegistryRow,
                "score": ScoringRoundRow,
                "weights": WeightingRoundRow,
            }[damage]
            session.execute(delete(table))
    with pytest.raises(ValueError, match="retained|missing|changed"):
        store.save_aggregation_round(report)
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(AggregationRoundRow)) == 0


def test_self_consistent_report_must_reproduce_from_retained_evidence(store, evidence):
    report = persist(store, evidence())
    data = report.model_dump(mode="json", exclude={"report_id"})
    data["input_sha256"] = "0" * 64
    forged = AggregationReport.model_validate({**data, "report_id": checksum(data)})
    with pytest.raises(ValueError, match="does not reproduce"):
        store.save_aggregation_round(forged)
    assert store.aggregation_rounds(dataset=report.dataset) == []


@pytest.mark.parametrize("damage", ["payload", "metadata"])
def test_corrupt_saved_results_are_rejected(store, evidence, damage):
    report = persist(store, evidence())
    store.save_aggregation_round(report)
    with store.session() as session, session.begin():
        values = {"payload": "{}"} if damage == "payload" else {"weighting_report_id": "0" * 64}
        session.execute(update(AggregationRoundRow).values(**values))
    with pytest.raises(ValueError):
        store.get_aggregation_round(report.report_id, dataset=report.dataset)


def test_cli_api_and_dataset_isolation(store, evidence, monkeypatch, capsys):
    report = persist(store, evidence())
    monkeypatch.setattr(
        "sys.argv",
        [
            "verdict",
            "aggregate",
            report.weighting_report_id,
            "--dataset",
            report.dataset,
        ],
    )
    for _ in range(2):
        main()
        assert AggregationReport.model_validate_json(capsys.readouterr().out) == report
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        path = f"/v1/aggregations/{report.report_id}"
        assert client.get(path).status_code == 404
        assert client.get("/v1/aggregations").json() == []
        assert client.get(path, params={"dataset": report.dataset}).json() == report.model_dump(
            mode="json"
        )
        assert client.get("/v1/aggregations", params={"dataset": report.dataset}).json() == [
            report.model_dump(mode="json"),
        ]
        assert client.get("/v1/aggregations/bad").status_code == 422
        assert client.get("/v1/aggregations", params={"limit": 101}).status_code == 422
        assert client.post(path, json={}).status_code == 405
        assert client.get("/v1/feed/ETH-USD").status_code == 503
    monkeypatch.setattr("sys.argv", ["verdict", "aggregate", report.weighting_report_id])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_upgrade_from_0008_preserves_weights_and_head(store, evidence):
    config = Config(str(ROOT / "alembic.ini"))
    command.downgrade(config, "0008")
    report = persist(store, evidence())
    with store.session() as session:
        head = session.get(WeightingHeadRow, report.dataset).report_id
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        assert client.get("/health/ready").status_code == 503
    command.upgrade(config, "head")
    command.check(config)
    store.check_ready()
    with store.session() as session:
        assert session.get(WeightingHeadRow, report.dataset).report_id == head
    assert store.save_aggregation_round(report)
    assert store.get_aggregation_round(
        report.report_id, dataset=report.dataset
    ) == report.model_dump(mode="json")
