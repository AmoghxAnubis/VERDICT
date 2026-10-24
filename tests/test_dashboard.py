from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import delete, event
from starlette.websockets import WebSocketDisconnect

from verdict.api import create_app
from verdict.confidence import ConfidencePolicy, build_confidence_report
from verdict.config import Settings
from verdict.dashboard import seed_demo, snapshot
from verdict.storage import ObservationStore, RoundEventHeadRow, build_engine

SUITE = Path(__file__).resolve().parents[1] / "benchmarks/adversarial-v1.json"
DATASET = "replay:adv-synthetic-baseline"


@pytest.fixture
def seeded(store):
    seed_demo(store, SUITE, case="baseline")
    return store


def app(store):
    return create_app(Settings(_env_file=None), store)


def variant(store):
    latest = store.confidence_rounds(dataset=DATASET, limit=1)[0]
    return build_confidence_report(
        store,
        dataset=DATASET,
        aggregation_report_id=latest["aggregation_report_id"],
        policy=ConfidencePolicy(minimum_quality_bps=8000),
    )


def test_snapshot_pins_complete_round_and_never_supplies_usable_output(seeded):
    data = snapshot(seeded, dataset=DATASET)
    assert data["cursor"] == 25
    assert data["mode"] == "replay" and data["value"] is data["confidence"] is None
    assert data["assessment"]["candidate_value"] == "100"
    assert data["assessment"]["quality_score_bps"] == 9700
    assert len(data["sources"]) == 6 and len(data["history"]) == 25
    assert data["assessment"]["scoring_report_id"] == data["scoring_report_id"]
    assert data["aggregation"]["scoring_report_id"] == data["scoring_report_id"]
    assert data["weighting"]["registry_id"] == data["registry"]["registry_id"]
    assert all("content" not in evidence for evidence in data["registry"]["evidence"])
    first = data["history"][-1]
    earlier = snapshot(seeded, dataset=DATASET, assessment_id=first["report_id"])
    assert earlier["as_of"] == first["cutoff"]
    assert earlier["assessment"]["candidate_value"] is None  # cold-start probation
    assert all(source["weight_bps"] == 0 for source in earlier["sources"])


def test_live_and_replay_datasets_cannot_mix(seeded):
    with TestClient(app(seeded)) as client:
        live = client.get("/v1/dashboard").json()
        assert live["assessment"] is None and live["history"] == []
        assert all(source["value"] is None for source in live["sources"])
        report_id = seeded.confidence_rounds(dataset=DATASET, limit=1)[0]["report_id"]
        assert client.get("/v1/dashboard", params={"assessment_id": report_id}).status_code == 404
        assert client.get("/v1/datasets").json() == ["live", DATASET]
        assert client.get("/v1/feed/ETH-USD").status_code == 503


def test_demo_seed_is_idempotent_and_invalid_cases_write_nothing(seeded):
    head = seeded.event_cursor()
    seed_demo(seeded, SUITE, case="baseline")
    assert seeded.event_cursor() == head
    with pytest.raises(ValueError, match="unknown"):
        seed_demo(seeded, SUITE, case="live")
    assert seeded.datasets() == ["live", DATASET]
    assert seeded.list() == []


def test_paginated_events_resume_without_gaps_or_cross_dataset_payloads(seeded):
    cursor, found = 0, []
    while True:
        batch = seeded.round_events(dataset=DATASET, after=cursor, limit=2)
        found.extend(batch["events"])
        cursor = batch["cursor"]
        if not batch["has_more"]:
            break
    assert [item["cursor"] for item in found] == list(range(1, 26))
    assert all(item["dataset"] == DATASET for item in found)
    assert seeded.round_events(dataset="live", after=0) == {
        "events": [],
        "cursor": 25,
        "has_more": False,
    }
    assert seeded.round_events(dataset=DATASET, after=cursor)["events"] == []


def test_concurrent_idempotent_saves_create_only_one_durable_event(seeded):
    report = variant(seeded)
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: seeded.save_confidence_round(report), range(4)))
    assert outcomes.count(True) == 1
    assert seeded.event_cursor() == 26
    reopened = build_engine(str(seeded.engine.url))
    try:
        batch = ObservationStore(reopened).round_events(dataset=DATASET, after=25)
        assert batch["events"][0]["assessment_id"] == report.report_id
    finally:
        reopened.dispose()


def test_event_insert_failure_rolls_back_assessment_and_cursor(seeded):
    report = variant(seeded)

    def fail(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO round_events"):
            raise RuntimeError("simulated outbox failure")

    event.listen(seeded.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="outbox"):
            seeded.save_confidence_round(report)
    finally:
        event.remove(seeded.engine, "before_cursor_execute", fail)
    assert seeded.event_cursor() == 25
    assert seeded.get_confidence_round(report.report_id, dataset=DATASET) is None
    assert seeded.save_confidence_round(report)
    assert seeded.event_cursor() == 26


def test_websocket_delivers_new_round_and_reconnects_at_committed_cursor(seeded):
    report = variant(seeded)
    with TestClient(app(seeded)) as client:
        with client.websocket_connect(f"/v1/stream?dataset={DATASET}&after=25") as ws:
            assert ws.receive_json()["type"] == "hello"
            assert ws.receive_json()["events"] == []
            assert seeded.save_confidence_round(report)
            ws.send_text("ping")
            batch = ws.receive_json()
            assert batch["cursor"] == 26
            assert [item["assessment_id"] for item in batch["events"]] == [report.report_id]
        with client.websocket_connect(f"/v1/stream?dataset={DATASET}&after=26") as ws:
            ws.receive_json()
            assert ws.receive_json()["events"] == []
        with client.websocket_connect("/v1/stream?dataset=live&after=0") as ws:
            ws.receive_json()
            assert ws.receive_json()["events"] == []


@pytest.mark.parametrize(
    "query", ["after=-1", "after=no", "after=999999", "dataset=private", "after=１２"]
)
def test_websocket_rejects_invalid_resume_boundaries(store, query):
    with TestClient(app(store)) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/v1/stream?{query}"):
                pass
        assert exc.value.code == 1008


def test_websocket_rejects_untrusted_origin_and_unsupported_commands(store):
    with TestClient(app(store)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/v1/stream", headers={"origin": "https://untrusted.example"}
            ):
                pass
        with client.websocket_connect(
            "/v1/stream", headers={"origin": "http://127.0.0.1:3000"}
        ) as ws:
            ws.receive_json()
            ws.receive_json()
            ws.send_text("publish")
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()


def test_upgrade_preserves_old_reports_and_starts_explicitly_new_stream(seeded):
    before = seeded.confidence_rounds(dataset=DATASET, limit=100)
    command.downgrade(Config("alembic.ini"), "0010")
    command.upgrade(Config("alembic.ini"), "head")
    command.check(Config("alembic.ini"))
    assert seeded.confidence_rounds(dataset=DATASET, limit=100) == before
    assert seeded.event_cursor() == 0
    assert snapshot(seeded, dataset=DATASET)["assessment"] is not None
    assert seeded.save_confidence_round(variant(seeded))
    assert seeded.event_cursor() == 1


def test_missing_event_head_fails_readiness(store):
    with store.engine.begin() as connection:
        connection.execute(delete(RoundEventHeadRow))
    with TestClient(app(store)) as client:
        assert client.get("/health/ready").status_code == 503
        assert client.get("/v1/dashboard").status_code == 503
