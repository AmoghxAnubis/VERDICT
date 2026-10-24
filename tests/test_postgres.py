import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from verdict.aggregation import build_aggregation_report
from verdict.confidence import build_confidence_report
from verdict.config import get_settings
from verdict.dependencies import bundled_registry
from verdict.queue_models import QueuedAttempt
from verdict.scoring import ScoringPolicy, build_scoring_report
from verdict.statistics import analyze
from verdict.storage import (
    AttemptConflict,
    IngestionReceiptRow,
    ObservationConflict,
    ObservationStore,
    build_engine,
)


@pytest.mark.skipif(
    not os.getenv("VERDICT_TEST_DATABASE_URL"), reason="PostgreSQL URL not provided"
)
def test_postgres_migration_and_storage_contract(observation, monkeypatch):
    database_url = os.environ["VERDICT_TEST_DATABASE_URL"]
    monkeypatch.setenv("VERDICT_DATABASE_URL", database_url)
    get_settings.cache_clear()
    engine = build_engine(database_url)
    try:
        command.upgrade(Config("alembic.ini"), "head")
        command.check(Config("alembic.ini"))
        store = ObservationStore(engine)
        store.check_ready()
        registry = bundled_registry().model_copy(update={"version": f"test-{uuid.uuid4().hex}"})
        assert store.save_dependency_registry(registry)
        assert not store.save_dependency_registry(registry)
        assert store.get_dependency_registry(registry.registry_id) == registry
        with pytest.raises(ValueError, match="version conflict"):
            store.save_dependency_registry(registry.model_copy(update={"scope": "changed"}))
        isolated = observation.model_copy(update={"dataset": f"replay:postgres-{uuid.uuid4().hex}"})
        assert store.add(isolated)
        assert not store.add(isolated)
        assert store.list(dataset=isolated.dataset) == [isolated]
        scored = build_scoring_report(
            store,
            dataset=isolated.dataset,
            cutoff=isolated.received_at,
            registry_id=registry.registry_id,
            policy=ScoringPolicy(),
        )
        assert store.save_scoring_round(scored)
        assert not store.save_scoring_round(scored)
        assert store.get_scoring_round(scored.report_id, dataset=scored.dataset) == (
            scored.model_dump(mode="json")
        )
        with ThreadPoolExecutor(max_workers=4) as workers:
            weighted = list(
                workers.map(
                    lambda _: store.apply_weighting(scored.report_id, dataset=scored.dataset),
                    range(4),
                )
            )
        assert all(item == weighted[0] for item in weighted)
        successor = build_scoring_report(
            store,
            dataset=isolated.dataset,
            cutoff=scored.cutoff + timedelta(seconds=scored.policy.step_seconds),
            registry_id=registry.registry_id,
            policy=scored.policy,
        )
        assert store.save_scoring_round(successor)
        next_weight = store.apply_weighting(successor.report_id, dataset=successor.dataset)
        assert next_weight.previous_report_id == weighted[0].report_id
        assert store.apply_weighting(scored.report_id, dataset=scored.dataset) == weighted[0]
        assert store.get_weighting_round(next_weight.report_id) is None
        assert store.get_weighting_round(
            next_weight.report_id, dataset=scored.dataset
        ) == next_weight.model_dump(mode="json")
        assert not next_weight.publication_ready
        candidate = build_aggregation_report(
            store, weighting_report_id=next_weight.report_id, dataset=scored.dataset
        )
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: store.save_aggregation_round(candidate), range(4)))
        assert results.count(True) == 1
        assert store.get_aggregation_round(candidate.report_id) is None
        assert store.get_aggregation_round(
            candidate.report_id, dataset=scored.dataset
        ) == candidate.model_dump(mode="json")
        assert candidate.candidate_value is None and not candidate.publication_ready
        confidence = build_confidence_report(
            store, aggregation_report_id=candidate.report_id, dataset=scored.dataset
        )
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: store.save_confidence_round(confidence), range(4)))
        assert results.count(True) == 1
        assert store.get_confidence_round(confidence.report_id) is None
        assert store.get_confidence_round(
            confidence.report_id, dataset=scored.dataset
        ) == confidence.model_dump(mode="json")
        assert confidence.value is confidence.confidence is None
        report = analyze([isolated], dataset=isolated.dataset, cutoff=isolated.received_at)
        assert store.save_round(report)
        assert not store.save_round(report)
        assert store.get_round(report.round_id, dataset=isolated.dataset) == report.model_dump(
            mode="json"
        )
        live = isolated.model_copy(
            update={
                "dataset": "live",
                "source": "kraken",
                "provider_event_id": f"test-{uuid.uuid4().hex}",
            }
        )
        store.record_attempt("kraken", error_type="ConnectError")
        assert store.record_attempt("kraken", observation=live)
        assert not store.record_attempt("kraken", observation=live)
        assert store.health()["kraken"]["consecutive_failures"] == 0
        queued = QueuedAttempt(source="kraken", observation=live)
        assert store.consume_attempt(queued) == "duplicate_observation"
        health = store.health()
        assert store.consume_attempt(queued) == "duplicate_attempt"
        assert store.health() == health
        store.set_coinbase_stream_status("recovering")
        previous = store.stream_states()["coinbase"]["last_trade_id"]
        trade_id = previous + 1 if previous is not None else 100
        streamed = observation.model_copy(
            update={
                "dataset": "live",
                "source": "coinbase",
                "provider_event_id": str(trade_id),
            }
        )
        assert store.record_coinbase_stream_batch([streamed], expected_trade_id=previous) == 1
        assert store.stream_states()["coinbase"]["last_trade_id"] == trade_id
    finally:
        engine.dispose()
        get_settings.cache_clear()


@pytest.fixture
def postgres_store(monkeypatch):
    database_url = os.getenv("VERDICT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("PostgreSQL URL not provided")
    monkeypatch.setenv("VERDICT_DATABASE_URL", database_url)
    get_settings.cache_clear()
    engine = build_engine(database_url)
    try:
        command.upgrade(Config("alembic.ini"), "head")
        yield ObservationStore(engine)
    finally:
        engine.dispose()
        get_settings.cache_clear()


@pytest.mark.parametrize("failure", [False, True])
def test_postgres_receipt_claims_serialize_and_survive_reconnection(
    postgres_store, market_observation, failure
):
    store = postgres_store
    row = market_observation(dataset="live", event=f"pg-receipt-{uuid.uuid4().hex}")
    queued = (
        QueuedAttempt(source="coinbase", error_type="ConnectError")
        if failure
        else (QueuedAttempt(source="coinbase", observation=row))
    )
    before = store.health().get("coinbase", {}).get("attempts", 0)
    with ThreadPoolExecutor(max_workers=4) as workers:
        outcomes = list(workers.map(lambda _: store.consume_attempt(queued), range(4)))
    expected = "recorded_error" if failure else "inserted"
    assert outcomes.count(expected) == 1
    assert outcomes.count("duplicate_attempt") == 3
    assert store.health()["coinbase"]["attempts"] == before + 1
    health = store.health()
    reopened = build_engine(str(store.engine.url))
    try:
        assert ObservationStore(reopened).consume_attempt(queued) == "duplicate_attempt"
    finally:
        reopened.dispose()
    assert store.health() == health
    changed = queued.model_copy(update={"attempted_at": queued.attempted_at + timedelta(seconds=1)})
    with pytest.raises(AttemptConflict):
        store.consume_attempt(changed)
    assert store.health() == health
    distinct = queued.model_copy(update={"attempt_id": uuid.uuid4()})
    assert store.consume_attempt(distinct) == (
        "recorded_error" if failure else "duplicate_observation"
    )
    assert store.health()["coinbase"]["attempts"] == before + 2


def test_postgres_failed_observation_rolls_back_receipt_and_health(
    postgres_store, market_observation
):
    store = postgres_store
    row = market_observation(dataset="live", event=f"pg-conflict-{uuid.uuid4().hex}")
    assert store.consume_attempt(QueuedAttempt(source="coinbase", observation=row)) == "inserted"
    changed = row.model_copy(update={"value": Decimal("999")})
    conflict = QueuedAttempt(source="coinbase", observation=changed)
    health = store.health()
    with pytest.raises(ObservationConflict):
        store.consume_attempt(conflict)
    with store.session() as session:
        assert session.get(IngestionReceiptRow, str(conflict.attempt_id)) is None
    assert store.health() == health
    corrected = conflict.model_copy(update={"observation": row})
    assert store.consume_attempt(corrected) == "duplicate_observation"
    assert store.consume_attempt(corrected) == "duplicate_attempt"


def test_postgres_positive_pipeline_quarantine_and_recovery(postgres_store):
    from verdict.adversarial import (
        CASES,
        AdversarialSuite,
        observations,
        run_case,
        synthetic_registry,
    )
    from verdict.replay import load_observations

    root = Path(__file__).resolve().parents[1]
    suite = AdversarialSuite.model_validate_json(
        (root / "benchmarks/adversarial-v1.json").read_text(encoding="utf-8")
    )
    registry = synthetic_registry(bundled_registry())
    rows = observations(
        load_observations(root / "simulations/six-provider.json"),
        suite,
        "single_spike",
        CASES["single_spike"],
        "synthetic",
    )
    dataset = f"replay:pg-pipeline-{uuid.uuid4().hex}"
    rows = [row.model_copy(update={"dataset": dataset}) for row in rows]
    expected = run_case(rows, suite, registry)
    store = postgres_store
    store.save_dependency_registry(registry)
    assert store.add_many(rows) == len(rows)
    for tick in expected:
        score = build_scoring_report(
            store,
            dataset=dataset,
            cutoff=datetime.fromisoformat(tick["cutoff"]),
            registry_id=registry.registry_id,
            policy=suite.scoring,
        )
        assert store.save_scoring_round(score)
        assert score.report_id == tick["scoring_report_id"]
        weight = store.apply_weighting(score.report_id, dataset=dataset, policy=suite.weighting)
        assert weight.report_id == tick["weighting_report_id"]
        candidate = build_aggregation_report(
            store, weighting_report_id=weight.report_id, dataset=dataset
        )
        assert store.save_aggregation_round(candidate)
        assert candidate.report_id == tick["aggregation_report_id"]
        quality = build_confidence_report(
            store,
            aggregation_report_id=candidate.report_id,
            dataset=dataset,
            policy=suite.confidence,
        )
        assert store.save_confidence_round(quality)
        assert quality.report_id == tick["assessment_id"]
        assert quality.value is quality.confidence is None and not quality.publication_ready
    assert expected[-1]["sources"]["coinbase"]["weight_bps"] > 0
    assert any(tick["candidate_value"] is not None for tick in expected)
