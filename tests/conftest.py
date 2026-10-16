from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from verdict.config import get_settings
from verdict.models import Observation
from verdict.replay import load_observations
from verdict.storage import ObservationStore, build_engine

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path, monkeypatch):
    database_url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("VERDICT_DATABASE_URL", database_url)
    get_settings.cache_clear()
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    engine = build_engine(database_url)
    try:
        yield ObservationStore(engine)
    finally:
        engine.dispose()
        get_settings.cache_clear()


@pytest.fixture
def observation():
    return load_observations(ROOT / "simulations" / "baseline.json")[0]


@pytest.fixture
def market_observation():
    def make(
        source="coinbase", price="100", time=0, received=None, event=None, dataset="replay:test"
    ):
        base = datetime(2026, 10, 16, tzinfo=UTC)
        return Observation(
            dataset=dataset,
            source=source,
            provider_event_id=event or f"{source}-{time}",
            value=price,
            timestamp=base + timedelta(seconds=time),
            received_at=base + timedelta(seconds=received if received is not None else time),
            latency_ms=1,
            raw_payload={"fixture": True},
        )

    return make
