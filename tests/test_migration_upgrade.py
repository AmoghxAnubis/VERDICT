import json

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from verdict.config import get_settings
from verdict.storage import ObservationStore, build_engine


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
