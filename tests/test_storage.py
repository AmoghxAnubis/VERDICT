from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from verdict.replay import load_observations, replay
from verdict.storage import ObservationConflict, ObservationStore, build_engine

BASELINE = Path(__file__).resolve().parents[1] / "simulations" / "baseline.json"


def test_migrations_match_model_and_are_repeatable(store):
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")
    command.check(Config("alembic.ini"))
    store.check_ready()


def test_evidence_survives_new_database_connection(store, observation):
    assert store.add(observation)
    reopened = build_engine(str(store.engine.url))
    try:
        stored = ObservationStore(reopened).list(dataset=observation.dataset)[0]
        assert stored == observation
        assert stored.value == Decimal("3843.010000000000000001")
        assert stored.raw_payload == observation.raw_payload
    finally:
        reopened.dispose()


def test_duplicate_keeps_first_receipt_and_original_evidence(store, observation):
    assert store.add(observation)
    duplicate = observation.model_copy(
        update={
            "received_at": observation.received_at + timedelta(minutes=5),
            "raw_payload": {**observation.raw_payload, "volume": "9999"},
        }
    )
    assert not store.add(duplicate)
    assert store.list(dataset=observation.dataset) == [observation]


@pytest.mark.parametrize("field", ["value", "timestamp"])
def test_conflicting_provider_event_is_rejected(store, observation, field):
    store.add(observation)
    replacement = (
        observation.value + 1 if field == "value" else observation.timestamp + timedelta(seconds=1)
    )
    with pytest.raises(ObservationConflict):
        store.add(observation.model_copy(update={field: replacement}))
    assert store.list(dataset=observation.dataset) == [observation]


def test_out_of_order_arrivals_do_not_replace_newer_trade(store):
    observations = load_observations(BASELINE)
    for observation in observations:
        store.add(observation)
    assert [row.provider_event_id for row in store.list(dataset="replay:baseline")] == [
        "1003",
        "1002",
        "1001",
    ]
    assert store.list(dataset="live") == []


def test_replay_is_deterministic_and_idempotent(store):
    first = replay(BASELINE, store)
    second = replay(BASELINE, store)
    assert first["normalized_sha256"] == second["normalized_sha256"]
    assert (first["inserted"], first["duplicates"]) == (3, 1)
    assert (second["inserted"], second["duplicates"]) == (0, 4)


def test_invalid_replay_cannot_partially_write(store, tmp_path):
    import json

    scenario = json.loads(BASELINE.read_text())
    scenario["events"][-1]["payload"]["price"] = "-1"
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(scenario))
    with pytest.raises(ValueError):
        replay(path, store)
    assert store.list(dataset="replay:baseline") == []


def test_conflicting_batch_rolls_back_new_events(store, observation):
    store.add(observation)
    new_event = observation.model_copy(update={"provider_event_id": "new-event"})
    conflict = observation.model_copy(update={"value": Decimal("5200")})
    with pytest.raises(ObservationConflict):
        store.add_many([new_event, conflict])
    assert store.list(dataset=observation.dataset) == [observation]
