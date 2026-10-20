import asyncio
from datetime import UTC, datetime, timedelta
from decimal import ROUND_UP, Decimal, localcontext
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete, func, select, update

from verdict.aggregation import aggregate
from verdict.api import create_app
from verdict.cli import main
from verdict.confidence import (
    ConfidencePolicy,
    ConfidenceReport,
    QualityContribution,
    assess_confidence,
    build_confidence_report,
    components,
)
from verdict.config import Settings
from verdict.dependencies import DependencyRegistry, bundled_registry, checksum
from verdict.models import SOURCES
from verdict.replay import load_observations
from verdict.scoring import ScoringPolicy, score_observations
from verdict.storage import (
    AggregationRoundRow,
    ConfidenceRoundRow,
    DependencyRegistryRow,
    ObservationRow,
    ObservationStore,
    ScoringRoundRow,
    WeightingRoundRow,
    build_engine,
)
from verdict.weighting import WeightingPolicy, advance_weights

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 10, 20, tzinfo=UTC)


@pytest.fixture
def history():
    def make(*, unknown=(), ramp=2000, price=None, base=BASE, reviewed_at=None, outage=False):
        data = bundled_registry().model_dump(mode="json")
        removed = {node["id"] for node in data["nodes"] if node["kind"] == "unknown"} - set(unknown)
        data["nodes"] = [node for node in data["nodes"] if node["id"] not in removed]
        data["edges"] = [edge for edge in data["edges"] if edge["upstream"] not in removed]
        data["version"] = "synthetic-confidence-graph"
        data["scope"] = "TEST ONLY completeness assumption; no independent-domain evidence."
        if reviewed_at is not None:
            data["reviewed_at"] = reviewed_at.isoformat()
        registry = DependencyRegistry.model_validate(data)
        prices = dict(
            zip(
                sorted(SOURCES),
                ("99.98", "99.99", "100", "100.01", "100.02", "100.03"),
                strict=True,
            )
        )
        templates = load_observations(ROOT / "simulations/six-provider.json")
        rows = [
            row.model_copy(
                update={
                    "dataset": "replay:confidence",
                    "provider_event_id": f"{row.source}-{time}",
                    "timestamp": base + timedelta(seconds=time),
                    "received_at": base + timedelta(seconds=time),
                    "value": Decimal(price(row.source, time) if price else prices[row.source]),
                    "raw_payload": {"synthetic": "confidence", "source": row.source, "time": time},
                }
            )
            for time in range(10, 81, 10)
            for row in templates
            if not outage or time <= 40
        ]
        scores, weights = [], []
        score_policy = ScoringPolicy(
            window_seconds=60,
            minimum_fresh_samples=3,
            minimum_comparisons=3,
            minimum_unique_observations=3,
        )
        weight_policy = WeightingPolicy(
            recovery_new_observations=2,
            recovery_seconds=10,
            increase_per_round_bps=ramp,
            source_budget_bps=2000,
        )
        for time in (60, 70, 80):
            score = score_observations(
                rows,
                dataset="replay:confidence",
                cutoff=base + timedelta(seconds=time),
                registry=registry,
                policy=score_policy,
            )
            scores.append(score)
            weights.append(
                advance_weights(
                    score, registry, policy=weight_policy, previous=weights[-1] if weights else None
                )
            )
        return registry, rows, scores, weights

    return make


def assessment(history, **kwargs):
    registry, _, scores, weights = history
    return assess_confidence(aggregate(scores[-1], weights[-1]), scores[-1], registry, **kwargs)


def checks(report):
    return {item.name: item for item in report.gates}


def retain(store, history):
    registry, rows, scores, weights = history
    store.save_dependency_registry(registry)
    store.add_many(rows)
    for score, expected in zip(scores, weights, strict=True):
        store.save_scoring_round(score)
        assert (
            store.apply_weighting(score.report_id, dataset=score.dataset, policy=expected.policy)
            == expected
        )
    aggregation = aggregate(scores[-1], weights[-1])
    store.save_aggregation_round(aggregation)
    return assessment(history)


def test_high_quality_cannot_establish_independent_domains(history):
    report = assessment(history())
    assert report.quality_score_bps == 9700
    assert report.components.agreement_bps == 9700
    assert report.dispersion_bps == 3
    assert report.total_weight_bps == 9996
    assert report.eligible_source_count == 6
    assert report.candidate_value == Decimal("100")
    assert report.source_concentration_bps == 1667
    assert report.economic_concentration_bps == 3334
    assert report.unknown_share_bps == 0
    assert [item.name for item in report.gates if not item.passed] == ["independent_domains"]
    assert report.reasons == ("independent_domains:unknown",)
    assert report.status == "unavailable"
    assert report.value is report.confidence is report.independent_domain_count is None
    assert not report.publication_ready


def test_quality_weighted_components_have_explicit_units():
    contributions = (
        QualityContribution(
            source="coinbase",
            weight_bps=8000,
            value="100",
            uptime_bps=10000,
            freshness_bps=9000,
            consistency_bps=10000,
            comparison_coverage_bps=7000,
            unknown_dependencies=(),
        ),
        QualityContribution(
            source="kraken",
            weight_bps=2000,
            value="100",
            uptime_bps=5000,
            freshness_bps=4000,
            consistency_bps=6000,
            comparison_coverage_bps=10000,
            unknown_dependencies=("unknown:kraken",),
        ),
    )
    parts, dispersion = components(contributions, ConfidencePolicy())
    assert parts.model_dump() == {
        "uptime_bps": 9000,
        "freshness_bps": 8000,
        "consistency_bps": 9200,
        "comparison_coverage_bps": 7600,
        "allocation_bps": 10000,
        "agreement_bps": 10000,
        "resolved_dependency_share_bps": 8000,
    }
    assert dispersion == 0


def test_small_allocations_are_not_normalized_into_high_quality(history):
    report = assessment(history(ramp=250))
    assert report.total_weight_bps == report.quality_score_bps == 3000
    assert not checks(report)["allocated_budget"].passed
    assert not checks(report)["quality"].passed
    assert report.components.freshness_bps == 10000


def test_unknown_share_is_pooled_and_rounded_conservatively(history):
    evidence = history(unknown=("unknown:coinbase",))
    report = assessment(evidence)
    assert report.unknown_share_bps == 1667
    assert report.components.resolved_dependency_share_bps == 8333
    assert report.quality_score_bps == 8333
    assert not checks(report)["unknown_share"].passed
    variant = assessment(evidence, policy=ConfidencePolicy(maximum_unknown_share_bps=1667))
    assert checks(variant)["unknown_share"].passed
    assert variant.status == "unavailable" and variant.confidence is None


@pytest.mark.parametrize(
    ("field", "passing", "failing", "gate"),
    [
        ("minimum_total_weight_bps", 9996, 9997, "allocated_budget"),
        ("minimum_quality_bps", 9700, 9701, "quality"),
        ("maximum_source_share_bps", 1667, 1666, "source_concentration"),
        ("maximum_economic_share_bps", 3334, 3333, "economic_concentration"),
    ],
)
def test_gate_boundaries_are_inclusive(history, field, passing, failing, gate):
    evidence = history()
    for threshold, expected in ((passing, True), (failing, False)):
        report = assessment(evidence, policy=ConfidencePolicy(**{field: threshold}))
        assert checks(report)[gate].passed is expected


@pytest.mark.parametrize(
    ("offset_us", "future", "expired"),
    [
        (-5000000, False, False),
        (-5000001, False, True),
        (1, True, False),
    ],
)
def test_review_age_gates_keep_microsecond_boundaries(history, offset_us, future, expired):
    report = assessment(
        history(reviewed_at=BASE + timedelta(seconds=80, microseconds=offset_us)),
        policy=ConfidencePolicy(registry_max_age_seconds=5),
    )
    assert checks(report)["registry_not_future"].passed is not future
    assert checks(report)["registry_not_expired"].passed is not expired


def test_outliers_are_retained_but_do_not_enter_quality_weights(history):
    report = assessment(
        history(price=lambda source, time: "135" if source == "coinbase" and time == 80 else "100")
    )
    target = next(item for item in report.contributions if item.source == "coinbase")
    assert target.value == Decimal("135") and target.weight_bps == 0
    assert report.eligible_source_count == 5
    assert report.dispersion_bps == 0
    assert report.components.agreement_bps == 10000
    strict = assessment(
        history(price=lambda source, time: "135" if source == "coinbase" and time == 80 else "100"),
        policy=ConfidencePolicy(minimum_sources=6),
    )
    assert not checks(strict)["eligible_sources"].passed


def test_coordinated_move_can_score_high_but_cannot_bypass_independence(history):
    report = assessment(history(price=lambda source, time: "135" if time >= 70 else "100"))
    assert report.candidate_value == Decimal("135")
    assert report.quality_score_bps == 9996
    assert report.status == "unavailable" and report.confidence is None


def test_outage_has_zero_quality_and_unknown_concentration(history):
    report = assessment(history(outage=True))
    assert report.quality_score_bps == report.total_weight_bps == 0
    assert report.candidate_value is report.dispersion_bps is None
    assert report.source_concentration_bps is report.unknown_share_bps is None
    assert not checks(report)["candidate"].passed


def test_decimal_context_and_reordered_input_do_not_change_report(history):
    retained = history()
    expected = assessment(retained).model_dump_json()
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        assert assessment(retained).model_dump_json() == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"minimum_sources": 2},
        {"minimum_independent_domains": 0},
        {"maximum_source_share_bps": 10000},
        {"maximum_unknown_share_bps": 10000},
        {"registry_max_age_seconds": 0},
        {"minimum_quality_bps": True},
        {"full_disagreement_bps": 0},
        {"allow_unknown_domains": True},
    ],
)
def test_policy_cannot_disable_required_gates(changes):
    with pytest.raises(ValidationError):
        ConfidencePolicy.model_validate(changes)


def resign(model, **updates):
    data = model.model_dump(mode="json", exclude={"report_id"})
    data.update(updates)
    return type(model).model_validate({**data, "report_id": checksum(data)})


def test_wrong_pins_and_model_copies_are_rejected(history):
    registry, _, scores, weights = history()
    aggregation = aggregate(scores[-1], weights[-1])
    with pytest.raises(ValueError, match="evidence mismatch"):
        assess_confidence(aggregation, scores[0], registry)
    with pytest.raises(ValueError, match="evidence mismatch"):
        assess_confidence(aggregation, scores[-1], bundled_registry())
    report = assessment((registry, [], scores, weights))
    with pytest.raises(ValueError, match="checksum mismatch"):
        ConfidenceReport.model_validate(report.model_copy(update={"quality_score_bps": 10000}))
    with pytest.raises(ValueError, match="does not reproduce"):
        resign(report, quality_score_bps=10000)
    data = report.model_dump(mode="json", exclude={"report_id"})
    data["gates"][-1]["observed"] = 6
    data["gates"][-1]["passed"] = True
    with pytest.raises(ValueError, match="does not reproduce"):
        ConfidenceReport.model_validate({**data, "report_id": checksum(data)})
    with pytest.raises(ValidationError):
        resign(
            report, independent_domain_count=6, value="100", confidence=9700, publication_ready=True
        )


def test_storage_reopens_with_policy_variants_and_dataset_isolation(store, history):
    report = retain(store, history())
    assert store.save_confidence_round(report)
    assert not store.save_confidence_round(report)
    variant = build_confidence_report(
        store,
        aggregation_report_id=report.aggregation_report_id,
        dataset=report.dataset,
        policy=ConfidencePolicy(minimum_quality_bps=9800),
    )
    assert store.save_confidence_round(variant)
    assert variant.report_id != report.report_id
    reopened = build_engine(str(store.engine.url))
    try:
        other = ObservationStore(reopened)
        assert other.get_confidence_round(
            report.report_id, dataset=report.dataset
        ) == report.model_dump(mode="json")
        assert other.get_confidence_round(report.report_id) is None
        assert other.confidence_rounds() == []
        assert len(other.confidence_rounds(dataset=report.dataset)) == 2
        assert len(other.confidence_rounds(dataset=report.dataset, limit=1)) == 1
    finally:
        reopened.dispose()


def test_concurrent_duplicate_assessments_are_saved_once(store, history):
    report = retain(store, history())

    async def run():
        return await asyncio.gather(
            *(asyncio.to_thread(store.save_confidence_round, report) for _ in range(4))
        )

    assert asyncio.run(run()).count(True) == 1


@pytest.mark.parametrize(
    "table",
    [
        AggregationRoundRow,
        ScoringRoundRow,
        DependencyRegistryRow,
        WeightingRoundRow,
        ObservationRow,
    ],
)
def test_missing_upstream_evidence_rolls_back_assessment(store, history, table):
    report = retain(store, history())
    with store.session() as session, session.begin():
        session.execute(delete(table))
    with pytest.raises(ValueError, match="retained|missing"):
        store.save_confidence_round(report)
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(ConfidenceRoundRow)) == 0


def test_changed_original_observation_is_rechecked_after_aggregation(store, history):
    retained = history()
    report = retain(store, retained)
    with store.session() as session, session.begin():
        row = session.get(ObservationRow, retained[1][0].observation_id)
        row.payload = row.payload.replace('"synthetic":"confidence"', '"synthetic":"changed"')
    with pytest.raises(ValueError, match="missing or changed"):
        store.save_confidence_round(report)
    assert store.confidence_rounds(dataset=report.dataset) == []


def test_forged_self_consistent_contributions_cannot_replace_evidence(store, history):
    report = retain(store, history())
    forged = resign(report, registry_id="0" * 64)
    with pytest.raises(ValueError, match="does not reproduce"):
        store.save_confidence_round(forged)


@pytest.mark.parametrize("damage", ["payload", "metadata"])
def test_saved_corruption_is_detected(store, history, damage):
    report = retain(store, history())
    store.save_confidence_round(report)
    with store.session() as session, session.begin():
        values = {"payload": "{}"} if damage == "payload" else {"aggregation_report_id": "0" * 64}
        session.execute(update(ConfidenceRoundRow).values(**values))
    with pytest.raises(ValueError):
        store.get_confidence_round(report.report_id, dataset=report.dataset)
    if damage == "payload":
        with pytest.raises(ValueError, match="identity conflict"):
            store.save_confidence_round(report)


def test_cli_api_keep_live_output_unavailable(store, history, monkeypatch, capsys, tmp_path):
    report = retain(store, history())
    policy_path = tmp_path / "confidence.json"
    policy_path.write_text(report.policy.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "verdict",
            "confidence",
            report.aggregation_report_id,
            "--dataset",
            report.dataset,
            "--policy",
            str(policy_path),
        ],
    )
    for _ in range(2):
        main()
        assert ConfidenceReport.model_validate_json(capsys.readouterr().out) == report
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        path = f"/v1/confidence/{report.report_id}"
        assert client.get(path).status_code == 404
        assert client.get("/v1/confidence").json() == []
        assert client.get(path, params={"dataset": report.dataset}).json() == report.model_dump(
            mode="json"
        )
        assert client.get("/v1/confidence", params={"dataset": report.dataset}).json() == [
            report.model_dump(mode="json")
        ]
        assert client.get("/v1/confidence/bad").status_code == 422
        assert client.get("/v1/confidence", params={"limit": 101}).status_code == 422
        assert client.post(path, json={}).status_code == 405
        assert client.get("/v1/feed/ETH-USD").status_code == 503
    monkeypatch.setattr("sys.argv", ["verdict", "confidence", report.aggregation_report_id])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_upgrade_from_0009_preserves_aggregation(store, history):
    config = Config(str(ROOT / "alembic.ini"))
    command.downgrade(config, "0009")
    report = retain(store, history())
    before = store.get_aggregation_round(report.aggregation_report_id, dataset=report.dataset)
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        assert client.get("/health/ready").status_code == 503
    command.upgrade(config, "head")
    command.check(config)
    store.check_ready()
    assert (
        store.get_aggregation_round(report.aggregation_report_id, dataset=report.dataset) == before
    )
    assert store.save_confidence_round(report)
