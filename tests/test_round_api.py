from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from verdict.api import create_app
from verdict.config import Settings
from verdict.replay import replay
from verdict.statistics import build_report

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 10, 16, tzinfo=UTC)


@pytest.mark.parametrize(
    "scenario,at,status",
    [
        ("market-baseline", 10, "analyzed"),
        ("single-outlier", 10, "insufficient_data"),
        ("market-jump", 10, "analyzed"),
        ("provider-outage", 40, "insufficient_data"),
    ],
)
def test_real_adapter_replays_have_expected_outcomes(store, scenario, at, status):
    result = replay(ROOT / "simulations" / f"{scenario}.json", store)
    report = build_report(store, dataset=result["dataset"], cutoff=BASE + timedelta(seconds=at))
    assert report.status == status
    assert not report.publication_ready
    if scenario == "single-outlier":
        assert report.sources[2].exclusion_reasons == ["extreme_deviation"]
    if scenario == "provider-outage":
        assert report.sources[2].exclusion_reasons == ["stale"]
    if scenario == "market-jump":
        assert all(row.signals == ["high_price_velocity"] for row in report.sources[:3])


def test_analysis_endpoint_is_read_only_and_persisted_rounds_are_isolated(store):
    replay(ROOT / "simulations" / "single-outlier.json", store)
    params = {"dataset": "replay:single-outlier", "at": "2026-10-16T00:00:10Z"}
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        response = client.get("/v1/analysis/ETH-USD", params=params)
        assert response.status_code == 200
        report = response.json()
        assert report["sources"][2]["exclusion_reasons"] == ["extreme_deviation"]
        assert client.get("/v1/rounds", params=params).json() == []
        persisted = build_report(
            store, dataset=params["dataset"], cutoff=BASE + timedelta(seconds=10)
        )
        store.save_round(persisted)
        assert client.get(f"/v1/rounds/{persisted.round_id}").status_code == 404
        assert client.get(f"/v1/rounds/{persisted.round_id}", params=params).json() == report
        assert client.get("/v1/analysis/ETH-USD", params=params).json() == report
        assert client.get("/v1/feed/ETH-USD").status_code == 503


@pytest.mark.parametrize(
    "params",
    [
        {"dataset": "replay:test"},
        {"at": "2026-10-16T00:00:00"},
        {"window": 0},
    ],
)
def test_analysis_rejects_ambiguous_cutoff_or_window(store, params):
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        assert client.get("/v1/analysis/ETH-USD", params=params).status_code == 422


def test_source_filters_and_replay_health_do_not_leak_live_state(store, market_observation):
    store.record_attempt("kraken", observation=market_observation("kraken", dataset="live"))
    store.add(market_observation("binance_us"))
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        live = client.get("/v1/sources").json()
        assert live[1]["connector_health"]["received"] == 1
        historical = client.get("/v1/sources", params={"dataset": "replay:test"}).json()
        assert all(row["connector_health"] is None for row in historical)
        assert client.get("/v1/observations", params={"source": "coinbase"}).json() == []
        assert len(client.get("/v1/observations", params={"source": "kraken"}).json()) == 1
