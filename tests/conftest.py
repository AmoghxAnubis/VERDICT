from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from verdict.config import get_settings
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
