from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from verdict.api import create_app
from verdict.config import Settings


def test_readiness_reports_unmigrated_storage(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'empty.db').as_posix()}", _env_file=None
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").status_code == 200
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert "migrations" in response.json()["detail"]
        unavailable = client.get("/v1/observations")
        assert unavailable.status_code == 503
        assert unavailable.json() == {"detail": "storage unavailable"}


def test_api_preserves_decimal_precision_and_replay_isolation(store, observation):
    store.add(observation)
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/v1/observations").json() == []
        response = client.get("/v1/observations", params={"dataset": observation.dataset})
        assert response.status_code == 200
        row = response.json()[0]
        assert row["value"] == "3843.010000000000000001"
        assert row["raw_payload"] == observation.raw_payload
        assert row["observation_id"] == observation.observation_id


@pytest.mark.parametrize("age,status", [(1, "fresh"), (60, "stale"), (-60, "clock_skew")])
def test_source_status_uses_event_time(store, observation, age, status):
    now = datetime.now(UTC)
    live = observation.model_copy(
        update={"dataset": "live", "timestamp": now - timedelta(seconds=age), "received_at": now}
    )
    store.add(live)
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        row = client.get("/v1/sources").json()[0]
        assert row["observation_status"] == status
        assert row["trust_score"] is None


def test_empty_source_and_feed_are_explicitly_unavailable(store):
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        assert client.get("/v1/sources").json()[0]["observation_status"] == "unavailable"
        response = client.get("/v1/feed/ETH-USD")
        assert response.status_code == 503
        assert response.json()["detail"]["value"] is None
        assert response.json()["detail"]["confidence"] is None


@pytest.mark.parametrize(
    "query", [{"limit": 0}, {"limit": 1001}, {"dataset": "replay:../live"}, {"asset": "ETH-USDT"}]
)
def test_observation_query_validation(store, query):
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        assert client.get("/v1/observations", params=query).status_code == 422
