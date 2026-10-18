import os
import uuid

import pytest
from alembic import command
from alembic.config import Config

from verdict.config import get_settings
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
        isolated = observation.model_copy(update={"dataset": f"replay:postgres-{uuid.uuid4().hex}"})
        assert store.add(isolated)
        assert not store.add(isolated)
        assert store.list(dataset=isolated.dataset) == [isolated]
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
