import json

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from verdict.config import get_settings
from verdict.statistics import analyze
from verdict.storage import ObservationStore, build_engine


def test_queue_upgrade_preserves_checkpoint_evidence_and_health(tmp_path, monkeypatch, observation):
    from verdict.queue_models import QueuedAttempt

    database_url = f"sqlite:///{(tmp_path / 'queue-upgrade.db').as_posix()}"
    monkeypatch.setenv("VERDICT_DATABASE_URL", database_url)
    get_settings.cache_clear()
    engine = build_engine(database_url)
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0004")
        store = ObservationStore(engine)
        store.set_coinbase_stream_status("recovering")
        live = observation.model_copy(update={"dataset": "live", "provider_event_id": "100"})
        store.record_coinbase_stream_batch([live], expected_trade_id=None)
        states, health = store.stream_states(), store.health()
        command.upgrade(config, "head")
        command.check(config)
        store.check_ready()
        assert store.stream_states() == states
        assert store.health() == health
        assert store.list() == [live]
        queued = QueuedAttempt(source="coinbase", observation=live)
        assert store.consume_attempt(queued) == "duplicate_observation"
        assert store.consume_attempt(queued) == "duplicate_attempt"
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_upgrade_backfills_receipt_time_without_changing_existing_evidence(
    tmp_path, monkeypatch, observation
):
    database_url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    monkeypatch.setenv("VERDICT_DATABASE_URL", database_url)
    get_settings.cache_clear()
    engine = build_engine(database_url)
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0001")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO observations (id,dataset,source,asset,timestamp,payload) "
                    "VALUES (:id,:dataset,:source,:asset,:timestamp,:payload)"
                ),
                {
                    "id": observation.observation_id,
                    "dataset": observation.dataset,
                    "source": observation.source,
                    "asset": observation.asset,
                    "timestamp": observation.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    "payload": observation.model_dump_json(),
                },
            )
        command.upgrade(config, "head")
        store = ObservationStore(engine)
        store.check_ready()
        assert store.list(dataset=observation.dataset) == [observation]
        with engine.connect() as connection:
            payload = connection.scalar(text("SELECT payload FROM observations"))
            assert json.loads(payload) == observation.model_dump(mode="json")
        assert store.snapshot(
            dataset=observation.dataset,
            cutoff=observation.received_at,
            history_seconds=300,
            freshness_seconds=30,
        ) == [observation]
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_stream_checkpoint_upgrade_preserves_existing_evidence_health_and_rounds(
    tmp_path, monkeypatch, observation
):
    database_url = f"sqlite:///{(tmp_path / 'stream-upgrade.db').as_posix()}"
    monkeypatch.setenv("VERDICT_DATABASE_URL", database_url)
    get_settings.cache_clear()
    engine = build_engine(database_url)
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0003")
        store = ObservationStore(engine)
        store.add(observation)
        store.record_attempt("coinbase", error_type="ConnectError")
        report = analyze([observation], dataset=observation.dataset, cutoff=observation.received_at)
        store.save_round(report)
        health = store.health()
        command.upgrade(config, "head")
        command.check(config)
        store.check_ready()
        assert store.list(dataset=observation.dataset) == [observation]
        assert store.health() == health
        assert store.get_round(report.round_id, dataset=observation.dataset) == report.model_dump(
            mode="json"
        )
        assert store.stream_states() == {}
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_sequence_upgrade_backfills_existing_payload_and_preserves_evidence(
    tmp_path, monkeypatch, observation
):
    database_url = f"sqlite:///{(tmp_path / 'sequence-upgrade.db').as_posix()}"
    monkeypatch.setenv("VERDICT_DATABASE_URL", database_url)
    get_settings.cache_clear()
    engine = build_engine(database_url)
    sequenced = observation.model_copy(update={"event_sequence": 777000001})
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0002")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO observations "
                    "(id,dataset,source,asset,timestamp,received_at,payload) "
                    "VALUES (:id,:dataset,:source,:asset,:timestamp,:received_at,:payload)"
                ),
                {
                    "id": sequenced.observation_id,
                    "dataset": sequenced.dataset,
                    "source": sequenced.source,
                    "asset": sequenced.asset,
                    "timestamp": sequenced.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    "received_at": sequenced.received_at.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    "payload": sequenced.model_dump_json(),
                },
            )
        command.upgrade(config, "head")
        command.check(config)
        store = ObservationStore(engine)
        assert store.list(dataset=sequenced.dataset) == [sequenced]
        with engine.connect() as connection:
            sequence, payload = connection.execute(
                text("SELECT event_sequence,payload FROM observations")
            ).one()
            assert sequence == sequenced.event_sequence
            assert payload == sequenced.model_dump_json()
        command.downgrade(config, "0002")
        command.upgrade(config, "head")
        assert store.list(dataset=sequenced.dataset) == [sequenced]
    finally:
        engine.dispose()
        get_settings.cache_clear()
