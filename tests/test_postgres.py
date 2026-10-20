import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config

from verdict.aggregation import build_aggregation_report
from verdict.config import get_settings
from verdict.dependencies import bundled_registry
from verdict.queue_models import QueuedAttempt
from verdict.scoring import ScoringPolicy, build_scoring_report
from verdict.statistics import analyze
from verdict.storage import ObservationStore, build_engine


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
