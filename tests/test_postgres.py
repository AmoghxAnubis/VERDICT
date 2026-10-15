import os
import uuid

import pytest
from alembic import command
from alembic.config import Config

from verdict.config import get_settings
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
    finally:
        engine.dispose()
        get_settings.cache_clear()
